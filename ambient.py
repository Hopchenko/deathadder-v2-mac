"""Read the Mac's built-in screen brightness and whether Night Shift is warming the screen right now.

Both use private Apple frameworks (no public API exists), so every call is wrapped: on failure they
return None and the app just leaves the LEDs alone.
"""
import ctypes

import objc
import Quartz as Q

_ds = None
_night = None


def _builtin_display():
    err, ids, n = Q.CGGetOnlineDisplayList(16, None, None)
    return next((d for d in ids[:n] if Q.CGDisplayIsBuiltin(d)), None)


def screen_brightness():
    """Built-in screen brightness 0.0-1.0 (macOS adjusts it to the room light), or None if unavailable."""
    global _ds
    try:
        if _ds is None:
            _ds = ctypes.cdll.LoadLibrary("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
            _ds.DisplayServicesGetBrightness.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_float)]
        d = _builtin_display()
        if d is None:
            return None
        v = ctypes.c_float(-1)
        if _ds.DisplayServicesGetBrightness(d, ctypes.byref(v)) != 0 or not 0 <= v.value <= 1:
            return None
        return v.value
    except (OSError, AttributeError):
        return None


def night_shift_active():
    """True while Night Shift is warming the screen, False when not, None if it can't be read."""
    global _night
    try:
        if _night is None:
            objc.loadBundle("CoreBrightness", {}, bundle_path="/System/Library/PrivateFrameworks/CoreBrightness.framework")
            client = objc.lookUpClass("CBBlueLightClient").alloc().init()
            lib = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
            lib.sel_registerName.restype = ctypes.c_void_p
            lib.sel_registerName.argtypes = [ctypes.c_char_p]
            send = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                ctypes.cast(lib.objc_msgSend, ctypes.c_void_p).value)
            _night = (client, lib.sel_registerName(b"getBlueLightStatus:"), send)   # keep client alive
        client, sel, send = _night
        buf = (ctypes.c_ubyte * 64)()        # struct { BOOL active; BOOL enabled; ... } — larger than needed
        if not send(objc.pyobjc_id(client), sel, ctypes.addressof(buf)):
            return None
        return bool(buf[0])
    except (objc.error, OSError, AttributeError):
        return None


def warm(rgb, amount=0.55):
    """Blend a colour toward a soft amber, like Night Shift does to the screen."""
    return tuple(round(c * (1 - amount) + w * amount) for c, w in zip(rgb, (255, 140, 50)))


if __name__ == "__main__":   # self-check
    assert warm((0, 0, 255)) == (140, 77, 142)
    assert warm((255, 140, 50)) == (255, 140, 50)
    b, n = screen_brightness(), night_shift_active()
    assert b is None or 0 <= b <= 1
    assert n in (None, True, False)
    print(f"ambient self-check ok (screen {b}, night shift {n})")
