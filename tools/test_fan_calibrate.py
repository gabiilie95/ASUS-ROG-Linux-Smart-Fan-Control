"""Calibration lifecycle tests use temporary CSVs, a fake clock and mocked hardware."""
import argparse
import contextlib
import csv
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
        events = [json.loads(line) for line in self.output.getvalue().splitlines()]
        self.assertEqual([e["kind"] for e in events], ["step","sample","result","restore","done"])
        self.assertFalse(events[-1]["cancelled"])

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


if __name__ == "__main__":
    unittest.main()
