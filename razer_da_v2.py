#!/usr/bin/env python3
"""
Razer DeathAdder V2 (USB 1532:0084) control for macOS: importable module + tiny CLI.
Every command below is taken from the open-source OpenRazer Linux driver
(driver/razerchromacommon.c, razermouse_driver.c) and is enabled there for this exact PID.

Setup:   pip install hidapi
Usage:   python3 razer_da_v2.py status                  # read current settings
         python3 razer_da_v2.py color 00ff88            # static, both zones
         python3 razer_da_v2.py effect breath --zone logo --colors ff0000 0000ff
         python3 razer_da_v2.py brightness 128          # 0-255
         python3 razer_da_v2.py off
         python3 razer_da_v2.py dpi 1600                # 100-20000
         python3 razer_da_v2.py poll 1000               # 125 / 500 / 1000 Hz
         python3 razer_da_v2.py list                    # show detected interfaces
"""
import argparse
import ctypes
import sys
import time

import hid

VID, PID = 0x1532, 0x0084
TRANSACTION_ID = 0x3F          # OpenRazer uses 0x3F for every DeathAdder V2 command
VARSTORE, NOSTORE = 0x01, 0x00
ZONES = {"logo": 0x04, "wheel": 0x01}
DPI_MIN, DPI_MAX = 100, 20000  # OpenRazer daemon: RazerDeathAdderV2.DPI_MAX
POLL_RATES = {1000: 0x01, 500: 0x02, 125: 0x08}
STATUS = {0x00: "new", 0x01: "busy", 0x02: "ok", 0x03: "failed",
          0x04: "timeout", 0x05: "not supported"}
# Effects OpenRazer enables for 0x0084 on both zones (daemon RazerDeathAdderV2.METHODS).
EFFECTS = ("static", "breath", "spectrum", "reactive", "none")

# hidapi on macOS seizes (opens exclusively) by default, which macOS refuses for a mouse;
# without this only interface 3 opens, and that one just echoes reports back unprocessed.
_lib = ctypes.CDLL(hid.__file__)
_lib.hid_init()
_lib.hid_darwin_set_open_exclusive(0)


class RazerError(Exception):
    pass


