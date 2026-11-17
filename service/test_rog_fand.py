"""Run with python3 -B -m unittest discover -s service. Hardware writes are mocked."""
import contextlib
import gzip
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import rog_fand as fand
import rog_sensors as sensors

ROOT = HERE.parent
CFG = fand.config()
BASE = CFG["base_curve"]


class PolicyTest(unittest.TestCase):
    def test_table_edges(self):
        for watts, expected in [(0, (0, 0, 0)), (49.99, (0, 0, 0)), (50, (35, 40, 70)),
                                (109.99, (35, 40, 70)), (110, (60, 60, 75)),
                                (139.99, (60, 60, 75)), (140, (80, 80, 80))]:
            with self.subTest(watts=watts):
                # CPU contributes to total heat, but never selects the GPU bin.
                p = fand.Policy(CFG)
                self.assertEqual(p.step(0, watts, 100)[0], expected)
                self.assertEqual(p.heat, watts + 100)

    def test_smoothing_and_missing_heat(self):
        p = fand.Policy(CFG)
        p.step(0, 40, 10)
        p.step(6, 100, 20)
        self.assertAlmostEqual(p.gpu, 77.9272, places=3)
        self.assertAlmostEqual(p.heat, 94.2484, places=3)
        p.step(12, 40, None)
        self.assertGreater(p.gpu, 74)
        self.assertIsNone(p.heat)

    def test_consecutive_boost_and_margin_cap(self):
        p = fand.Policy(CFG)
        def bad(t):
            return p.step(t, 80, 10, active=True, traffic=True, drops=5)
        self.assertIsNone(bad(0)[1])
        p.step(2, 80, 10, active=False, drops=10)
        self.assertIsNone(bad(4)[1])
        floors, event = bad(6)
        self.assertIsNotNone(event)
        self.assertEqual(floors, (45, 50, 90))
        self.assertEqual(p.margins, {"0": 0, "50": 5, "110": 0, "140": 0})
        for t in range(8, 50, 2):
            floors, _ = bad(t)
        self.assertEqual(p.margins["50"], 20)
        self.assertEqual(floors, (65, 70, 100))

    def test_decay_requires_clean_active_time_in_same_bin(self):
        p = fand.Policy(CFG, {"50": 5, "140": 10})
        def clean(t):
            p.step(t, 80, 10, active=True, traffic=True)
        clean(0)
        clean(1799)
        self.assertEqual(p.margins["50"], 5)
        clean(1800)
        self.assertEqual(p.margins["50"], 2.5)
        p.step(1802, 80, 10, active=True, traffic=False)
        clean(3600)
        self.assertEqual(p.margins["50"], 2.5)
        clean(5400)
        clean(7200)
        self.assertEqual(p.margins["50"], 0)
        self.assertEqual(p.margins["140"], 10)

    def test_ramp_per_fan(self):
        ramp = fand.Ramp()
        def step(t, target):
            return ramp.step(target, t, CFG)
        self.assertEqual(step(0, (80, 80, 80)), (80, 80, 80))
        step(2, (35, 40, 70))
        self.assertEqual(step(61, (83, 40, 90)), (80, 80, 90))
        self.assertEqual(step(62, (35, 40, 70)), (80, 75, 90))
        self.assertEqual(step(81, (35, 40, 70)), (80, 75, 90))
        self.assertEqual(step(82, (35, 40, 70)), (80, 70, 90))
        self.assertEqual(step(122, (35, 40, 70)), (75, 65, 85))

    def test_degradation_and_manual_modes(self):
        p = fand.Policy(CFG)
        self.assertEqual(p.step(0, None, None, degraded=True)[0], (60, 60, 80))
        self.assertEqual(p.step(2, 150, None, degraded=True)[0], (80, 80, 80))
        self.assertEqual(p.step(4, 150, None, mode="off")[0], fand.ZERO)
        self.assertEqual(p.step(6, 150, None, mode="preset:light")[0], (35, 40, 70))
        self.assertEqual(p.step(8, 150, None, controlled=False)[0], fand.ZERO)


