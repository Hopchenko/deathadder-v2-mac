"""Menu-bar app for the Razer DeathAdder V2. Run: .venv/bin/python app.py

The panel is ui.html in a WKWebView inside an NSPopover. The page sends {cmd, ...} messages here;
we answer by calling setState(json) / toast(msg) / buttonError(msg) in the page."""
import base64
import ctypes
import json
import queue
import os
import subprocess
import sys
import threading
import time
import traceback

import objc
from AppKit import (NSApp, NSAppearance, NSAppearanceNameAqua, NSAppearanceNameDarkAqua, NSApplication,
                    NSApplicationActivationPolicyAccessory, NSColor, NSColorPanel, NSColorSpace, NSImage,
                    NSMakeRect, NSMakeSize, NSMinYEdge, NSPopover, NSPopoverBehaviorTransient, NSScreen, NSStatusBar,
                    NSVariableStatusItemLength, NSViewController, NSWorkspace, NSWorkspaceDidWakeNotification,
                    NSWorkspaceScreensDidSleepNotification, NSWorkspaceScreensDidWakeNotification,
                    NSWorkspaceWillSleepNotification, NSFont, NSFontAttributeName, NSAttributedString)
from Foundation import NSDictionary, NSDistributedNotificationCenter, NSObject, NSTimer, NSURL
from PyObjCTools import AppHelper
from ServiceManagement import SMAppService
from WebKit import WKWebView, WKWebViewConfiguration

import ambient
import hud
import razer_da_v2 as razer
import remap

CONFIG = os.path.expanduser("~/Library/Application Support/RazerDAV2/config.json")
HERE = os.environ.get("RESOURCEPATH", os.path.dirname(os.path.abspath(__file__)))   # py2app sets RESOURCEPATH
SYNC_SECONDS = 0.12    # how often we re-read DPI from the mouse (catches the DPI button); each read is ~1 ms
WIDTH = 376
LIGHT_MODES = ("off", "manual", "screen", "night")
# Page messages handled on the main thread (windows, panels, system settings, event tap); the rest go to the worker.
MAIN_THREAD_CMDS = {"ready", "size", "theme", "login", "dpiPopup", "quit", "openSettings", "openAccessibility", "customColour",
                    "remap", "button", "resetButtons"}
THEMES = {"system": None, "light": NSAppearanceNameAqua, "dark": NSAppearanceNameDarkAqua}   # popover appearance
BUSY_RED = (255, 0, 0)
BUSY_LEVEL = 200        # busy light brightness (0-255)
MIN_AUTO = 12           # lowest LED level 'match screen brightness' goes to (0-255)
# UI effect key -> (device effect, number of colours it uses)
EFFECTS = {"static": ("static", 1), "breath1": ("breath", 1), "breath2": ("breath", 2), "breathR": ("breath", 0),
           "spectrum": ("spectrum", 0), "reactive": ("reactive", 1), "off": ("none", 0)}
AX_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
INPUT_MONITORING_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def symbol(name):
    img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "DeathAdder V2")
    img.setTemplate_(True)
    return img


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def from_hex(s):
    s = str(s).lstrip("#")
    if len(s) != 6:
        raise ValueError(f"bad colour {s!r}")
    return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))


def ask_accessibility():
    """Show macOS's 'allow this app to control your computer' prompt (adds the app to the Accessibility list)."""
    lib = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    lib.AXIsProcessTrustedWithOptions.restype = ctypes.c_bool
    lib.AXIsProcessTrustedWithOptions.argtypes = [ctypes.c_void_p]
    opts = NSDictionary.dictionaryWithObject_forKey_(True, "AXTrustedCheckOptionPrompt")
    return lib.AXIsProcessTrustedWithOptions(objc.pyobjc_id(opts))


def light_mode_from(cfg):
    """Saved brightness mode; older configs had two separate switches instead."""
    mode = cfg.get("light_mode")
    if mode in LIGHT_MODES:
        return mode
    return "screen" if cfg.get("auto_brightness") else "night" if cfg.get("night_calm") else "manual"


def effect_key(light):
    eff, n = light.get("effect"), len(light.get("colors", []))
    return next((k for k, (e, c) in EFFECTS.items() if e == eff and (e != "breath" or c == n)), None)


