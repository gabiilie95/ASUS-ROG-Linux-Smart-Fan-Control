"""Tests for rog_usbaudio.py against a fake sysfs tree. Run with python3 -B -m unittest discover -s service."""
import contextlib
import io
import lzma
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rog_usbaudio as ua  # noqa: E402

VOLT = "2b5a:0021"
OURS = VOLT + ":PLAYBACK_URB_FIXUP"
RELEASE = "7.3.4-300.fc45.x86_64"


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class Tree:
    """A laptop with the Volt 2 (audio + HID), a hub, and a USB audio driver that has the flag."""
    def __init__(self, root, module=b"\0GET_SAMPLE_RATE\0PLAYBACK_URB_FIXUP\0ALWAYS_SET_RATE\0"):
        self.root = Path(root)
        self.paths = ua.Paths(root)
        write(self.paths.param, ",".join(["(null)"] * 32))
        self.device("1-1.4", "17ef", "30b7", "USB2.0 Hub", {"1.0": "hub"})
        self.device("1-1.4.3", "2b5a", "0021", "Volt 2", {"1.0": "snd-usb-audio", "1.1": "snd-usb-audio",
                                                          "1.3": "usbhid"}, card=2)
        write(self.paths.asound / "card2" / "id", "V2")
        write(self.paths.asound / "card2" / "stream0", "Universal Audio Volt 2\n\nPlayback:\n  Status: Running\n")
        self.module(module)
        zone = self.root / "sys/class/thermal/thermal_zone2"
        write(zone / "type", "SEN3")
        write(zone / "temp", "81050")

    def device(self, path, vid, pid, name, interfaces, card=None):
        d = self.paths.usb / path
        for key, value in (("idVendor", vid), ("idProduct", pid), ("product", name), ("authorized", "1")):
            write(d / key, value + "\n")
        for itf, driver in interfaces.items():
            i = d / f"{path}:{itf}"
            i.mkdir(parents=True)
            target = self.root / "sys/bus/usb/drivers" / driver
            target.mkdir(parents=True, exist_ok=True)
            os.symlink(target, i / "driver")
        if card is not None:
            (d / f"{path}:1.0" / "sound" / f"card{card}").mkdir(parents=True)

    def module(self, data):
        base = self.paths.modules / RELEASE
        write(base / "modules.builtin", "kernel/drivers/usb/core/usbcore.ko\n")
        path = base / "kernel/sound/usb/snd-usb-audio.ko.xz"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(b"\x7fELF" + data * 3))

    def param(self):
        return ua.active_elements(self.paths.param.read_text())

    def set_param(self, text):
        self.paths.param.write_text(text)


class TreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = Tree(self.tmp.name)
        self.paths = self.tree.paths
        self.reconnects = []
        ua._support.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def reconnect(self, paths, device):
        self.reconnects.append((device, ua.flag_state(ua.entries(self.tree.param()), device)))

    def state(self):
        return ua.state(self.paths, RELEASE)


class ParamTest(unittest.TestCase):
    def test_elements_and_entries(self):
        self.assertEqual(ua.active_elements("(null),(null)"), [])
        self.assertEqual(ua.active_elements("a;b,,(null)"), ["a;b"], "the driver stops at the first empty element")
        self.assertEqual(ua.active_elements(None), [])
        self.assertEqual(ua.entries(["2b5a:0021:X;1234:5678:Y", "abcd:0001:Z"]),
                         ["2b5a:0021:X", "1234:5678:Y", "abcd:0001:Z"])
        with self.assertRaises(ua.Refused):
            ua.entries(["0x10"])           # the old per-card number form

    def test_flag_state_follows_the_driver(self):
        self.assertTrue(ua.flag_state([OURS], VOLT))
        self.assertTrue(ua.flag_state(["2B5A:21:playback_urb_fixup"], VOLT), "hex and names as the driver parses")
        self.assertTrue(ua.flag_state(["*:*:PLAYBACK_URB_FIXUP"], VOLT))
        self.assertFalse(ua.flag_state(["1234:5678:PLAYBACK_URB_FIXUP"], VOLT))
        self.assertFalse(ua.flag_state([OURS, "2b5a:0021:!PLAYBACK_URB_FIXUP"], VOLT), "later entries win")
        self.assertFalse(ua.flag_state(["2b5a:0021:80000|PLAYBACK_URB_FIXUP"], VOLT), "a number ends the entry")
        self.assertFalse(ua.flag_state(["garbage", OURS], VOLT), "a malformed entry ends the list")
        self.assertFalse(ua.flag_state([OURS], VOLT, skip=(OURS.lower(),)))

    def test_updated_and_param_text(self):
        others = ["1234:5678:GET_SAMPLE_RATE"]
        self.assertEqual(ua.updated(others, VOLT, True), others + [OURS])
        self.assertEqual(ua.updated(others + [OURS.lower()], VOLT, False), others, "removed whatever its case")
        self.assertEqual(ua.updated(others + [OURS], VOLT, True), others + [OURS], "never added twice")
        self.assertEqual(ua.param_text([OURS]), OURS + ",")
        self.assertEqual(ua.param_text([]), ",", "an empty first element clears the list")
        with self.assertRaises(ValueError):
            ua.check_device("2b5a-0021")
        self.assertEqual(ua.check_device("2B5A:0021"), VOLT)