def build_report(cmd_class, cmd_id, args, size=None, txn=TRANSACTION_ID):
    """90-byte Razer report: status, txn id, remaining(2), proto, size, class, id, args[80], crc, reserved."""
    r = bytearray(90)
    r[1] = txn
    r[5] = len(args) if size is None else size
    r[6] = cmd_class
    r[7] = cmd_id
    r[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in r[2:88]:
        crc ^= b
    r[88] = crc
    return r


class Mouse:
    def __init__(self):
        found = hid.enumerate(VID, PID)
        if not found:
            raise RazerError("DeathAdder V2 not found. Is it plugged in?")
        # Control requests only work on interface 0.
        paths = {d["path"] for d in found if d.get("interface_number") == 0}
        err = None
        for p in paths:
            self.dev = hid.device()
            try:
                self.dev.open_path(p)
                return
            except OSError as e:
                err = e
        raise RazerError(f"Found the DeathAdder V2 but couldn't open it ({err}). "
                         "Allow this app under System Settings > Privacy & Security > Input Monitoring.")

    def close(self):
        self.dev.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def cmd(self, cmd_class, cmd_id, args, size=None, txn=TRANSACTION_ID):
        """Send one report, return the 80 argument bytes of the reply (retries like OpenRazer)."""
        report = bytes([0x00]) + bytes(build_report(cmd_class, cmd_id, args, size, txn))  # 0x00 = report ID
        status = "no reply"
        for _ in range(5):
            try:
                self.dev.send_feature_report(report)
                time.sleep(0.06)                      # mouse needs a moment
                resp = bytes(self.dev.get_feature_report(0x00, 91))
            except (OSError, ValueError) as e:
                raise RazerError(f"USB error talking to the mouse: {e}. Was it unplugged?")
            resp = resp[1:] if len(resp) == 91 else resp
            if len(resp) < 90 or resp[6] != cmd_class or resp[7] != cmd_id:
                status = "mismatched reply"
            else:
                status = STATUS.get(resp[0], f"unknown 0x{resp[0]:02x}")
                if status in ("ok", "busy"):
                    return resp[8:88]
                if status == "not supported":
                    break
            time.sleep(0.01)
        raise RazerError(f"Mouse replied '{status}' to command {cmd_class:#04x}/{cmd_id:#04x}")

    # --- read-only ---------------------------------------------------------
    def firmware(self):
        a = self.cmd(0x00, 0x81, [0, 0])
        return f"v{a[0]}.{a[1]:02d}"

    def poll_rate(self):
        code = self.cmd(0x00, 0x85, [0])[0]
        return {v: k for k, v in POLL_RATES.items()}.get(code, code)

    def dpi(self):
        a = self.cmd(0x04, 0x85, [NOSTORE, 0, 0, 0, 0, 0, 0])
        return (a[1] << 8) | a[2], (a[3] << 8) | a[4]

    def brightness(self, zone):
        return self.cmd(0x0F, 0x84, [VARSTORE, ZONES[zone], 0])[2]

    def dpi_stages(self):
        """(active stage 1-based, [dpi per stage]). OpenRazer razer_chroma_misc_get_dpi_stages.
        Not enabled for 0x0084 in OpenRazer; uses the sibling DA V2 Pro/Mini txn id 0xFF,
        verified read-only on a real 0x0084 (fw v1.01) before enabling."""
        a = self.cmd(0x04, 0x86, [VARSTORE], size=0x26, txn=0xFF)
        active, count = a[1], a[2]
        return active, [(a[3 + 7 * i + 1] << 8) | a[3 + 7 * i + 2] for i in range(count)]

    def read_state(self):
        return {"firmware": self.firmware(), "poll_rate": self.poll_rate(), "dpi": self.dpi(),
                "dpi_stages": self.dpi_stages(), "brightness": {z: self.brightness(z) for z in ZONES}}

    # --- write ---------------------------------------------------------------
    def set_poll_rate(self, hz):
        self.cmd(0x00, 0x05, [POLL_RATES[hz]])

    def set_dpi(self, x, y=None):
        x = max(DPI_MIN, min(DPI_MAX, int(x)))
        y = x if y is None else max(DPI_MIN, min(DPI_MAX, int(y)))
        # OpenRazer's razer_chroma_misc_set_dpi_xy always sends VARSTORE here.
        self.cmd(0x04, 0x05, [VARSTORE, x >> 8, x & 0xFF, y >> 8, y & 0xFF, 0, 0])

    def set_dpi_stages(self, active, dpis):
        """OpenRazer razer_chroma_misc_set_dpi_stages layout: varstore, active, count, then per stage
        [index, x hi, x lo, y hi, y lo, 0, 0]. Active is 1-based. Max 5 stages."""
        dpis = [max(DPI_MIN, min(DPI_MAX, int(d))) for d in dpis][:5]
        if not dpis or not 1 <= active <= len(dpis):
            raise ValueError("need 1-5 stages and an active stage within them")
        args = [VARSTORE, active, len(dpis)]
        for i, d in enumerate(dpis):
            args += [i, d >> 8, d & 0xFF, d >> 8, d & 0xFF, 0, 0]
        self.cmd(0x04, 0x06, args, size=0x26, txn=0xFF)

    def set_brightness(self, zone, level):
        self.cmd(0x0F, 0x04, [VARSTORE, ZONES[zone], max(0, min(255, int(level)))])

    def set_effect(self, zone, effect, colors=(), speed=2):
        """effect in EFFECTS. breath: 0 colours = random, 1 = single, 2 = dual.
        reactive: 1 colour, speed 1-4 (1 = shortest afterglow)."""
        z = ZONES[zone]
        c = [v for rgb in colors for v in rgb]
        # Argument layouts copied from razer_chroma_extended_matrix_effect_* (class 0x0F, id 0x02).
        if effect == "none":
            args = [VARSTORE, z, 0x00, 0, 0, 0]
        elif effect == "static":
            args = [VARSTORE, z, 0x01, 0, 0, 0x01] + c[:3]
        elif effect == "spectrum":
            args = [VARSTORE, z, 0x03, 0, 0, 0]
        elif effect == "reactive":
            args = [VARSTORE, z, 0x05, 0, max(1, min(4, speed)), 0x01] + c[:3]
        elif effect == "breath" and len(colors) == 0:
            args = [VARSTORE, z, 0x02, 0, 0, 0]
        elif effect == "breath" and len(colors) == 1:
            args = [VARSTORE, z, 0x02, 0x01, 0, 0x01] + c[:3]
        elif effect == "breath":
            args = [VARSTORE, z, 0x02, 0x02, 0, 0x02] + c[:6]
        else:
            raise ValueError(f"unknown effect {effect!r}")
        if effect in ("static", "reactive") and len(colors) < 1:
            raise ValueError(f"{effect} needs a colour")
        self.cmd(0x0F, 0x02, args)


def parse_hex(s):
    s = s.lstrip("#")
    if len(s) != 6:
        raise argparse.ArgumentTypeError("use a hex colour like ff8800")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def main():
    p = argparse.ArgumentParser(description="Razer DeathAdder V2 control for macOS")
    sub = p.add_subparsers(dest="cmd", required=True)
    zone = dict(choices=["logo", "wheel", "both"], default="both")
    c = sub.add_parser("color"); c.add_argument("hex", type=parse_hex); c.add_argument("--zone", **zone)
    e = sub.add_parser("effect"); e.add_argument("name", choices=EFFECTS); e.add_argument("--zone", **zone)
    e.add_argument("--colors", nargs="*", type=parse_hex, default=[]); e.add_argument("--speed", type=int, default=2)
    b = sub.add_parser("brightness"); b.add_argument("level", type=int); b.add_argument("--zone", **zone)
    sub.add_parser("off")
    d = sub.add_parser("dpi"); d.add_argument("value", type=int)
    pl = sub.add_parser("poll"); pl.add_argument("hz", type=int, choices=sorted(POLL_RATES))
    sub.add_parser("status")
    sub.add_parser("list")
    a = p.parse_args()

    if a.cmd == "list":
        for d in hid.enumerate(VID, PID):
            print(f"interface {d.get('interface_number')}  usage_page {d.get('usage_page'):#06x}  {d['path']}")
        return
    zones = list(ZONES) if getattr(a, "zone", "both") == "both" else [a.zone]
    try:
        with Mouse() as m:
            if a.cmd == "status":
                for k, v in m.read_state().items():
                    print(f"{k:11} {v}")
                return
            for z in zones:
                if a.cmd == "color":
                    m.set_effect(z, "static", [a.hex])
                elif a.cmd == "effect":
                    m.set_effect(z, a.name, a.colors, a.speed)
                elif a.cmd == "brightness":
                    m.set_brightness(z, a.level)
                elif a.cmd == "off":
                    m.set_brightness(z, 0)
            if a.cmd == "dpi":
                m.set_dpi(a.value)
            elif a.cmd == "poll":
                m.set_poll_rate(a.hz)
        print("done")
    except (RazerError, ValueError) as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    main()