class Bridge(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """Objective-C callbacks (page messages, status item click, colour panel, timer) forwarded to App."""
    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        self.app.on_message(message.body())

    def toggle_(self, sender):
        self.app.toggle()

    def colourChanged_(self, panel):
        self.app.colour_changed(panel)

    def tick_(self, timer):
        self.app.tick()

    def screenSlept_(self, note):
        self.app.on_power("screen", True)

    def screenWoke_(self, note):
        self.app.on_power("screen", False)

    def locked_(self, note):
        self.app.on_power("lock", True)

    def unlocked_(self, note):
        self.app.on_power("lock", False)


class App:
    def __init__(self):
        self.bridge = Bridge.alloc().init()
        self.bridge.app = self
        cfg = load_config()
        self.zone = cfg.get("zone", "both")
        # Effects/colours can't be read back from the mouse (no documented command), so we remember them.
        self.lighting = cfg.get("lighting", {z: {"effect": "static", "colors": [[0, 255, 0]]} for z in razer.ZONES})
        self.colors = [tuple(c) for c in cfg.get("colors", [[0, 255, 0], [0, 0, 255]])]
        self.buttons = cfg.get("buttons", {name: "default" for name in remap.BUTTONS})
        self.want_remap = cfg.get("remap_on", False)   # what the user asked for (saved)
        self.remap_on = False                           # whether the event tap is actually running
        self.retry_ticks = 0
        self.saved_brightness = cfg.get("saved_brightness", 178)
        # off | manual | screen (follow the Mac screen) | night (your level, warmer + dimmer during Night Shift)
        self.light_mode = light_mode_from(cfg)
        self.theme = cfg.get("theme") if cfg.get("theme") in THEMES else "system"
        self.sleep_off = cfg.get("sleep_off", True)       # LEDs off while the Mac sleeps or is locked
        self.busy_light = cfg.get("busy_light", False)    # red while a mic or camera is in use
        self.dpi_popup = cfg.get("dpi_popup", True)       # overlay when the mouse's DPI button is pressed
        self.sleeping = False                             # LEDs currently off for sleep/lock
        self.screen_asleep = self.locked = False
        self.pre_sleep = None                             # brightness per zone to restore on wake
        self.busy_on = False                              # busy light currently showing
        self.dpi_set_at = 0.0                             # when the app itself last changed DPI
        self.hud = None
        self.hud_gen = 0
        self.night_on = False                                    # night tint currently applied
        self.ambient_ticks = 0
        self.fading = False
        self.state = None
        self.error = None
        self.pending_colour = None
        self.panel_slot = 0
        self.lock = threading.Lock()         # one HID conversation at a time (UI actions + sync thread)
        self.save_lock = threading.Lock()
        self._conn = None                    # shared mouse connection, see conn()
        self.remapper = remap.Remapper()
        self.build_ui()
        self.watch_power()
        if self.want_remap:
            self.start_remap(prompt=False)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.2, self.bridge, "tick:", None, True)
        self.jobs = queue.Queue()                # USB work runs here, off the main thread, one job at a time
        self.follow_pending = False
        threading.Thread(target=self.worker, daemon=True).start()
        self.run(lambda: self.refresh(quiet=True))
        threading.Thread(target=self.sync_loop, daemon=True).start()

    # --- window plumbing ---------------------------------------------------
    def build_ui(self):
        # logo.png is optional (kept out of the public repo): without it we use a plain mouse symbol.
        self.logo_path = os.path.join(HERE, "logo.png")
        self.logo = NSImage.alloc().initWithContentsOfFile_(self.logo_path) if os.path.exists(self.logo_path) else None
        if self.logo:
            self.logo.setSize_(NSMakeSize(18, 18))
            self.logo.setTemplate_(True)                         # macOS tints it to match the menu bar
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item_width = None
        self.item.button().setTarget_(self.bridge)
        self.item.button().setAction_("toggle:")

        conf = WKWebViewConfiguration.alloc().init()
        conf.userContentController().addScriptMessageHandler_name_(self.bridge, "app")
        self.web = WKWebView.alloc().initWithFrame_configuration_(NSMakeRect(0, 0, WIDTH, 600), conf)
        self.web.setValue_forKey_(False, "drawsBackground")     # let the popover's dark material show through
        self.web.setAutoresizingMask_(18)                        # width + height sizable
        page = os.path.join(HERE, "ui.html")
        self.web.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(page), NSURL.fileURLWithPath_(HERE))

        vc = NSViewController.alloc().init()
        vc.setView_(self.web)
        self.popover = NSPopover.alloc().init()
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_(NSMakeSize(WIDTH, 600))
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.apply_theme()
        self.update_status_item()

    def watch_power(self):
        """Screen sleep, system sleep and lock/unlock, so the LEDs can go dark with the Mac."""
        wc = NSWorkspace.sharedWorkspace().notificationCenter()
        for name, sel in [(NSWorkspaceScreensDidSleepNotification, "screenSlept:"),
                          (NSWorkspaceWillSleepNotification, "screenSlept:"),
                          (NSWorkspaceScreensDidWakeNotification, "screenWoke:"),
                          (NSWorkspaceDidWakeNotification, "screenWoke:")]:
            wc.addObserver_selector_name_object_(self.bridge, sel, name, None)
        dc = NSDistributedNotificationCenter.defaultCenter()
        dc.addObserver_selector_name_object_(self.bridge, "locked:", "com.apple.screenIsLocked", None)
        dc.addObserver_selector_name_object_(self.bridge, "unlocked:", "com.apple.screenIsUnlocked", None)

    def toggle(self):
        if self.popover.isShown():
            self.popover.performClose_(None)
            return
        NSApp.activateIgnoringOtherApps_(True)                   # so the page gets key presses (shortcut recorder)
        b = self.item.button()
        self.popover.showRelativeToRect_ofView_preferredEdge_(b.bounds(), b, NSMinYEdge)
        self.web.window().makeFirstResponder_(self.web)
        self.push()

    def run(self, fn):
        """Queue fn for the worker thread."""
        self.jobs.put(fn)

    def worker(self):
        while True:
            fn = self.jobs.get()
            try:
                fn()
            except Exception as e:               # never let a UI action kill the app
                traceback.print_exc()
                self.toast(f"Something went wrong: {e}")
                self.push()

    def js(self, code):
        if threading.current_thread() is not threading.main_thread():
            AppHelper.callAfter(self.js, code)   # WebKit and AppKit only work on the main thread
            return
        self.web.evaluateJavaScript_completionHandler_(code, None)

    def push(self):
        if threading.current_thread() is not threading.main_thread():
            AppHelper.callAfter(self.push)
            return
        self.update_status_item()
        self.js(f"setState({json.dumps(self.ui_state())})")

    def toast(self, msg):
        self.js(f"toast({json.dumps(msg)})")

    def ui_state(self):
        s = self.state or {}
        zone = "logo" if self.zone == "both" else self.zone
        return {"connected": bool(self.state), "error": self.error, "firmware": s.get("firmware"),
                "dpi": s["dpi"][0] if s else 0, "stages": list(s.get("dpi_stages", (1, []))),
                "poll": s.get("poll_rate"), "zone": self.zone, "effect": effect_key(self.lighting[zone]),
                "colors": [to_hex(c) for c in self.colors],
                "brightness": s.get("brightness", {}).get(zone, 0),
                "lightsOn": any(s.get("brightness", {}).values()),
                "lightMode": self.light_mode, "theme": self.theme, "login": self.login_state(),
                "sleepOff": self.sleep_off, "busyLight": self.busy_light, "busyOn": self.busy_on,
                "dpiPopup": self.dpi_popup, "autoAvailable": ambient.screen_brightness() is not None,
                "nightOn": self.night_on, "yourLevel": self.saved_brightness,
                "remapOn": self.want_remap, "remapWaiting": self.want_remap and not self.remap_on,
                "buttons": self.buttons}

    def update_status_item(self):
        b, s = self.item.button(), self.state
        b.setImage_((self.logo or symbol("computermouse.fill")) if s else symbol("exclamationmark.triangle"))
        if s:
            # Even-width digits and a fixed item width: otherwise the item (and the open popover anchored to it)
            # shifts sideways whenever the DPI changes, e.g. from 800 to 1600.
            font = NSFont.monospacedDigitSystemFontOfSize_weight_(NSFont.menuBarFontOfSize_(0).pointSize(), 0)
            b.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
                f" {s['dpi'][0]}", {NSFontAttributeName: font}))
            if self.item_width is None:
                widest = NSAttributedString.alloc().initWithString_attributes_(" 20000", {NSFontAttributeName: font})
                self.item_width = round(widest.size().width + 18 + 12)    # + 18 pt icon + padding
            self.item.setLength_(self.item_width)
        else:
            b.setTitle_("")
            self.item.setLength_(NSVariableStatusItemLength)              # just the warning icon
        b.setToolTip_("DeathAdder V2" if s else "DeathAdder V2: not connected")

    # --- messages from the page ---------------------------------------------
    def on_message(self, body):
        body = dict(body)
        cmd = str(body.pop("cmd"))
        handler = getattr(self, "on_" + cmd, None)
        if handler is None:
            return
        if cmd in MAIN_THREAD_CMDS:              # window/system calls stay on the main thread
            try:
                handler(**body)
            except Exception as e:               # never let a UI action kill the app
                traceback.print_exc()
                self.toast(f"Something went wrong: {e}")
                self.push()
        else:                                    # anything that may talk to the mouse
            self.run(lambda: handler(**body))

    def on_ready(self):
        if self.logo:
            with open(self.logo_path, "rb") as f:
                self.js(f"setLogo({json.dumps('data:image/png;base64,' + base64.b64encode(f.read()).decode())})")
        self.push()

    def on_size(self, h):
        screen = NSScreen.mainScreen().visibleFrame().size.height - 40   # the page scrolls if it's still taller
        self.popover.setContentSize_(NSMakeSize(WIDTH, max(200, min(int(h), int(screen)))))

    def on_refresh(self):
        self.refresh()

    def on_theme(self, theme):
        if theme in THEMES:
            self.theme = theme
            self.save()
            self.apply_theme()
        self.push()

    def apply_theme(self):
        """None follows macOS; WebKit passes the popover's appearance to the page as prefers-color-scheme."""
        name = THEMES[self.theme]
        self.popover.setAppearance_(NSAppearance.appearanceNamed_(name) if name else None)

    def login_state(self):
        """'on' | 'off' | 'approval' (blocked in System Settings) | 'unavailable' (running from source, not the .app)."""
        if getattr(sys, "frozen", None) != "macosx_app":
            return "unavailable"
        return {1: "on", 2: "approval"}.get(SMAppService.mainAppService().status(), "off")

    def on_login(self, on):
        svc = SMAppService.mainAppService()
        ok, err = (svc.registerAndReturnError_(None) if on else svc.unregisterAndReturnError_(None))
        if not ok:
            self.toast(f"Couldn't change Open at login: {err.localizedDescription() if err else 'unknown error'}")
        if self.login_state() == "approval":                 # macOS wants you to allow it in Login Items
            SMAppService.openSystemSettingsLoginItems()
        self.push()

    def on_openSettings(self):
        self.popover.performClose_(None)
        subprocess.run(["open", INPUT_MONITORING_URL])

    def on_quit(self):
        NSApp.terminate_(None)

    # sensor
    def on_dpi(self, dpi):
        self.dpi_set_at = time.time()
        self.mouse(lambda m: m.set_dpi(int(dpi)))
        self.refresh_sensor()

    def on_stage(self, i):
        self.dpi_set_at = time.time()
        _, stages = self.state["dpi_stages"]
        self.mouse(lambda m: m.set_dpi_stages(int(i), stages))
        self.refresh_sensor()

    def on_stages(self, active, list):
        self.dpi_set_at = time.time()
        values = [int(d) for d in list]
        self.mouse(lambda m: m.set_dpi_stages(int(active), values))
        self.refresh_sensor()

    def on_poll(self, hz):
        self.mouse(lambda m: m.set_poll_rate(int(hz)))
        self.refresh_sensor()

    # lighting
    def on_zone(self, zone):
        if zone in ("both", *razer.ZONES):
            self.zone = zone
            self.save()
        self.push()

    def on_effect(self, effect):
        eff, n = EFFECTS[effect]
        self.apply_effect(eff, n)

    def on_colour(self, slot, hex):
        self.set_colour(int(slot), from_hex(hex))

    def on_customColour(self, slot):
        self.panel_slot = int(slot)
        panel = NSColorPanel.sharedColorPanel()
        panel.setShowsAlpha_(False)
        panel.setContinuous_(True)
        panel.setTitle_("DeathAdder V2: " + ("colour" if self.panel_slot == 0 else "second colour"))
        panel.setColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(*(v / 255 for v in self.colors[self.panel_slot]), 1))
        panel.setTarget_(self.bridge)
        panel.setAction_("colourChanged:")
        panel.makeKeyAndOrderFront_(None)

    def colour_changed(self, panel):
        c = panel.color().colorUsingColorSpace_(NSColorSpace.sRGBColorSpace())
        self.pending_colour = (self.panel_slot, tuple(round(v * 255) for v in
                                                      (c.redComponent(), c.greenComponent(), c.blueComponent())))

    def tick(self):
        """Colour panel fires continuously; send at most one colour to the mouse per tick."""
        if self.pending_colour is not None:
            (slot, rgb), self.pending_colour = self.pending_colour, None
            if rgb != self.colors[slot]:
                self.run(lambda: self.set_colour(slot, rgb, quiet=True))
        self.ambient_ticks += 1
        night_check = self.ambient_ticks >= 10      # Night Shift: every 2 s; brightness: every tick
        if night_check:
            self.ambient_ticks = 0
        if not self.follow_pending:                 # skip a tick rather than pile up work if USB is slow
            self.follow_pending = True
            self.run(lambda: self.follow_tick(night_check))
        # Waiting for Accessibility permission: retry every 2 s so it starts as soon as it's granted.
        if self.want_remap and not self.remap_on:
            self.retry_ticks += 1
            if self.retry_ticks >= 10:
                self.retry_ticks = 0
                self.start_remap(prompt=False)

    def follow_tick(self, check_night):
        try:
            self.follow_mac(check_night=check_night)
        finally:
            self.follow_pending = False

    def on_brightness(self, level):
        level = max(0, min(255, int(level)))
        if self.night_on and level:              # slider is your daytime level; the LEDs get the dimmed one
            self.saved_brightness = level
            self.save()
            self.set_brightness(self.target_level(), self.zones(), remember=False)
        else:
            self.set_brightness(level, self.zones())

    def on_mode(self, mode):
        if mode not in LIGHT_MODES:
            return
        self.light_mode = mode
        self.save()
        if mode == "off":
            self.set_brightness(0, list(razer.ZONES), remember=False)   # always both zones
        else:
            self.follow_mac(force=True)          # jump to the new mode's level (and tint) right away
        self.push()

    @property
    def auto_bright(self):
        return self.light_mode == "screen"

    @property
    def night_calm(self):
        return self.light_mode == "night"

    def target_level(self):
        """LED level the app wants right now: the screen's brightness (auto) or your own, dimmed at night."""
        level = self.saved_brightness or 178
        screen = ambient.screen_brightness() if self.auto_bright else None
        if screen is not None:
            # LEDs look much brighter at low duty than their number suggests, so square the screen value
            # (roughly the eye's gamma) to make equal screen steps look like equal LED steps.
            level = round(MIN_AUTO + screen ** 2 * (255 - MIN_AUTO))   # never fully dark while lights are on
        if self.busy_on:
            return BUSY_LEVEL
        return round(level * 0.6) if self.night_on else level

    def follow_mac(self, force=False, check_night=True):
        """Every tick (0.2 s): fade the LEDs towards the screen brightness. Night Shift is checked every 2 s.
        force=True jumps straight to the target (after a switch is flipped)."""
        if not self.state or self.sleeping:          # asleep/locked: LEDs stay off until wake
            return
        if check_night or force:                     # mic/camera and Night Shift: every 2 s
            busy = bool(self.busy_light and ambient.mic_or_camera_in_use())
            night = bool(self.night_calm and ambient.night_shift_active())
        else:
            busy, night = self.busy_on, self.night_on
        if busy != self.busy_on or night != self.night_on:
            self.busy_on, self.night_on = busy, night
            self.send_lighting()                     # red for a call / warm tint at night / your normal effect
            self.push()
            force = True
        if not (self.auto_bright or self.night_calm or self.busy_on or force):
            return
        target = 0 if self.light_mode == "off" and not self.busy_on else self.target_level()
        cur = max(self.state["brightness"].values())
        diff = target - cur
        # Dead band of 2 stops ±1 flicker from tiny sensor wobble; once fading, go all the way.
        if not force and (diff == 0 or (abs(diff) < 2 and not self.fading)):
            self.fading = False
            return
        step = diff if force else max(1, round(abs(diff) * 0.35)) * (1 if diff > 0 else -1)   # ease out
        self.fading = cur + step != target
        self.set_brightness(cur + step, list(razer.ZONES), remember=False, quiet=True)

    def on_remap(self, on):
        self.want_remap = bool(on)
        if not on:
            self.remap_on = False
            self.remapper.set_mapping({})           # tap stays installed but passes everything through
        else:
            self.start_remap(prompt=True)
        self.save()
        self.push()

    def on_openAccessibility(self):
        self.popover.performClose_(None)
        subprocess.run(["open", AX_URL])

    def on_button(self, name, action):
        if name not in remap.BUTTONS:
            return
        action = str(action).strip() or "default"
        try:
            remap.parse(action)
        except ValueError as e:
            self.js(f"buttonError({json.dumps(f'Could not understand that: {e}')})")
            return
        self.buttons = dict(self.buttons, **{name: action})
        if action != "default" and not self.want_remap:
            self.on_remap(True)                     # picking an action means you want it to work
            return
        if self.remap_on:
            self.remapper.set_mapping(self.buttons)
        self.save()
        self.push()

    def on_resetButtons(self):
        self.buttons = {name: "default" for name in remap.BUTTONS}
        self.remapper.set_mapping(self.buttons if self.remap_on else {})
        self.save()
        self.push()

    # --- mouse / state -----------------------------------------------------
    def save(self):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with self.save_lock:                         # main thread and worker both save
            with open(CONFIG + ".tmp", "w") as f:
                json.dump({"zone": self.zone, "lighting": self.lighting, "colors": self.colors,
                           "buttons": self.buttons, "remap_on": self.want_remap,
                           "saved_brightness": self.saved_brightness, "light_mode": self.light_mode,
                           "theme": self.theme, "sleep_off": self.sleep_off, "busy_light": self.busy_light,
                           "dpi_popup": self.dpi_popup}, f, indent=2)
            os.replace(CONFIG + ".tmp", CONFIG)      # atomic: a crash mid-write never leaves half a file

    def zones(self):
        return list(razer.ZONES) if self.zone == "both" else [self.zone]

    def conn(self):
        """The shared mouse connection (opening one takes ~35 ms, so keep it). Call with self.lock held."""
        if self._conn is None:
            self._conn = razer.Mouse()
        return self._conn

    def drop_conn(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def mouse(self, fn, quiet=False):
        """Run fn(mouse) on the shared connection; show a clear error instead of crashing."""
        try:
            with self.lock:
                try:
                    return fn(self.conn())
                except razer.RazerError:
                    self.drop_conn()                 # unplugged or stale: one fresh try
                    return fn(self.conn())
        except razer.RazerError as e:
            with self.lock:
                self.drop_conn()
            self.set_disconnected(str(e))
            if not quiet:
                self.toast(str(e))
        except ValueError as e:
            self.toast(str(e))

    def set_disconnected(self, why):
        self.state, self.error = None, why
        self.push()

    def refresh_sensor(self):
        """After a sensor change: read back DPI, stages and polling rate only (3 quick reads)."""
        got = self.mouse(lambda m: (m.dpi(), m.dpi_stages(), m.poll_rate()))
        if got and self.state:
            self.state = dict(self.state, dpi=got[0], dpi_stages=got[1], poll_rate=got[2])
        self.push()

    def refresh(self, quiet=False):
        state = self.mouse(lambda m: m.read_state(), quiet=quiet)
        if state:
            self.state, self.error = state, None
        self.push()

    def sync_loop(self):
        """Background: re-read DPI + stages so presses of the DPI button show up."""
        while True:
            time.sleep(SYNC_SECONDS if self.state else 1.0)   # look for a replugged mouse once a second
            try:
                with self.lock:
                    m = self.conn()
                    if self.state is None:
                        new = m.read_state()                      # (re)connected: read everything
                    else:
                        new = dict(self.state, dpi=m.dpi(), dpi_stages=m.dpi_stages())
            except razer.RazerError as e:
                with self.lock:
                    self.drop_conn()
                if self.state is not None:
                    self.run(lambda e=e: self.set_disconnected(str(e)))
                continue
            if new != self.state:
                self.run(lambda new=new: self.apply_synced(new))

    def apply_synced(self, new):
        old = self.state
        self.state, self.error = new, None
        changed = old and (old["dpi"] != new["dpi"] or old["dpi_stages"][0] != new["dpi_stages"][0])
        if changed and self.dpi_popup and time.time() - self.dpi_set_at > 3:   # the DPI button, not the app
            AppHelper.callAfter(self.show_hud, new["dpi"][0], *new["dpi_stages"])
        self.push()

    def apply_effect(self, effect, n, quiet=False):
        colors = self.colors[:n]
        sent = [ambient.warm(c) for c in colors] if self.night_on else colors
        if self.busy_on or self.sleeping:            # saved now, shown when the call or sleep ends
            ok = True
        else:
            ok = self.mouse(lambda m: [m.set_effect(z, effect, sent) for z in self.zones()], quiet) is not None
        if ok:
            for z in self.zones():
                self.lighting[z] = {"effect": effect, "colors": [list(c) for c in colors]}
            self.save()
        self.push()

    def send_lighting(self):
        """Show what should be on the LEDs now without changing your saved lighting: red during a call,
        otherwise each zone's own effect (warm-tinted at night)."""
        def fn(m):
            for z in razer.ZONES:
                if self.busy_on:
                    m.set_effect(z, "static", [BUSY_RED])
                else:
                    cur = self.lighting[z]
                    cols = [tuple(c) for c in cur.get("colors", [])]
                    m.set_effect(z, cur["effect"], [ambient.warm(c) for c in cols] if self.night_on else cols)
        self.mouse(fn, quiet=True)

    # --- sleep / lock ---------------------------------------------------------
    def on_power(self, what, value):
        """Main thread, from macOS notifications: what is 'screen' (asleep) or 'lock' (locked)."""
        if what == "screen":
            self.screen_asleep = value
        else:
            self.locked = value
        self.run(self.update_sleep)

    def update_sleep(self):
        want = self.sleep_off and (self.screen_asleep or self.locked)
        if want == self.sleeping or not self.state:
            return
        if want:
            self.pre_sleep = dict(self.state["brightness"])
            self.set_brightness(0, list(razer.ZONES), remember=False, quiet=True)
            self.sleeping = True
        else:
            self.sleeping = False
            before = self.pre_sleep or {}
            if self.mouse(lambda m: [m.set_brightness(z, lvl) for z, lvl in before.items()], quiet=True) is not None:
                self.state["brightness"].update(before)
            if self.auto_bright or self.night_calm or self.busy_light:
                self.follow_mac(force=True)          # catch up on a call, Night Shift or screen changes
        self.push()

    def on_sleepOff(self, on):
        self.sleep_off = bool(on)
        self.save()
        self.update_sleep()
        self.push()

    def on_busyLight(self, on):
        self.busy_light = bool(on)
        self.save()
        self.follow_mac(force=True)
        self.push()

    # --- DPI pop-up -------------------------------------------------------------
    def on_dpiPopup(self, on):
        self.dpi_popup = bool(on)
        self.save()
        if on:
            s = self.state or {}
            self.show_hud(s.get("dpi", (0,))[0], *s.get("dpi_stages", (1, [])))   # a preview
        self.push()

    def show_hud(self, dpi, active, stages):
        """Main thread: a small overlay like the volume pop-up, centred on the screen with the pointer."""
        if not dpi:
            return
        if self.hud is None:
            self.hud = hud.Hud()
        self.hud.show(dpi, active, len(stages))

    def set_colour(self, slot, rgb, quiet=False):
        self.colors[slot] = tuple(rgb)
        cur = self.lighting["logo" if self.zone == "both" else self.zone]
        eff, n = cur["effect"], len(cur.get("colors", []))
        if eff in ("spectrum", "none") or (eff == "breath" and n == 0):
            eff, n = "static", 1          # picking a colour implies a coloured effect
        self.apply_effect(eff, max(n, slot + 1) if eff == "breath" else 1, quiet)

    def set_brightness(self, level, zones, remember=True, quiet=False):
        if self.mouse(lambda m: [m.set_brightness(z, level) for z in zones], quiet) is not None and self.state:
            for z in zones:
                self.state["brightness"][z] = level
            if level and remember:
                self.saved_brightness = level
                self.save()
        self.push()

    def start_remap(self, prompt):
        """Start the event tap. Without Accessibility permission, optionally show macOS's own prompt
        (which also adds the app to the list); tick() keeps retrying until it's granted."""
        if self.remapper.start():
            self.remap_on = True
            self.remapper.set_mapping(self.buttons)
            self.push()
            return True
        self.remap_on = False
        if prompt:
            ask_accessibility()
        self.push()
        return False


if __name__ == "__main__":
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)   # menu bar only, no Dock icon
    controller = App()
    AppHelper.runEventLoop()