class KernelTest(TreeTest):
    def test_support(self):
        support, text = ua.kernel_support(self.paths, RELEASE)
        self.assertTrue(support)
        self.tree.module(b"\0QUIRK_TYPE_PLAYBACK_URB_FIXUP\0MIXER_GET_CUR_OK\0")
        ua._support.clear()
        support, text = ua.kernel_support(self.paths, RELEASE)
        self.assertIs(support, False, "only the flag's own name counts")
        self.assertIn("Linux 7.3", text)
        write(self.paths.modules / RELEASE / "modules.builtin", "kernel/sound/usb/snd-usb-audio.ko\n")
        self.assertIsNone(ua.kernel_support(self.paths, RELEASE)[0])
        self.assertIsNone(ua.kernel_support(self.paths, "6.0.0")[0], "no module tree for this kernel")
        bad = self.paths.modules / RELEASE / "kernel/sound/usb/snd-usb-audio.ko.xz"
        write(self.paths.modules / RELEASE / "modules.builtin", "")
        bad.write_bytes(b"not xz")
        self.assertIsNone(ua.kernel_support(self.paths, RELEASE)[0])

    def test_devices(self):
        found = ua.devices(self.paths)
        self.assertEqual(found, [dict(device=VOLT, path="1-1.4.3", name="Volt 2", card="V2", playback=True)],
                         "the hub is not an audio device; HID interfaces do not count")
        self.assertEqual(ua.board_c(self.paths), 81.05)

    def test_conflicts(self):
        self.assertEqual(ua.conflicts(self.paths), [])
        write(self.paths.conf, "options snd_usb_audio quirk_flags=%s\n" % OURS)
        self.assertEqual(ua.conflicts(self.paths), [], "this program's own file is not a conflict")
        other = self.tree.root / "usr/lib/modprobe.d/audio.conf"
        write(other, "# comment\noptions snd-usb-audio index=1 quirk_flags=1234:5678:DSD_RAW\n")
        write(self.tree.root / "etc/modprobe.d/unrelated.conf", "options snd_hda_intel power_save=0\n")
        write(self.paths.cmdline, "quiet snd_usb_audio.quirk_flags=x\n")
        self.assertEqual(ua.conflicts(self.paths), [str(other), "the kernel command line"])


