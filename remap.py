"""
Software button remapping + macros (CGEventTap). Runs only while the app runs; needs Accessibility.
Onboard remapping for the DeathAdder V2 isn't documented in OpenRazer / razer-macos / OpenRGB, so
nothing here talks to the mouse.

Action syntax (per button):
    ""  or "default"          normal behaviour
    "disabled"                swallow the click
    "cmd+shift+4"             one shortcut
    "cmd+c; wait 150; cmd+v"  macro: steps separated by ';'
    "text hello world"        type text (as a macro step)
"""
import threading
import time

import Quartz as Q

BUTTONS = {"back": 3, "forward": 4, "middle": 2}   # kCGMouseEventButtonNumber
LABELS = {"back": "Side button (back)", "forward": "Side button (forward)", "middle": "Wheel click"}

MODS = {"cmd": Q.kCGEventFlagMaskCommand, "shift": Q.kCGEventFlagMaskShift,
        "alt": Q.kCGEventFlagMaskAlternate, "opt": Q.kCGEventFlagMaskAlternate,
        "ctrl": Q.kCGEventFlagMaskControl, "fn": Q.kCGEventFlagMaskSecondaryFn}
# macOS virtual key codes (ANSI layout, same as Carbon kVK_*).
KEYS = dict(zip("asdfhgzxcv", [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]))
KEYS.update(b=11, q=12, w=13, e=14, r=15, y=16, t=17, o=31, u=32, i=34, p=35, l=37, j=38, k=40, n=45, m=46)
KEYS.update({"1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27,
             "8": 28, "0": 29, "]": 30, "[": 33, "'": 39, ";": 41, "\\": 42, ",": 43, "/": 44, ".": 47, "`": 50,
             "return": 36, "enter": 36, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "esc": 53,
             "escape": 53, "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
             "left": 123, "right": 124, "down": 125, "up": 126,
             "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
             "f9": 101, "f10": 109, "f11": 103, "f12": 111})


def parse(action):
    """Action string -> list of steps: ("key", code, flags) | ("wait", seconds) | ("text", str).
    Returns None for default, [] for disabled. Raises ValueError on bad input."""
    action = (action or "").strip()
    if action.lower() in ("", "default"):
        return None
    if action.lower() == "disabled":
        return []
    steps = []
    for part in action.split(";"):
        part = part.strip()
        low = part.lower()
        if not part:
            continue
        if low.startswith("text "):
            steps.append(("text", part[5:]))
        elif low.startswith("wait "):
            ms = int(low[5:].strip().rstrip("ms"))
            if not 0 <= ms <= 10000:
                raise ValueError("wait must be 0-10000 ms")
            steps.append(("wait", ms / 1000))
        else:
            *mods, key = low.replace(" ", "").split("+")
            if key not in KEYS:
                raise ValueError(f"unknown key {key!r}")
            flags = 0
            for mname in mods:
                if mname not in MODS:
                    raise ValueError(f"unknown modifier {mname!r} (use cmd, shift, alt, ctrl, fn)")
                flags |= MODS[mname]
            steps.append(("key", KEYS[key], flags))
    return steps


def _play(steps):
    src = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
    for step in steps:
        if step[0] == "wait":
            time.sleep(step[1])
        elif step[0] == "key":
            for down in (True, False):
                e = Q.CGEventCreateKeyboardEvent(src, step[1], down)
                Q.CGEventSetFlags(e, step[2])
                Q.CGEventPost(Q.kCGHIDEventTap, e)
        else:
            for ch in step[1]:
                for down in (True, False):
                    e = Q.CGEventCreateKeyboardEvent(src, 0, down)
                    Q.CGEventKeyboardSetUnicodeString(e, len(ch), ch)
                    Q.CGEventPost(Q.kCGHIDEventTap, e)
        time.sleep(0.01)


class Remapper:
    def __init__(self):
        self.actions = {}      # button number -> parsed steps
        self.tap = None

    def set_mapping(self, mapping):
        """mapping: {"back": "cmd+[", ...}. Raises ValueError before changing anything."""
        parsed = {BUTTONS[name]: parse(act) for name, act in mapping.items()}
        self.actions = {b: s for b, s in parsed.items() if s is not None}

    def _callback(self, proxy, etype, event, refcon):
        if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            Q.CGEventTapEnable(self.tap, True)
            return event
        steps = self.actions.get(Q.CGEventGetIntegerValueField(event, Q.kCGMouseEventButtonNumber))
        if steps is None:
            return event
        if etype == Q.kCGEventOtherMouseDown and steps:
            threading.Thread(target=_play, args=(steps,), daemon=True).start()
        return None            # swallow both down and up of a remapped button

    def start(self):
        """Returns False if macOS refused the tap (Accessibility not granted)."""
        if self.tap:
            return True
        mask = Q.CGEventMaskBit(Q.kCGEventOtherMouseDown) | Q.CGEventMaskBit(Q.kCGEventOtherMouseUp)
        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                      Q.kCGEventTapOptionDefault, mask, self._callback, None)
        if not self.tap:
            return False
        src = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetMain(), src, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)
        return True


if __name__ == "__main__":   # self-check of the parser
    assert parse("") is None and parse("Default") is None and parse("disabled") == []
    assert parse("cmd+shift+4") == [("key", 21, Q.kCGEventFlagMaskCommand | Q.kCGEventFlagMaskShift)]
    assert parse("cmd+c; wait 150; text hi") == [("key", 8, Q.kCGEventFlagMaskCommand), ("wait", 0.15), ("text", "hi")]
    for bad in ("cmd+nope", "hyper+a", "wait 99999"):
        try:
            parse(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("remap parser self-check ok")