class CurveTest(unittest.TestCase):
    def test_exact_base_from_balanced_script_and_pointwise_max(self):
        original = (ROOT / "tools/fans-balanced.sh").read_text()
        curves = fand.build_curves(BASE, (35, 40, 70))
        for fan, pwm in zip(fand.FANS, (90, 102, 179)):
            data = re.search(fr'--fan {fan} --data "([^"]+)"', original).group(1)
            expected = [tuple(map(int, p.split("c:"))) for p in data.split(",")]
            self.assertEqual(fand.build_curves(BASE, fand.ZERO)[fan], expected)
            self.assertEqual(curves[fan], [(t, max(v, pwm)) for t, v in expected])
        self.assertTrue(all(c[-1][1] == 255 for c in curves.values()))

    def test_invalid_inputs(self):
        for points in [BASE["cpu"][:-1], [[40,20]] * 8,
                       [[29,1], *BASE["cpu"][1:]], [[40,99], *BASE["cpu"][1:]],
                       [*BASE["cpu"][:-1], [88,254]], [[40,False], *BASE["cpu"][1:]]]:
            with self.subTest(points=points), self.assertRaises(ValueError):
                fand.build_curves({**BASE, "cpu": points}, fand.ZERO)
        for floors in [(0, 0), (1, 2, 101), (False, 1, 1), (float("nan"), 0, 0)]:
            with self.assertRaises(ValueError):
                fand.build_curves(BASE, floors)

    def test_mode_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mode"
            for mode in fand.MODES:
                fand.atomic_write(path, mode + "\n")
                self.assertEqual(fand.read_mode(path), mode)
            for raw in (" auto", "OFF", "preset:custom", "auto\noff", "off\n\n", "auto"*100):
                fand.atomic_write(path, raw)
                with self.assertRaises(ValueError):
                    fand.read_mode(path)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                fand.read_mode(path)
            path.unlink()
            path.symlink_to("/dev/null")
            with self.assertRaises(OSError):
                fand.read_mode(path)