class ActTest(TreeTest):
    def act(self, action):
        return ua.act(action, VOLT, self.paths, out=lambda _m: None, reconnect=self.reconnect)

    def test_try_keep_off(self):
        self.tree.set_param("1234:5678:GET_SAMPLE_RATE,(null)")
        with mock.patch.object(ua.os, "uname", return_value=os.uname_result(("", "", RELEASE, "", ""))):
            self.act("try")
            self.assertEqual(self.tree.param(), ["1234:5678:GET_SAMPLE_RATE;" + OURS], "others are kept")
            self.assertEqual(self.reconnects, [(VOLT, True)], "reconnected after the flag was set")
            self.assertFalse(self.paths.conf.exists(), "try changes nothing at start")
            self.act("keep")
            text = self.paths.conf.read_text()
            self.assertIn("options snd_usb_audio quirk_flags=%s\n" % OURS, text)
            self.assertEqual(self.paths.conf.stat().st_mode & 0o777, 0o644)
            ds = ua.device_state(self.state(), VOLT)
            self.assertEqual((ds["on"], ds["ours"], ds["kept"], ds["other"]), (True, True, True, False))
            message = self.act("off")
            self.assertFalse(self.paths.conf.exists())
            self.assertEqual(self.tree.param(), ["1234:5678:GET_SAMPLE_RATE"])
            self.assertEqual(self.reconnects[-1], (VOLT, False))
            self.assertNotIn("Another", message)

    def test_keep_without_the_device_or_driver(self):
        self.paths.param.unlink()
        with mock.patch.object(ua.os, "uname", return_value=os.uname_result(("", "", RELEASE, "", ""))):
            message = self.act("keep")
            self.assertIn("applies when the device is connected", message)
            self.assertEqual(self.reconnects, [])
            with self.assertRaises(ua.Refused):
                self.act("try")

    def test_refusals(self):
        uname = os.uname_result(("", "", RELEASE, "", ""))
        with mock.patch.object(ua.os, "uname", return_value=uname):
            write(self.tree.root / "etc/modprobe.d/audio.conf", "options snd_usb_audio quirk_flags=1:2:X\n")
            with self.assertRaisesRegex(ua.Refused, "audio.conf"):
                self.act("keep")
            self.assertFalse(self.paths.conf.exists(), "nothing written when refused")
            self.tree.set_param("*:*:PLAYBACK_URB_FIXUP")
            self.assertIn("Another quirk_flags entry", self.act("off"))
            self.tree.set_param("0x4,(null)")
            with self.assertRaisesRegex(ua.Refused, "number"):
                self.act("try")
            self.tree.module(b"\0MIXER_GET_CUR_OK\0")
            ua._support.clear()
            self.tree.set_param("(null)")
            with self.assertRaisesRegex(ua.Refused, "Linux 7.3"):
                self.act("try")

    def test_reattach(self):
        times = iter(range(1000))
        ua.reattach(self.paths, VOLT, sleep=lambda _s: None, clock=lambda: next(times))
        self.assertEqual((self.paths.usb / "1-1.4.3" / "authorized").read_text(), "1")
        for driver in (self.paths.usb / "1-1.4.3").glob("1-1.4.3:*/driver"):
            driver.unlink()
        with self.assertRaisesRegex(ua.Refused, "Volt 2 did not come back"):
            ua.reattach(self.paths, VOLT, sleep=lambda _s: None, clock=lambda: next(times))
        with self.assertRaisesRegex(ua.Refused, "not connected"):
            ua.reattach(self.paths, "1111:2222", sleep=lambda _s: None)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class FakeStop:
    """threading.Event on the fake clock: wait advances time; optionally sets itself at a time."""
    def __init__(self, clock, at=None):
        self.clock, self.at, self.flag = clock, at, False

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True

    def wait(self, seconds):
        self.clock.t += seconds
        if self.at is not None and self.clock.t >= self.at:
            self.flag = True
        return self.flag


class FakeMonitor:
    """8000 packets/s at high speed; skips depend on whether the flag is set right now."""
    def __init__(self, tree, clock, lost=(150, 50), events=(20, 20), playing=(True, True)):
        self.tree, self.clock, self.lost, self.events, self.playing = tree, clock, lost, events, playing
        self.last = 0.0

    def snapshot(self, now):
        dt, self.last = now - self.last, now
        on = ua.flag_state(ua.entries(self.tree.param()), VOLT)
        if not self.playing[on]:
            return dict(devices=[], error=None)
        return dict(error=None, devices=[dict(path="1-1.4.3", packets=int(8000 * dt), skipped=int(self.lost[on] * dt),
                                              warmup_skipped=0, events=int(self.events[on] * dt)),
                                         dict(path="1-1.4.4.4", packets=999, skipped=999, events=999)])


