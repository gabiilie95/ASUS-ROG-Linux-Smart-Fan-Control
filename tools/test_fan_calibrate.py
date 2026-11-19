"""Calibration lifecycle tests use temporary CSVs, a fake clock and mocked hardware."""
import argparse
import contextlib
import csv
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("calibrate", Path(__file__).with_name("fan-calibrate.py"))
cal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cal)


class SessionTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.out = tmp / "out.csv"
        self.args = argparse.Namespace(out=str(self.out), step=2, observe=None,
                                       dry_run=False, panel_json=True)
        self.saved = ({f: [(30+i*8, i*32) for i in range(8)] for f in cal.FANS},
                      {"cpu": True, "gpu": False, "mid": True})
        self.command = stack.enter_context(patch.object(cal.Fans, "command", autospec=True))
        self.snapshot = stack.enter_context(patch.object(cal, "snapshot", return_value=self.saved))
        self.audio = stack.enter_context(patch.object(cal.subprocess, "Popen"))
        stack.enter_context(patch.object(cal.core, "HDA_LABEL", "snd_hda_intel:card7"))
        stack.enter_context(patch.object(cal.core, "FAND_RUN", tmp / "runtime"))
        stack.enter_context(patch.object(cal.core, "FAND_UNIT", tmp / "unit"))
        stack.enter_context(patch.object(cal, "counter_ready", return_value=True))
        stack.enter_context(patch.object(cal, "rd", return_value="performance"))
        stack.enter_context(patch.object(cal, "gpu", return_value=(72, 140)))
        stack.enter_context(patch.object(cal, "hda_irqs", side_effect=[100,130]))
        stack.enter_context(patch.object(cal, "zone", return_value=70))
        stack.enter_context(patch.object(cal.core, "read_fans", return_value={f+"_rpm":5000 for f in cal.FANS}))
        stack.enter_context(patch.object(cal, "cpu_power", return_value=(40, 0)))
        self.clock = 0
        self.stop = threading.Event()
        def tick(seconds):
            self.clock += seconds
            return self.stop.is_set()
        stack.enter_context(patch.object(self.stop, "wait", side_effect=tick))
        stack.enter_context(patch.object(cal.time, "monotonic", side_effect=lambda: self.clock))
        self.output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))

    def session(self):
        return cal.run(self.args, [("Test", {"mid":80})], self.stop)

    def restored(self):
        calls = [c.args[1:] for c in self.command.call_args_list[-6:]]
        for i, fan in enumerate(cal.FANS):
            self.assertEqual(calls[i*2], ("--fan", fan, "--data", cal.data(self.saved[0][fan])))
            self.assertEqual(calls[i*2+1], ("--fan", fan, "--enable-fan-curve",
                                            str(self.saved[1][fan]).lower()))

    def test_complete_and_publish_samples(self):
        self.session()
        self.restored()
        with self.out.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mid_rpm"], "5000")
        self.assertEqual(rows[0]["elapsed_s"], "2")
        self.assertEqual(rows[0]["cpu_w"], "40")
        self.assertEqual(rows[0]["mid_pct"], "80")
        self.assertEqual(rows[0]["cpu_pct"], "")
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([e["kind"] for e in events], ["step","sample","result","restore","done"])
        self.assertFalse(events[-1]["cancelled"])
        self.assertEqual(events[2]["analysis"]["status"], "short")

    def test_bounded_extension_and_restore(self):
        self.args.step = 4
        self.args.settle_extra = 6
        with patch.object(cal, "hda_irqs", return_value=100), \
             patch.object(cal, "analyze_step", return_value={"can_extend": True, "reason": "Still cooling"}):
            self.session()
        self.restored()
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([e["duration_s"] for e in events if e["kind"] == "extend"], [10])
        self.assertEqual([e["elapsed"] for e in events if e["kind"] == "sample"], [2,4,6,8,10])

    def test_observe_does_not_extend(self):
        self.args.observe = self.args.step
        self.args.settle_extra = 120
        with patch.object(cal, "analyze_step", return_value={"can_extend": True, "reason": "Observe"}):
            self.session()
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertNotIn("extend", [e["kind"] for e in events])
        self.command.assert_not_called()

    def test_cancel_during_extension_restores(self):
        self.args.step = 2
        self.args.settle_extra = 30
        def tick(seconds):
            self.clock += seconds
            if self.clock >= 4:
                self.stop.set()
            return self.stop.is_set()
        with patch.object(self.stop, "wait", side_effect=tick), \
             patch.object(cal, "analyze_step", return_value={"can_extend": True, "reason": "Cooling"}):
            self.session()
        self.restored()
        self.assertTrue(json.loads(self.output.getvalue().splitlines()[-1])["cancelled"])

    def test_temperature_limit_ends_extension_on_next_sample(self):
        self.args.step = 2
        self.args.settle_extra = 30
        with patch.object(cal, "hda_irqs", return_value=100), \
             patch.object(cal, "zone", side_effect=lambda name: 88 if self.clock>=4 and name=="x86_pkg_temp" else 70), \
             patch.object(cal, "analyze_step", return_value={"can_extend": True, "reason": "Warming"}):
            self.session()
        self.restored()
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([e["elapsed"] for e in events if e["kind"] == "sample"], [2,4])

    def test_analysis_failure_still_restores_and_publishes_samples(self):
        with patch.object(cal, "analyze_step", side_effect=ValueError("analysis failed")):
            with self.assertRaisesRegex(ValueError, "analysis failed"):
                self.session()
        self.restored()
        with self.out.open() as source:
            self.assertEqual(len(list(csv.DictReader(source))), 1)

    def test_cli_summary_uses_board_and_rpm_in_correct_order(self):
        self.args.panel_json = False
        summary = self.session()
        cal.print_summary(self.args, summary)
        self.assertRegex(self.output.getvalue(), r"5000/\s*5000/\s*5000")

    def test_cancel_during_first_write(self):
        self.command.side_effect = lambda *_: self.stop.set()
        self.session()
        self.restored()
        self.assertTrue(json.loads(self.output.getvalue().splitlines()[-1])["cancelled"])

    def test_partial_write_failure(self):
        self.command.side_effect = [None, OSError("asusd failed")] + [None]*6
        with self.assertRaisesRegex(OSError, "asusd failed"):
            self.session()
        self.restored()

    def test_observe_never_reads_or_writes_curves(self):
        self.args.observe = self.args.step
        self.session()
        self.command.assert_not_called()
        self.snapshot.assert_not_called()
        self.audio.assert_not_called()

    def test_explicit_restore_error(self):
        self.command.side_effect = [None]*4 + [OSError("write failed")] + [None]*5
        with self.assertRaisesRegex(RuntimeError, "restore failed"):
            self.session()
        self.restored()
        self.assertTrue(self.out.is_file())

    def test_reject_invalid_custom_step(self):
        for bad in ("", "mid=101", "CPU=80", "mid=70,mid=60", "mid=70;"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cal.core.calibration_steps(bad)


class SavedAnalysisTest(unittest.TestCase):
    def test_exported_samples_reproduce_live_analysis(self):
        readings = []
        for elapsed in range(2, 182, 2):
            temp = 75-max(0, elapsed-70)/30
            readings.append((elapsed, dict(t=f"12:{elapsed//60:02d}:{elapsed%60:02d}",
                step=1, label="Steady load", elapsed_s=elapsed, cpu_w=25, cpu_w_age_s=1,
                gpu_w=100, cpu_rpm=4000, gpu_rpm=4500, mid_rpm=5000,
                board_c=temp, cpu_c=temp+5, gpu_c=temp-5, stalls_per_s=.04, counter_ok=1,
                observe=0, cpu_pct=60, gpu_pct=60, mid_pct=70)))
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "samples.csv"
            with path.open("w", newline="") as dest:
                writer = csv.DictWriter(dest, fieldnames=cal.COLUMNS)
                writer.writeheader()
                writer.writerows(row for _, row in readings)
            result = cal.analyze_csv(path)[0]["analysis"]
        self.assertEqual(result, cal.analyze_step(readings))
        self.assertEqual(result["status"], "cooling")
        self.assertIsNotNone(result["sensors"]["board_c"]["cooling_onset_c"])
        json.dumps(result, allow_nan=False)

    def test_legacy_csv_crosses_midnight_without_hardware(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "samples.csv"
            path.write_text("t,step,label,gpu_w,board_c\n23:59:58,1,Test,80,70\n00:00:02,1,Test,80,69\n")
            with patch.object(cal, "analyze_step", return_value={"status": "short"}) as analyze, \
                 patch.object(cal.core, "fan_service", side_effect=AssertionError("hardware access")), \
                 patch.object(cal.Fans, "command", side_effect=AssertionError("fan write")):
                result = cal.analyze_csv(path)
            self.assertEqual([t for t, _ in analyze.call_args.args[0]], [0,4])
            self.assertEqual(result[0]["name"], "Test")

    def test_new_csv_uses_monotonic_elapsed_and_observe_flag(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "samples.csv"
            path.write_text("t,step,label,elapsed_s,cpu_w,observe\n12:00:00,1,Observe,2,40,1\n12:00:10,1,Observe,4,40,1\n")
            with patch.object(cal, "analyze_step", return_value={}) as analyze:
                cal.analyze_csv(path)
            self.assertEqual([t for t, _ in analyze.call_args.args[0]], [2,4])
            self.assertTrue(analyze.call_args.kwargs["observe"])


class CpuPowerTest(unittest.TestCase):
    def sample(self, age, watts=40):
        return {"t": (datetime.now(timezone.utc)-timedelta(seconds=age)).isoformat(),
                "sensors": {"cpu_w": watts}}

    def test_fresh_power_and_reject_stale_future_or_invalid(self):
        with patch.object(cal.core, "fan_service", return_value=("Running", self.sample(2))):
            watts, age = cal.cpu_power()
        self.assertEqual(watts, 40)
        self.assertAlmostEqual(age, 2, delta=.1)
        for state, sample in [("Stale", self.sample(10)), ("Running", self.sample(7)),
                              ("Running", self.sample(-2)), ("Running", self.sample(0, float("nan"))),
                              ("Running", self.sample(0, -10)), ("Stopped", None)]:
            with self.subTest(state=state, sample=sample), \
                 patch.object(cal.core, "fan_service", return_value=(state, sample)):
                self.assertEqual(cal.cpu_power(), (None, None))


if __name__ == "__main__":
    unittest.main()
