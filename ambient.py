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


_U32, _P = ctypes.c_uint32, ctypes.c_void_p


class _Addr(ctypes.Structure):          # AudioObjectPropertyAddress / CMIOObjectPropertyAddress
    _fields_ = [("selector", _U32), ("scope", _U32), ("element", _U32)]


def _fcc(s):
    return int.from_bytes(s.encode(), "big")


_av = None


def _av_libs():
    global _av
    if _av is None:
        ap = ctypes.POINTER(_Addr)
        ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        ca.AudioObjectGetPropertyDataSize.argtypes = [_U32, ap, _U32, _P, ctypes.POINTER(_U32)]
        ca.AudioObjectGetPropertyData.argtypes = [_U32, ap, _U32, _P, ctypes.POINTER(_U32), _P]
        cm = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO")
        cm.CMIOObjectGetPropertyDataSize.argtypes = [_U32, ap, _U32, _P, ctypes.POINTER(_U32)]
        # CMIO's getter takes an extra "bytes used" out-argument compared to CoreAudio's
        cm.CMIOObjectGetPropertyData.argtypes = [_U32, ap, _U32, _P, _U32, ctypes.POINTER(_U32), _P]
        _av = ca, cm
    return _av


def _ids(getsize, getdata, cmio):
    a, sz = _Addr(_fcc("dev#"), _fcc("glob"), 0), _U32()
    if getsize(1, ctypes.byref(a), 0, None, ctypes.byref(sz)) != 0 or not sz.value:   # 1 = system object
        return []
    ids, used = (_U32 * (sz.value // 4))(), _U32()
    ok = (getdata(1, ctypes.byref(a), 0, None, sz.value, ctypes.byref(used), ids) if cmio
          else getdata(1, ctypes.byref(a), 0, None, ctypes.byref(sz), ids)) == 0
    return list(ids) if ok else []


def _running(getdata, dev, cmio):
    r, v, used = _Addr(_fcc("gone"), _fcc("glob"), 0), _U32(), _U32(4)   # "...IsRunningSomewhere"
    ok = (getdata(dev, ctypes.byref(r), 0, None, 4, ctypes.byref(used), ctypes.byref(v)) if cmio
          else getdata(dev, ctypes.byref(r), 0, None, ctypes.byref(used), ctypes.byref(v))) == 0
    return ok and bool(v.value)


def mic_or_camera_in_use():
    """True while any app records from a microphone or camera (a call, a recording). No permission needed:
    macOS only says *whether* a device is busy, not what it hears or sees. None if it can't be read."""
    try:
        ca, cm = _av_libs()
        for d in _ids(ca.AudioObjectGetPropertyDataSize, ca.AudioObjectGetPropertyData, False):
            s, sz = _Addr(_fcc("stm#"), _fcc("inpt"), 0), _U32()
            if ca.AudioObjectGetPropertyDataSize(d, ctypes.byref(s), 0, None, ctypes.byref(sz)) == 0 and sz.value \
                    and _running(ca.AudioObjectGetPropertyData, d, False):   # only devices with inputs (not speakers)
                return True
        return any(_running(cm.CMIOObjectGetPropertyData, d, True)
                   for d in _ids(cm.CMIOObjectGetPropertyDataSize, cm.CMIOObjectGetPropertyData, True))
    except (OSError, AttributeError):
        return None


def warm(rgb, amount=0.55):
    """Blend a colour toward a soft amber, like Night Shift does to the screen."""
    return tuple(round(c * (1 - amount) + w * amount) for c, w in zip(rgb, (255, 140, 50)))


if __name__ == "__main__":   # self-check
    assert warm((0, 0, 255)) == (140, 77, 142)
    assert warm((255, 140, 50)) == (255, 140, 50)
    b, n, busy = screen_brightness(), night_shift_active(), mic_or_camera_in_use()
    assert b is None or 0 <= b <= 1
    assert n in (None, True, False) and busy in (None, True, False)
    print(f"ambient self-check ok (screen {b}, night shift {n}, mic/camera in use {busy})")