class RunTest(TreeTest):
    def run_test(self, monitor_kw=None, stop_at=None):
        self.clock = Clock()
        self.events = []
        stop = FakeStop(self.clock, stop_at)
        monitor = FakeMonitor(self.tree, self.clock, **(monitor_kw or {}))
        with mock.patch.object(ua.os, "uname", return_value=os.uname_result(("", "", RELEASE, "", ""))):
            return ua.run_test(VOLT, 60, 10, monitor, self.events.append, stop, self.paths, clock=self.clock,
                               reconnect=self.reconnect)

    def kinds(self):
        return [e["kind"] for e in self.events if e["kind"] != "progress"]

    def test_helps(self):
        result = self.run_test()
        self.assertEqual(self.kinds(), ["start", "phase-start", "phase", "phase-start", "phase", "phase-start",
                                        "phase", "result"])
        self.assertEqual([p["fix"] for p in result["phases"]], [False, True, False])
        self.assertEqual([p["seconds"] for p in result["phases"]], [60, 120, 60], "twice as long with the fix")
        self.assertEqual(result["verdict"], "helps")
        self.assertEqual(result["off"]["lost_ms_per_min"], 150 * 0.125 * 60)
        self.assertEqual(result["on"]["ms_per_event"], round(50 * 0.125 / 20, 2))
        self.assertAlmostEqual(result["off"]["board_c"], 81.05, delta=0.06)
        self.assertIn("ms per minute (−67 %)", result["text"])
        self.assertEqual(self.reconnects, [(VOLT, True), (VOLT, False)], "on for the middle phase, then off")
        self.assertEqual(self.tree.param(), [], "left as it was")
        progress = [e for e in self.events if e["kind"] == "progress"]
        self.assertTrue(progress and all(e["elapsed"] <= e["total_s"] for e in progress))
        self.assertEqual(self.events[0]["total_s"], 60 + 120 + 60 + 30)

    def test_cancel_restores(self):
        self.assertIsNone(self.run_test(stop_at=100))
        self.assertEqual(self.kinds()[-1], "cancelled")
        self.assertEqual(self.reconnects, [(VOLT, True), (VOLT, False)])
        self.assertEqual(self.tree.param(), [])

    def test_original_on_is_restored(self):
        self.tree.set_param(OURS + ",")
        result = self.run_test()
        self.assertTrue(result["restored"])
        self.assertEqual(self.reconnects, [(VOLT, False), (VOLT, True), (VOLT, False), (VOLT, True)])
        self.assertEqual(self.tree.param(), [OURS])

    def test_refuses(self):
        self.tree.set_param("*:*:PLAYBACK_URB_FIXUP")
        with self.assertRaisesRegex(ua.Refused, "Another quirk_flags entry"):
            self.run_test()
        self.tree.set_param("(null)")
        (self.paths.usb / "1-1.4.3" / "idProduct").write_text("9999\n")
        with self.assertRaisesRegex(ua.Refused, "not connected"):
            self.run_test()

    def test_verdicts(self):
        self.assertEqual(self.run_test(dict(lost=(150, 260)))["verdict"], "worse")
        self.assertEqual(self.run_test(dict(lost=(150, 140)))["verdict"], "same")
        few = self.run_test(dict(events=(0.1, 0.1)))
        self.assertEqual(few["verdict"], "inconclusive")
        self.assertIn("too few", few["text"])
        self.assertEqual(self.run_test(dict(playing=(True, False)))["verdict"], "no-audio")
        gone = self.run_test(dict(lost=(0, 0), events=(0, 0)))
        self.assertEqual(gone["verdict"], "inconclusive", "no dropouts at all: nothing to compare")
        cured = self.run_test(dict(lost=(150, 0), events=(20, 0)))
        self.assertEqual(cured["verdict"], "helps")
        self.assertIn("0 ms each", cured["text"])

    def test_audio_stopping_is_inconclusive(self):
        rows = [dict(fix=False, seconds=60, packets=480000, lost=9000, events=1200, board_c=80),
                dict(fix=True, seconds=120, packets=400000, lost=3000, events=2400, board_c=81),
                dict(fix=False, seconds=60, packets=480000, lost=9000, events=1200, board_c=82)]
        v = ua.verdict(rows)
        self.assertEqual(v["verdict"], "inconclusive")
        self.assertIn("stopped", v["text"])
        self.assertEqual(v["off"]["board_c"], 81.0)


class MainTest(unittest.TestCase):
    def test_needs_root_and_a_device(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            ua.main(["try", VOLT])
        self.assertIn("needs root", err.getvalue())


if __name__ == "__main__":
    unittest.main()