class SensorTest(unittest.TestCase):
    def test_reference_capture(self):
        counts = sensors.UsbCounts()
        with gzip.open(ROOT / "data/fixtures/usbmon-volt-isoc-out-completions-30s.txt.gz", "rt") as src:
            for line in src:
                counts.add(sensors.parse_usbmon(line), 0)
        self.assertEqual(dict(counts.interval), {(1, 12): [239990, 22718, 2522]})

    def test_packet_filter_and_tail_estimate(self):
        line = "ab 25 C Zo:1:003:1 0:1:8:0 7 0:0:48 -18:48:0 0:96:48 0:144:48 -18:192:0 336 >"
        self.assertEqual(sensors.parse_usbmon(line), ((1, 3), 25, 7, 4))
        for bad in ("", line.replace("C Zo", "S Zo"), line.replace("Zo", "Zi"), line[:30]):
            self.assertIsNone(sensors.parse_usbmon(bad))
        self.assertEqual(sensors.parse_usbmon(line.replace("-18:192:0", "-18:192:48"))[-1], 4)

    def test_intervals_device_names_and_audio_expiry(self):
        u = sensors.UsbMonitor(threading.Event())
        u.error, u.sample_at = None, 20
        u.counts.add(((1,3), 1000, 10, 4), 21)
        u.counts.add(((2,3), 1100, 7, 0), 21)
        with patch.object(sensors, "usb_names", return_value={(1,3): ("1-2", "DAC")}):
            snapshot = u.snapshot(22)
        self.assertTrue(snapshot["audio_active"])
        self.assertEqual(snapshot["devices"][0]["name"], "DAC")
        self.assertEqual(snapshot["devices"][0]["drops_per_s"], 2)
        self.assertEqual(len(snapshot["devices"]), 2)
        self.assertEqual(u.snapshot(24)["devices"], [])
        self.assertTrue(u.snapshot(26)["audio_active"])
        self.assertFalse(u.snapshot(26.1)["audio_active"])

    def test_rapl_wrap_failure_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = sensors.Rapl()
            r.zone = Path(tmp)
            fand.atomic_write(r.zone / "max_energy_range_uj", "100000000")
            def sample(now, value):
                fand.atomic_write(r.zone / "energy_uj", str(value))
                return r.watts(now)
            self.assertIsNone(sample(1, 90000000))
            self.assertEqual(sample(3, 30000000), 20)
            self.assertIsNone(sample(5, "bad"))
            self.assertIsNone(sample(7, 40000000))
            self.assertEqual(sample(9, 60000000), 10)


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for attr in ("STATE", "RUN", "ETC", "LOG"):
            stack.enter_context(patch.object(fand, attr, self.path))
        self.profile = stack.enter_context(patch.object(fand, "read", return_value="performance"))
        self.command = stack.enter_context(patch.object(subprocess, "run"))
        self.actuator = fand.Actuator()

    def test_partial_write_profile_change_and_stop_post_recovery(self):
        self.profile.side_effect = ["performance", "quiet"]
        self.assertFalse(self.actuator.apply(BASE, (80, 80, 80)))
        self.assertEqual(self.command.call_count, 1)
        self.profile.side_effect = None
        self.profile.return_value = "balanced"
        self.assertFalse(fand.Actuator().restore())
        self.assertEqual(self.command.call_count, 1)
        self.assertTrue(self.actuator.receipt.exists())
        self.profile.return_value = "performance"
        self.assertTrue(fand.Actuator().restore())
        self.assertFalse(self.actuator.receipt.exists())
        for fan, call in zip(fand.FANS, self.command.call_args_list[1:4]):
            self.assertEqual(call.args[0], ["/usr/bin/asusctl", "fan-curve", "--mod-profile",
                "performance", "--fan", fan, "--data", ",".join(f"{t}c:{p}" for t,p in BASE[fan])])
        self.assertTrue(fand.Actuator().restore())
        self.assertEqual(self.command.call_count, 5)

    def test_no_duplicate_writes_and_off(self):
        self.actuator.apply(BASE, (80, 80, 80))
        self.actuator.apply(BASE, (80, 80, 80))
        self.assertEqual(self.command.call_count, 4)
        self.actuator.apply(BASE, (80, 80, 85))
        self.assertEqual(self.command.call_count, 6)
        self.actuator.apply(BASE, fand.ZERO)
        self.assertEqual(self.command.call_count, 10)
        self.assertFalse(self.actuator.receipt.exists())

    def test_failed_restore_remains_retryable(self):
        self.actuator.apply(BASE, (80, 80, 80))
        self.command.side_effect = [OSError("asusd unavailable"), None, None, None]
        with self.assertRaises(RuntimeError):
            self.actuator.restore()
        self.assertTrue(self.actuator.receipt.exists())
        self.command.side_effect = None
        self.assertTrue(self.actuator.restore())

    def test_live_loop_restores_on_error_and_persists_margin(self):
        # Simulate three samples, then a failed sensor read. All paths and hardware are fakes.
        sample = dict(cpu_w=20, gpu_w=70, gpu_c=60, cpu_c=55, board_c=70, profile="performance")
        usb = dict(audio_active=True, devices=[dict(skipped=2)], error=None)
        with contextlib.ExitStack() as stack:
            sensor = stack.enter_context(patch.object(fand, "Sensors")).return_value
            sensor.sample.side_effect = [sample, sample, sample, RuntimeError("sensor error")]
            monitor = stack.enter_context(patch.object(fand, "UsbMonitor")).return_value
            monitor.snapshot.return_value = usb
            stack.enter_context(patch.object(fand, "config", return_value=CFG))
            stack.enter_context(patch.object(fand.signal, "signal"))
            stack.enter_context(patch.object(threading.Event, "wait", return_value=False))
            with self.assertRaisesRegex(RuntimeError, "sensor error"):
                fand.run_service(False, None)
        state = fand.read_json(self.path / "state.json")
        self.assertEqual(fand.Policy(CFG, state["margins"]).margins["50"], 5)
        status = self.path / "status.json"
        self.assertEqual(status.stat().st_mode & 0o777, 0o644)
        self.assertEqual(fand.read_json(status)["floors"]["mid"], 90)
        self.assertEqual(fand.read_json(status)["table"], CFG["table"])
        self.assertFalse(self.actuator.receipt.exists())
        self.assertEqual(self.command.call_args_list[-4].args[0][-1],
                         ",".join(f"{t}c:{p}" for t,p in BASE["cpu"]))

    def test_mode_restart_preserves_off_for_configured_owner(self):
        import pwd
        owner = pwd.getpwuid(os.geteuid())
        path = self.path / "mode"
        fand.atomic_write(path, "off\n")
        fand.init_mode({**CFG, "control_user": owner.pw_name})
        self.assertEqual(path.read_text(), "off\n")
        control = self.path / "control"
        fand.atomic_write(control / "mode", "preset:heavy\n")
        self.assertEqual(fand.read_mode(fand.init_mode({"control_user": owner.pw_name})), "preset:heavy")
        self.assertEqual(control.stat().st_uid, owner.pw_uid)
        self.assertEqual(path.stat().st_uid, owner.pw_uid)

    def test_live_rejects_invalid_mode_and_suspends_when_inactive(self):
        sample = dict(cpu_w=20, gpu_w=70, profile="performance")
        records = []
        def capture_status(path, text, *args, **kwargs):
            if path.name == "status.json":
                records.append(json.loads(text))
            original_write(path, text, *args, **kwargs)
        original_write = fand.atomic_write
        with contextlib.ExitStack() as stack:
            sensor = stack.enter_context(patch.object(fand, "Sensors")).return_value
            sensor.sample.side_effect = [sample, sample, {**sample, "profile": "quiet"},
                                         RuntimeError("end of simulation")]
            monitor = stack.enter_context(patch.object(fand, "UsbMonitor")).return_value
            monitor.snapshot.return_value = dict(audio_active=False, devices=[], error=None)
            stack.enter_context(patch.object(fand, "config", return_value=CFG))
            stack.enter_context(patch.object(fand.signal, "signal"))
            stack.enter_context(patch.object(fand, "atomic_write", side_effect=capture_status))
            stack.enter_context(patch.object(fand, "read_mode", side_effect=[
                "preset:heavy", ValueError("invalid"), "off"]))
            stack.enter_context(patch.object(threading.Event, "wait", return_value=False))
            self.profile.side_effect = ["performance"] * 4 + ["quiet"]
            with self.assertRaisesRegex(RuntimeError, "end of simulation"):
                fand.run_service(False, None)
        self.assertEqual(self.command.call_count, 4)
        self.assertEqual(records[1]["mode"], "preset:heavy")
        self.assertTrue(records[1]["invalid_mode"])
        self.assertFalse(records[2]["applied"])
        self.assertTrue(self.actuator.receipt.exists())

    def test_dry_run_has_no_writers_or_usb_thread_as_user(self):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(os, "geteuid", return_value=1000))
            stack.enter_context(patch.object(fand.signal, "signal"))
            stack.enter_context(patch.object(threading.Event, "wait", return_value=False))
            writer = stack.enter_context(patch.object(fand, "atomic_write"))
            thread = stack.enter_context(patch.object(threading.Thread, "start"))
            sensor = stack.enter_context(patch.object(fand, "Sensors")).return_value
            sensor.sample.side_effect = [dict(profile="performance", gpu_w=81, cpu_w=None),
                                         RuntimeError("end of simulation")]
            output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            with self.assertRaisesRegex(RuntimeError, "end of simulation"):
                fand.run_service(True, 120)
        status = json.loads(output.getvalue())
        # RAPL and usbmon are unavailable to the user; they are reported but must not force the
        # fallback (review fix 2026-11-17). 81 W selects the 50-110 W row.
        self.assertEqual(tuple(status["floors"].values()), (35, 40, 70))
        self.assertFalse(status["degraded"])
        self.assertFalse(status["applied"])
        self.assertIn("usbmon requires root", status["unavailable"])
        self.command.assert_not_called()
        writer.assert_not_called()
        thread.assert_not_called()

    def test_calibration_lease_and_mode_resume(self):
        control = self.path / "control"
        control.mkdir()
        fd = os.open(control, os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fand.fcntl.flock(fd, fand.fcntl.LOCK_EX)
        records = []
        original_write = fand.atomic_write
        def publish(path, text, *args, **kwargs):
            original_write(path, text, *args, **kwargs)
            if path.name == "status.json":
                records.append(json.loads(text))
                if len(records) == 1:
                    self.assertEqual(self.command.call_count, 0)
                    fand.fcntl.flock(fd, fand.fcntl.LOCK_UN)
        sample = dict(cpu_w=20, gpu_w=10, profile="performance")
        with contextlib.ExitStack() as stack:
            sensor = stack.enter_context(patch.object(fand, "Sensors")).return_value
            sensor.sample.side_effect = [sample, sample, sample, RuntimeError("end")]
            monitor = stack.enter_context(patch.object(fand, "UsbMonitor")).return_value
            monitor.snapshot.return_value = dict(audio_active=False, devices=[], error=None)
            stack.enter_context(patch.object(fand, "config", return_value=CFG))
            stack.enter_context(patch.object(fand.signal, "signal"))
            stack.enter_context(patch.object(fand, "atomic_write", side_effect=publish))
            stack.enter_context(patch.object(fand, "read_mode", side_effect=["off", "off", "auto"]))
            stack.enter_context(patch.object(threading.Event, "wait", return_value=False))
            with self.assertRaisesRegex(RuntimeError, "end"):
                fand.run_service(False, None)
        self.assertTrue(records[0]["manual_control"])
        self.assertFalse(records[0]["applied"])
        self.assertTrue(records[1]["applied"])
        # Off -> Auto must reassert the curves even if both decisions request a zero floor.
        self.assertEqual(self.command.call_count, 8)

    def test_model_override_and_invalid_config(self):
        fand.atomic_write(self.path / "config.json", "{}")
        model = self.path / "model.json"
        fand.atomic_write(model, '{"table":[{"min_gpu_w":0,"floors":[20,30,45]}]}')
        cfg = fand.config(installed=True)
        self.assertEqual(fand.Policy(cfg).step(0, 42, 10)[0], (20,30,45))
        fand.atomic_write(model, '{"table":[]}')
        with self.assertRaises(ValueError):
            fand.config(installed=True)

    def test_write_model_validates_installs_and_restarts(self):
        table = [{"min_gpu_w": 0, "floors": [0, 0, 0]}, {"min_gpu_w": 75, "floors": [35, 40, 70]}]
        fand.write_model(json.dumps(table))
        model = self.path / "model.json"
        self.assertEqual(model.stat().st_mode & 0o777, 0o644)
        self.assertEqual(fand.read_json(model)["table"], table)
        self.assertEqual(fand.read_json(model)["source"], "auto-tune")
        self.assertEqual(self.command.call_args.args[0],
                         ["/usr/bin/systemctl", "try-restart", "rog-fand.service"])
        fand.atomic_write(self.path / "config.json", "{}")
        self.assertEqual(fand.config(installed=True)["table"], table)
        row = lambda w, f: {"min_gpu_w": w, "floors": f}
        bad = ["[]", "{}", "not json", json.dumps([row(0, [0.5, 0, 0])]), json.dumps([row(0.0, [0, 0, 0])]),
               json.dumps([row(0, [True, 0, 0])]), json.dumps([dict(row(0, [0, 0, 0]), note="x")]),
               json.dumps([row(5, [0, 0, 0])]), json.dumps([row(0, [0, 0, 0]), row(0, [1, 1, 1])]),
               json.dumps([row(0, [0, 0, 101])]), json.dumps([row(0, [0, 0])]),
               json.dumps([row(i * 10, [0, 0, 0]) for i in range(17)]), " " * 4097]
        for text in bad:
            with self.subTest(text=text[:60]), self.assertRaises(ValueError):
                fand.write_model(text)
        self.assertEqual(fand.read_json(model)["table"], table)
        self.assertEqual(self.command.call_count, 1)

    def test_write_model_needs_root_and_reports_invalid_input(self):
        with patch.object(fand.os, "geteuid", return_value=1000), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fand.main(["--write-model", "[]"])
        err = io.StringIO()
        with patch.object(fand.os, "geteuid", return_value=0), contextlib.redirect_stderr(err):
            self.assertEqual(fand.main(["--write-model", "[]"]), 1)
        self.assertIn("invalid model", err.getvalue())
        self.assertFalse((self.path / "model.json").exists())
        self.command.assert_not_called()


QUIET_OUTPUT = """
Fan curves for Quiet

[
    (
        fan: CPU,
        pwm: (2, 2, 12, 12, 35, 35, 53, 76),
        temp: (64, 64, 72, 72, 76, 76, 80, 85),
        enabled: false,
    ),
    (
        fan: GPU,
        pwm: (2, 2, 28, 28, 38, 38, 73, 73),
        temp: (64, 64, 72, 72, 76, 76, 80, 85),
        enabled: false,
    ),
    (
        fan: MID,
        pwm: (2, 2, 25, 25, 58, 58, 71, 84),
        temp: (64, 64, 72, 72, 76, 76, 80, 85),
        enabled: false,
    ),
]
"""


class AllProfilesTest(unittest.TestCase):
    """Balanced and Quiet: untouched at zero floors, raised with an exact receipt, handed back."""
    setUp = LifecycleTest.setUp

    def stored(self):
        self.command.return_value = subprocess.CompletedProcess([], 0, stdout=QUIET_OUTPUT, stderr="")
        return fand.read_stored("quiet")

    def argv(self, i):
        return self.command.call_args_list[i].args[0]

    def test_read_stored_parses_asusctl_output(self):
        stored = self.stored()
        self.assertEqual(stored["curves"]["mid"][0], [64, 2])
        self.assertEqual(stored["curves"]["cpu"][-1], [85, 76])
        self.assertEqual(stored["enabled"], dict(cpu=False, gpu=False, mid=False))
        self.assertEqual(self.argv(0), ["/usr/bin/asusctl", "fan-curve", "--mod-profile", "quiet"])
        self.command.return_value = subprocess.CompletedProcess([], 0, stdout="no curves", stderr="")
        with self.assertRaises(ValueError):
            fand.read_stored("quiet")

    def test_raise_and_hand_back_quiet(self):
        stored = self.stored()
        self.command.reset_mock()
        self.profile.return_value = "quiet"
        with self.assertRaisesRegex(ValueError, "own curves"):
            self.actuator.apply(BASE, (35, 40, 70), "quiet")
        self.assertTrue(self.actuator.apply(BASE, (35, 40, 70), "quiet", stored))
        receipt = self.path / "restore-quiet.json"
        self.assertEqual(fand.read_json(receipt), stored)
        self.assertFalse(self.actuator.receipt.exists(), "Performance's receipt is separate")
        self.assertEqual([self.argv(i)[3] for i in range(4)], ["quiet"] * 4)
        self.assertEqual(self.argv(3)[-2:], ["--enable-fan-curves", "true"])
        mid = dict(zip(fand.FANS, fand.build_curves(BASE, (35, 40, 70)).values()))["mid"]
        self.assertEqual(self.argv(2)[-1], ",".join(f"{t}c:{p}" for t, p in mid))
        # The stored curves were disabled: handing back only switches custom curves off again.
        self.assertTrue(self.actuator.restore_profile("quiet"))
        self.assertEqual(self.argv(4), ["/usr/bin/asusctl", "fan-curve", "--mod-profile", "quiet",
                                        "--enable-fan-curves", "false"])
        self.assertFalse(receipt.exists())

    def test_enabled_stored_curves_are_rewritten_with_their_flags(self):
        stored = self.stored()
        stored["enabled"]["mid"] = True
        self.command.reset_mock()
        self.profile.return_value = "balanced"
        self.actuator.apply(BASE, (80, 80, 80), "balanced", stored)
        self.command.reset_mock()
        self.assertTrue(self.actuator.restore_profile("balanced"))
        calls = [self.argv(i)[4:] for i in range(self.command.call_count)]
        self.assertEqual(calls[2], ["--fan", "mid", "--data", ",".join(f"{t}c:{p}" for t, p in stored["curves"]["mid"])])
        self.assertEqual(calls[3:], [["--fan", "cpu", "--enable-fan-curve", "false"],
                                     ["--fan", "gpu", "--enable-fan-curve", "false"],
                                     ["--fan", "mid", "--enable-fan-curve", "true"]])

    def test_receipt_waits_for_its_own_profile(self):
        stored = self.stored()
        self.profile.return_value = "quiet"
        self.actuator.apply(BASE, (35, 40, 70), "quiet", stored)
        self.command.reset_mock()
        self.profile.return_value = "performance"
        self.assertFalse(fand.Actuator().restore(), "Quiet can only be handed back while active")
        self.assertEqual(self.command.call_count, 0)
        self.assertEqual(fand.Actuator().pending(), ["quiet"])
        self.profile.return_value = "quiet"
        self.assertTrue(fand.Actuator().restore())
        self.assertEqual(fand.Actuator().pending(), [])

    def test_config_profiles(self):
        self.assertEqual(CFG["profiles"], ["performance", "balanced", "quiet"])
        for bad in (["turbo"], ["quiet", "quiet"], "quiet"):
            with patch.object(fand, "read_json", side_effect=[dict(CFG, profiles=bad)]):
                with self.assertRaisesRegex(ValueError, "profiles"):
                    fand.config()

    def run_loop(self, samples, cfg=CFG, drops=0):
        records = []
        original_write = fand.atomic_write
        def capture(path, text, *args, **kwargs):
            if path.name == "status.json":
                records.append(json.loads(text))
            original_write(path, text, *args, **kwargs)
        usb = dict(audio_active=True, devices=[dict(skipped=drops)], error=None)
        with contextlib.ExitStack() as stack:
            sensor = stack.enter_context(patch.object(fand, "Sensors")).return_value
            sensor.sample.side_effect = [*samples, RuntimeError("end of simulation")]
            stack.enter_context(patch.object(fand, "UsbMonitor")).return_value.snapshot.return_value = usb
            stack.enter_context(patch.object(fand, "config", return_value=cfg))
            stack.enter_context(patch.object(fand.signal, "signal"))
            stack.enter_context(patch.object(fand, "atomic_write", side_effect=capture))
            stack.enter_context(patch.object(fand, "read_mode", return_value="auto"))
            stack.enter_context(patch.object(threading.Event, "wait", return_value=False))
            stored = stack.enter_context(patch.object(fand, "read_stored", return_value=self.stored()))
            self.command.reset_mock()
            with self.assertRaisesRegex(RuntimeError, "end of simulation"):
                fand.run_service(False, None)
        return records, stored

    def test_live_loop_protects_quiet_only_when_needed(self):
        self.profile.return_value = "quiet"
        idle = dict(cpu_w=8, gpu_w=12, gpu_c=45, cpu_c=50, board_c=50, cpu_rpm=2000, gpu_rpm=2000,
                    mid_rpm=2000, profile="quiet")
        records, stored = self.run_loop([idle] * 3)
        self.assertEqual(self.command.call_count, 0, "an idle Quiet profile is left alone")
        self.assertTrue(records[0]["controlled"] and records[0]["profiles"] == CFG["profiles"])
        stored.assert_not_called()
        gaming = dict(idle, gpu_w=54)
        records, stored = self.run_loop([gaming] * 3, drops=40)
        stored.assert_called_once_with("quiet")
        self.assertTrue(records[-1]["applied"] and records[-1]["floors"]["mid"] >= 70)
        self.assertTrue(all(c.args[0][3] == "quiet" for c in self.command.call_args_list[:4]))
        # The service stops while still raised: the finally block hands Quiet back exactly.
        self.assertEqual(self.argv(-1)[3:], ["quiet", "--enable-fan-curves", "false"])
        self.assertFalse((self.path / "restore-quiet.json").exists())

    def test_excluded_profile_is_never_touched(self):
        self.profile.return_value = "quiet"
        sample = dict(cpu_w=8, gpu_w=150, gpu_c=70, cpu_c=80, board_c=80, cpu_rpm=2000, gpu_rpm=2000,
                      mid_rpm=2000, profile="quiet")
        records, stored = self.run_loop([sample] * 3, cfg=dict(CFG, profiles=["performance"]), drops=40)
        self.assertEqual(self.command.call_count, 0)
        stored.assert_not_called()
        self.assertFalse(records[-1]["controlled"])
        self.assertEqual(records[-1]["floors"], dict(cpu=0, gpu=0, mid=0))


class ReplayTest(unittest.TestCase):
    def test_all_four_recordings(self):
        logs = sorted((ROOT / "data/logs").glob("1[0123]-*.csv"))
        self.assertEqual(len(logs), 4)
        for path in logs:
            with self.subTest(path=path.name):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), patch.object(fand, "Sensors") as hardware:
                    self.assertEqual(fand.main(["--replay", str(path)]), 0)
                hardware.assert_not_called()
                rows = [json.loads(line) for line in output.getvalue().splitlines()]
                light = sum(tuple(r["floors"].values()) == (35,40,70) for r in rows)
                heavy = sum(tuple(r["floors"].values()) == (80,80,80) for r in rows)
                if path.name.startswith("10"):
                    self.assertGreater(light, len(rows) * 0.8)
                elif path.name.startswith("13"):
                    self.assertGreater(min(light, heavy), len(rows) / 3)
                else:
                    self.assertGreater(heavy, len(rows) * 0.9)

    def test_temperature_log_format_and_unavailable_gpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.csv"
            fand.atomic_write(path, "t,err_per_s,sen3,cpu,gpu_temp,gpu_w\n"
                              "23:59:59,40,70,65,65,75\n00:00:01,40,70,65,-1,-1\n")
            rows = list(fand.replay(path, CFG))
            self.assertEqual(tuple(rows[0]["floors"].values()), (35,40,70))
            self.assertEqual(tuple(rows[1]["floors"].values()), (60,60,80))


