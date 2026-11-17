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
        self.assertEqual(p.step(8, 150, None, performance=False)[0], fand.ZERO)


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
        self.assertFalse(self.actuator.receipt.exists())
        self.assertEqual(self.command.call_args_list[-4].args[0][-1],
                         ",".join(f"{t}c:{p}" for t,p in BASE["cpu"]))

    def test_mode_restart_preserves_off_for_configured_owner(self):
        import pwd
        owner = pwd.getpwuid(os.geteuid())
        path = self.path / "mode"
        fand.atomic_write(path, "off\n")
        fand.init_mode({**CFG, "control_user": owner.pw_name})
        self.assertEqual(fand.read_mode(path), "off")
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
        self.assertEqual(tuple(status["floors"].values()), (60, 60, 80))
        self.assertFalse(status["applied"])
        self.assertIn("usbmon requires root", status["unavailable"])
        self.command.assert_not_called()
        writer.assert_not_called()
        thread.assert_not_called()

    def test_model_override_and_invalid_config(self):
        fand.atomic_write(self.path / "config.json", "{}")
        model = self.path / "model.json"
        fand.atomic_write(model, '{"table":[{"min_gpu_w":0,"floors":[20,30,45]}]}')
        cfg = fand.config(installed=True)
        self.assertEqual(fand.Policy(cfg).step(0, 42, 10)[0], (20,30,45))
        fand.atomic_write(model, '{"table":[]}')
        with self.assertRaises(ValueError):
            fand.config(installed=True)


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


if __name__ == "__main__":
    unittest.main()
