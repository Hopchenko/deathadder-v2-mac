"""Tests that need no mouse, no USB and no windows. Run: .venv/bin/python -m unittest -v"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ambient  # noqa: E402
import app as A  # noqa: E402
import razer_da_v2 as razer  # noqa: E402
import remap  # noqa: E402
import Quartz as Q  # noqa: E402


class FakeMouse:
    def __init__(self, calls):
        self.calls = calls

    def set_brightness(self, zone, level):
        self.calls.append(("bright", zone, level))

    def set_effect(self, zone, effect, colors):
        self.calls.append(("effect", zone, effect, [tuple(c) for c in colors]))


def make_app(screen=0.5, night=False):
    """An App with its state set by hand: no UI, no USB, no threads, no saving."""
    a = A.App.__new__(A.App)
    a.calls = []
    a.state = {"brightness": {"logo": 100, "wheel": 100}}
    a.zone = "both"
    a.colors = [(0, 0, 255), (255, 0, 0)]
    a.lighting = {z: {"effect": "static", "colors": [[0, 0, 255]]} for z in razer.ZONES}
    a.saved_brightness = 200
    a.light_mode = "manual"
    a.night_on = False
    a.fading = False
    a.mouse = lambda fn, quiet=False: fn(FakeMouse(a.calls)) or True
    a.save = a.push = lambda: None
    a.run = lambda fn: fn()                                   # no worker thread in tests
    a.sleep_off, a.busy_light, a.dpi_popup = True, False, True
    a.sleeping = a.busy_on = a.screen_asleep = a.locked = False
    a.pre_sleep, a.dpi_set_at = None, 0.0
    a.screen, a.night, a.mic = [screen], [night], [False]
    ambient.screen_brightness = lambda: a.screen[0]
    ambient.night_shift_active = lambda: a.night[0]
    ambient.mic_or_camera_in_use = lambda: a.mic[0]
    return a


def level(a):
    return a.state["brightness"]["logo"]


def ticks(a, n):
    seen = []
    for _ in range(n):
        a.follow_mac(check_night=False)
        seen.append(level(a))
    return seen


class Brightness(unittest.TestCase):
    def test_manual_mode_leaves_leds_alone(self):
        a = make_app()
        ticks(a, 5)
        self.assertEqual(a.calls, [])

    def test_screen_mode_fades_to_target_in_small_steps(self):
        a = make_app(screen=0.5)
        a.light_mode = "screen"
        want = round(A.MIN_AUTO + 0.5 ** 2 * (255 - A.MIN_AUTO))
        seen = ticks(a, 30)
        self.assertEqual(seen[-1], want)
        jumps = [abs(y - x) for x, y in zip([100] + seen, seen)]
        self.assertLessEqual(max(jumps), round(abs(want - 100) * 0.35) + 1)
        self.assertGreaterEqual(seen.index(want), 4)          # a fade over several ticks, not one jump

    def test_sensor_wobble_sends_nothing(self):
        a = make_app(screen=0.5)
        a.light_mode = "screen"
        ticks(a, 30)
        n = len(a.calls)
        a.screen[0] = 0.502
        ticks(a, 10)
        self.assertEqual(len(a.calls), n)

    def test_dark_room_dims_but_never_goes_off(self):
        a = make_app(screen=0.0)
        a.light_mode = "screen"
        self.assertEqual(ticks(a, 40)[-1], A.MIN_AUTO)
        self.assertEqual(a.saved_brightness, 200)             # your own level is untouched

    def test_night_tints_and_dims_then_restores(self):
        a = make_app(night=True)
        a.light_mode = "night"
        a.follow_mac(check_night=True)
        effects = [c for c in a.calls if c[0] == "effect"]
        self.assertEqual(effects[-1][3], [ambient.warm((0, 0, 255))])
        self.assertEqual(level(a), round(200 * 0.6))
        self.assertEqual(a.lighting["logo"]["colors"], [[0, 0, 255]])   # saved colour unchanged
        a.night[0] = False
        a.follow_mac(check_night=True)
        self.assertEqual([c for c in a.calls if c[0] == "effect"][-1][3], [(0, 0, 255)])

    def test_slider_sets_daytime_level_at_night(self):
        a = make_app(night=True)
        a.on_mode("night")
        a.on_brightness(100)
        self.assertEqual((a.saved_brightness, level(a)), (100, 60))

    def test_modes(self):
        a = make_app()
        a.on_mode("off")
        self.assertEqual(level(a), 0)
        n = len(a.calls)
        ticks(a, 5)
        self.assertEqual(len(a.calls), n)                     # off stays off
        a.on_mode("manual")
        self.assertEqual(level(a), 200)
        a.on_mode("bogus")
        self.assertEqual(a.light_mode, "manual")


class BusyLight(unittest.TestCase):
    def effects(self, a):
        return [c for c in a.calls if c[0] == "effect"]

    def test_red_during_a_call_then_back(self):
        a = make_app()
        a.busy_light = True
        a.mic[0] = True
        a.follow_mac(check_night=True)
        self.assertEqual(self.effects(a)[-1][2:], ("static", [A.BUSY_RED]))
        self.assertEqual(level(a), A.BUSY_LEVEL)
        a.mic[0] = False
        a.follow_mac(check_night=True)
        self.assertEqual(self.effects(a)[-1][2:], ("static", [(0, 0, 255)]))   # your effect and colour again
        self.assertEqual(level(a), 200)
        self.assertEqual(a.lighting["logo"]["colors"], [[0, 0, 255]])        # never saved as red

    def test_off_unless_switched_on(self):
        a = make_app()
        a.mic[0] = True
        a.follow_mac(check_night=True)
        self.assertEqual(self.effects(a), [])

    def test_shows_even_in_off_mode_and_goes_dark_after(self):
        a = make_app()
        a.on_mode("off")
        a.busy_light, a.mic[0] = True, True
        a.follow_mac(check_night=True)
        self.assertEqual(level(a), A.BUSY_LEVEL)
        a.mic[0] = False
        a.follow_mac(check_night=True)
        self.assertEqual(level(a), 0)

    def test_changing_the_effect_during_a_call_waits_until_it_ends(self):
        a = make_app()
        a.busy_light, a.mic[0] = True, True
        a.follow_mac(check_night=True)
        n = len(self.effects(a))
        a.apply_effect("spectrum", 0)
        self.assertEqual(len(self.effects(a)), n)                              # still red
        self.assertEqual(a.lighting["logo"]["effect"], "spectrum")            # but saved
        a.mic[0] = False
        a.follow_mac(check_night=True)
        self.assertEqual(self.effects(a)[-1][2], "spectrum")


class Sleep(unittest.TestCase):
    def test_dark_on_sleep_and_exactly_restored_on_wake(self):
        a = make_app()
        a.state["brightness"] = {"logo": 150, "wheel": 40}                    # zones can differ
        a.on_power("screen", True)
        self.assertTrue(a.sleeping)
        self.assertEqual(a.state["brightness"], {"logo": 0, "wheel": 0})
        ticks(a, 5)
        self.assertEqual(a.state["brightness"], {"logo": 0, "wheel": 0})     # nothing wakes them early
        a.on_power("screen", False)
        self.assertEqual(a.state["brightness"], {"logo": 150, "wheel": 40})

    def test_lock_counts_until_unlocked(self):
        a = make_app()
        a.on_power("lock", True)
        a.on_power("screen", True)
        a.on_power("screen", False)                                           # screen wakes to the lock screen
        self.assertTrue(a.sleeping)
        a.on_power("lock", False)
        self.assertFalse(a.sleeping)
        self.assertEqual(level(a), 100)

    def test_switch_off_means_leds_stay_on(self):
        a = make_app()
        a.sleep_off = False
        a.on_power("screen", True)
        self.assertFalse(a.sleeping)
        self.assertEqual(level(a), 100)


class DpiPopup(unittest.TestCase):
    def setUp(self):
        self.shown = []
        self._orig = A.AppHelper.callAfter
        A.AppHelper.callAfter = lambda fn, *args: self.shown.append(args)

    def tearDown(self):
        A.AppHelper.callAfter = self._orig

    def app(self):
        a = make_app()
        a.state.update(dpi=(1600, 1600), dpi_stages=(3, [400, 800, 1600, 3200]))
        return a

    def test_shows_for_the_dpi_button(self):
        a = self.app()
        a.apply_synced(dict(a.state, dpi=(3200, 3200), dpi_stages=(4, [400, 800, 1600, 3200])))
        self.assertEqual(self.shown, [(3200, 4, [400, 800, 1600, 3200])])

    def test_not_for_changes_made_in_the_app(self):
        a = self.app()
        a.dpi_set_at = A.time.time()
        a.apply_synced(dict(a.state, dpi=(800, 800)))
        self.assertEqual(self.shown, [])

    def test_not_when_switched_off_or_unchanged(self):
        a = self.app()
        a.dpi_popup = False
        a.apply_synced(dict(a.state, dpi=(800, 800)))
        a.dpi_popup = True
        a.apply_synced(dict(a.state))
        self.assertEqual(self.shown, [])


class Settings(unittest.TestCase):
    def test_old_configs_migrate(self):
        self.assertEqual(A.light_mode_from({}), "manual")
        self.assertEqual(A.light_mode_from({"auto_brightness": True}), "screen")
        self.assertEqual(A.light_mode_from({"night_calm": True}), "night")
        self.assertEqual(A.light_mode_from({"light_mode": "off", "auto_brightness": True}), "off")
        self.assertEqual(A.light_mode_from({"light_mode": "nonsense"}), "manual")

    def test_saves_from_two_threads_never_corrupt_the_file(self):
        a = make_app()
        del a.save                                            # use the real save()
        a.save_lock = threading.Lock()
        a.buttons, a.want_remap, a.theme = {"back": "cmd+c"}, False, "system"
        with tempfile.TemporaryDirectory() as d:
            old, A.CONFIG = A.CONFIG, os.path.join(d, "config.json")
            try:
                threads = [threading.Thread(target=lambda: [a.save() for _ in range(50)]) for _ in range(4)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join()
                with open(A.CONFIG) as f:
                    self.assertEqual(json.load(f)["light_mode"], "manual")
                self.assertEqual(os.listdir(d), ["config.json"])  # no leftover temp file
            finally:
                A.CONFIG = old


class Threading(unittest.TestCase):
    def test_mouse_commands_go_to_the_worker_window_commands_do_not(self):
        a = A.App.__new__(A.App)
        queued, ran = [], []
        a.run = queued.append
        a.on_dpi = lambda dpi: ran.append(("dpi", dpi))
        a.on_theme = lambda theme: ran.append(("theme", theme))
        a.on_message({"cmd": "dpi", "dpi": 800})
        a.on_message({"cmd": "theme", "theme": "dark"})
        self.assertEqual(ran, [("theme", "dark")])            # main-thread command ran right away
        self.assertEqual(len(queued), 1)                      # mouse command was queued...
        queued[0]()
        self.assertEqual(ran[-1], ("dpi", 800))               # ...and does the right thing when run

    def test_unknown_commands_are_ignored(self):
        a = A.App.__new__(A.App)
        a.run = lambda fn: self.fail("should not queue")
        a.on_message({"cmd": "rm_rf"})


class Remap(unittest.TestCase):
    def test_parser(self):
        self.assertIsNone(remap.parse(""))
        self.assertIsNone(remap.parse("Default"))
        self.assertEqual(remap.parse("disabled"), [])
        self.assertEqual(remap.parse("cmd+shift+4"),
                         [("key", 21, Q.kCGEventFlagMaskCommand | Q.kCGEventFlagMaskShift)])
        self.assertEqual(remap.parse("cmd+c; wait 150; text hi"),
                         [("key", 8, Q.kCGEventFlagMaskCommand), ("wait", 0.15), ("text", "hi")])

    def test_parser_rejects_bad_input(self):
        for bad in ("cmd+nope", "hyper+a", "wait 99999"):
            with self.assertRaises(ValueError, msg=bad):
                remap.parse(bad)


class Mouse(unittest.TestCase):
    def test_report_layout_and_checksum(self):
        r = razer.build_report(0x04, 0x05, [1, 2, 3])
        self.assertEqual(len(r), 90)
        self.assertEqual((r[1], r[5], r[6], r[7]), (razer.TRANSACTION_ID, 3, 0x04, 0x05))
        crc = 0
        for b in r[2:88]:
            crc ^= b
        self.assertEqual(r[88], crc)


class Ambient(unittest.TestCase):
    def test_warm(self):
        self.assertEqual(ambient.warm((0, 0, 255)), (140, 77, 142))
        self.assertEqual(ambient.warm((255, 140, 50)), (255, 140, 50))


if __name__ == "__main__":
    unittest.main()