class ReviewFixesTest(unittest.TestCase):
    """Regressions from the 2026-11-17 review: start-up fallback, reader, stream warm-up, config."""

    def idle(self, **missing):
        values = dict(cpu_w=8.0, gpu_w=15.0, gpu_c=45, cpu_c=52.0, board_c=50.0,
                      cpu_rpm=2100, gpu_rpm=2500, mid_rpm=3100, profile="performance")
        values.update(missing)
        return values

    def test_startup_gap_does_not_force_fallback(self):
        # First sample after a start: RAPL has no delta and usbmon is still starting.
        gaps, policy = fand.Gaps(), fand.Policy(CFG)
        gpu, degraded = gaps.update(self.idle(cpu_w=None))
        self.assertFalse(degraded)
        self.assertEqual(policy.step(0, gpu, None, degraded=degraded)[0], (0, 0, 0))
        for t in range(2, 400, 2):
            gpu, degraded = gaps.update(self.idle())
            self.assertEqual(policy.step(t, gpu, 8.0, degraded=degraded)[0], (0, 0, 0))

    def test_rapl_and_usbmon_never_degrade(self):
        gaps = fand.Gaps()
        for _ in range(10):
            self.assertFalse(gaps.update(self.idle(cpu_w=None))[1])

    def test_short_gpu_gap_holds_last_value_then_falls_back(self):
        gaps = fand.Gaps()
        self.assertEqual(gaps.update(self.idle(gpu_w=120.0)), (120.0, False))
        self.assertEqual(gaps.update(self.idle(gpu_w=None)), (120.0, False))
        self.assertEqual(gaps.update(self.idle(gpu_w=None)), (120.0, False))
        self.assertEqual(gaps.update(self.idle(gpu_w=None)), (None, True))
        self.assertEqual(gaps.update(self.idle(gpu_w=130.0)), (130.0, False))

    def test_persistent_missing_temperature_degrades(self):
        gaps = fand.Gaps()
        results = [gaps.update(self.idle(board_c=None))[1] for _ in range(4)]
        self.assertEqual(results, [False, False, True, True])
        self.assertFalse(gaps.update(self.idle())[1])

    def test_stream_warmup_skips_are_separated(self):
        counts = sensors.UsbCounts()
        dev = (1, 12)
        counts.add((dev, 1_000_000, 8, 3), 0)       # stream starts: skip inside warm-up
        counts.add((dev, 1_900_000, 8, 2), 0)       # still inside the first second
        counts.add((dev, 2_100_000, 8, 4), 0)       # after warm-up: a real skip
        interval, warmup = counts.take()
        self.assertEqual(interval[dev][:2], [24, 9])
        self.assertEqual(warmup[dev], 5)
        counts.add((dev, 5_000_000, 8, 1), 0)       # 2.9 s gap: a new stream, warm-up again
        self.assertEqual(counts.take()[1][dev], 1)
        counts.add((dev, 5_500_000, 8, 0), 0)
        counts.add((dev, 6_200_000, 8, 2), 0)       # 1.2 s into the stream: counted
        self.assertEqual(counts.take()[1].get(dev, 0), 0)

    def test_policy_ignores_warmup_skips_in_snapshot(self):
        u = sensors.UsbMonitor(threading.Event())
        u.error, u.sample_at = None, 0
        u.counts.add(((1, 12), 1_000_000, 8, 4), 1)
        with patch.object(sensors, "usb_names", return_value={}):
            device = u.snapshot(2)["devices"][0]
        self.assertEqual((device["skipped"], device["warmup_skipped"]), (4, 4))

    def test_reader_blocks_and_counts_from_a_stream(self):
        lines = [
            "aa 1000000 S Zo:1:012:1 -115:1:1 7 -18:0:48 -18:48:48 -18:96:48 -18:144:48 -18:192:48 336 = 00",
            "aa 1000100 C Zi:1:012:1 0:8:2:0 1 0:0:4 4 = 00",
            "aa 2500000 C Zo:1:012:1 0:1:1:0 7 0:0:48 0:48:48 0:96:48 0:144:48 0:192:48 336 >",
            "aa 2600000 C Zo:1:012:1 0:1:8:0 6 -18:0:0 -18:48:0 -18:96:0 -18:144:0 -18:192:0 288 >",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            fifo = Path(tmp) / "0u"
            os.mkfifo(fifo)
            stop = threading.Event()
            u = sensors.UsbMonitor(stop)
            with patch.object(sensors, "USBMON", str(fifo)):
                reader = threading.Thread(target=u.run, daemon=True)
                reader.start()
                with open(fifo, "w") as out:
                    # Two events in one write and one split across writes.
                    out.write(lines[0] + "\n" + lines[1] + "\n" + lines[2][:30])
                    out.flush()
                    out.write(lines[2][30:] + "\n" + lines[3] + "\n")
                # Let the reader drain the pipe before stopping it (closing the writer gives EOF).
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    with u.lock:
                        if u.counts.interval.get((1, 12), [0])[0] >= 13:
                            break
                    time.sleep(0.01)
                stop.set()
                reader.join(timeout=5)
        interval, _ = u.counts.take()
        self.assertEqual(dict(interval), {(1, 12): [13, 6, 1]})

    def test_unknown_control_user_is_a_clear_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            etc = Path(tmp)
            fand.atomic_write(etc / "config.json", json.dumps({"control_user": "no-such-user-xyz"}))
            with patch.object(fand, "ETC", etc):
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    fand.config(installed=True)
                fand.atomic_write(etc / "config.json", json.dumps({"control_user": 5}))
                with self.assertRaisesRegex(ValueError, "user name or null"):
                    fand.config(installed=True)


if __name__ == "__main__":
    unittest.main()
