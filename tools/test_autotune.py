"""Auto-tune tests: a fake machine (clock, sensors, fans and load), never real load or fan writes."""
import argparse
import contextlib
import csv
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("autotune", HERE / "autotune.py")
at = importlib.util.module_from_spec(spec)
spec.loader.exec_module(at)
spec = importlib.util.spec_from_file_location("gpuload", HERE / "gpuload.py")
gl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gl)
sys.path.insert(0, str(HERE.parent / "service"))
import rog_fand as fand  # noqa: E402

COST = {f: f[0] + f[1] + 2 * f[2] for f in at.LADDER}   # noise-weighted airflow of each rung


class FakeMachine:
    """Stalls (8/s) whenever the floors' weighted airflow is below what the level needs."""

    def __init__(self, need, log=None):
        self.need = need
        self.clock = 0.0
        self.floors = self.level = None
        self.log = log if log is not None else []
        self.cancel_at = None
        self.override = lambda m: {}
        self.usb_at = set()     # clock values at which one USB drop is reported

    def now(self):
        return self.clock

    def wait(self, seconds):
        self.clock += seconds
        return self.cancel_at is not None and self.clock >= self.cancel_at

    def set_floors(self, floors):
        self.floors = tuple(floors)
        self.log.append(("floors", self.floors))

    def start_load(self, level):
        self.level = level
        self.log.append(("start", level.gpu_w))

    def stop_load(self):
        self.level = None
        self.log.append(("stop",))

    def sample(self):
        clean = self.level is None or sum(self.floors) + self.floors[2] >= self.need[self.level.gpu_w]
        s = dict(stalls_per_s=0.0 if clean else 8.0, usb_drops=int(self.clock in self.usb_at),
                 cpu_w=40.0, gpu_w=(self.level.gpu_w - 1) if self.level else 15.0, gpu_c=70, cpu_c=80,
                 board_c=75, profile="performance", ac=True, cpu_rpm=4000, gpu_rpm=4200, mid_rpm=5000)
        s.update(self.override(self))
        return s


def tuner(machine, levels="160:4,120:3,80:2,40:2", events=None, **kw):
    events = events if events is not None else []
    kw.setdefault("rebalance", ())     # the ladder alone; QuieterMixTest turns the search on
    out = io.StringIO()
    t = at.Tuner(machine, at.parse_levels(levels), at.LADDER,
                 lambda kind, message, **v: events.append(dict(kind=kind, message=message, **v)),
                 csv.writer(out), warmup_s=0, clock=lambda: "12:00:00", **kw)
    t.started = machine.now()
    t.csv = out
    return t


class SteppingTest(unittest.TestCase):
    def test_descends_to_first_stall_per_level_and_builds_table(self):
        m = FakeMachine({160: 300, 120: 260, 80: 215, 40: 0})
        events = []
        t = tuner(m, events=events)
        table = t.run()
        floors = [r["floors"] for r in t.results]
        self.assertEqual(floors, [[75, 75, 75], [60, 60, 70], [35, 40, 70], [0, 0, 0]])
        # Each cooler level starts at the hotter level's clean rung, never above it.
        self.assertEqual([s["rung"] for s in t.steps if s["level"] == 2][0], 3)
        self.assertEqual([s["stalled"] for s in t.steps if s["level"] == 1],
                         [False, False, False, False, True])
        self.assertEqual(table, [{"min_gpu_w": 0, "floors": [0, 0, 0]},
                                 {"min_gpu_w": 35, "floors": [35, 40, 70]},
                                 {"min_gpu_w": 75, "floors": [60, 60, 70]},
                                 {"min_gpu_w": 115, "floors": [75, 75, 75]}])
        # The proposal is accepted by both the panel's writer check and the service itself.
        self.assertEqual(at.core.check_model(table), table)
        fand.validate_table(table)
        self.assertEqual(json.loads(fand.json.dumps(table)), table)
        kinds = [e["kind"] for e in events]
        self.assertEqual(kinds.count("level"), 4)
        self.assertEqual(kinds.count("level-result"), 4)
        self.assertEqual(kinds.count("result"), len(t.steps))
        self.assertTrue(all(0 <= e["progress"] <= 1 for e in events if e["kind"] == "sample"))
        rows = list(csv.reader(io.StringIO(t.csv.getvalue())))
        self.assertEqual(len(rows), sum(1 for e in events if e["kind"] == "sample"))

    def test_safe_curve_between_levels_before_the_load_changes(self):
        m = FakeMachine({160: 300, 40: 0})
        t = tuner(m, "160:4,40:2")
        t.run()
        stall = m.log.index(("floors", (70, 70, 75)))
        # Right after the stalling step: the last clean floors, then the load stops.
        self.assertEqual(m.log[stall + 1:stall + 3], [("floors", (75, 75, 75)), ("stop",)])
        self.assertEqual(m.log[stall + 3], ("start", 40.0))

    def test_clean_steps_take_the_full_step_and_stalls_end_early(self):
        m = FakeMachine({160: 300})
        t = tuner(m, "160:4")
        t.run()
        clean, stalled = t.steps[0], t.steps[-1]
        self.assertEqual(clean["samples"], 60)          # last 120 s of 180, every 2 s
        self.assertFalse(clean["early"])
        self.assertTrue(stalled["stalled"] and stalled["early"])
        self.assertLess(stalled["samples"], 10)         # 8 stalls/s settles it within seconds
        self.assertEqual(stalled["mean"], 8.0)

    def test_one_usb_drop_in_the_judged_part_is_a_stall(self):
        m = FakeMachine({160: 0})
        m.usb_at = {100.0}                              # inside the first step's judged window
        t = tuner(m, "160:4")
        t.run()
        self.assertTrue(t.steps[0]["stalled"])
        self.assertEqual(t.steps[0]["usb_drops"], 1)
        self.assertTrue(t.steps[0]["early"])

    def test_usb_drop_while_settling_is_not_judged(self):
        m = FakeMachine({160: 0})
        m.usb_at = {30.0}
        t = tuner(m, "160:4")
        t.run()
        self.assertFalse(t.steps[0]["stalled"])

    def test_threshold_is_more_than_one_stall_per_second(self):
        window = [dict(stalls_per_s=1.0, usb_drops=0)] * 60
        self.assertFalse(at.judge(window)["stalled"])
        self.assertTrue(at.judge(window[:-1] + [dict(stalls_per_s=4.0, usb_drops=0)])["stalled"])
        self.assertFalse(at.stall_certain(window[:30], 60))
        self.assertTrue(at.stall_certain([dict(stalls_per_s=62.0, usb_drops=0)], 60))

    def test_first_combination_stalls_goes_louder(self):
        m = FakeMachine({160: 300, 120: 330})
        t = tuner(m, "160:4,120:3")
        t.run()
        second = [s for s in t.steps if s["level"] == 2]
        self.assertEqual([s["rung"] for s in second], [3, 2, 1])
        self.assertEqual(t.results[1]["floors"], [90, 90, 90])
        # Floors never fall as power rises: the hotter row takes the louder result too.
        self.assertEqual(at.build_table(t.results)[-1]["floors"], [90, 90, 90])

    def test_never_clean_uses_full_fans(self):
        m = FakeMachine({160: 1000})
        t = tuner(m, "160:4")
        table = t.run()
        self.assertFalse(t.results[0]["clean"])
        self.assertEqual(table, [{"min_gpu_w": 0, "floors": [100, 100, 100]}])
        self.assertEqual(m.log[-2], ("floors", (100, 100, 100)))

    def test_cancel(self):
        m = FakeMachine({160: 300})
        m.cancel_at = 50
        with self.assertRaises(at.Cancelled):
            tuner(m, "160:4").run()


class MiddleHeavyMachine(FakeMachine):
    """Like FakeMachine, but the middle fan moves more air over the chipset (counts three times),
    so moving airflow from it to the CPU and GPU fans can stall, as it might on the laptop."""

    def sample(self):
        s = super().sample()
        if self.level is not None:
            c, g, m = self.floors
            clean = c + g + 3 * m >= self.need[self.level.gpu_w]
            s["stalls_per_s"] = 0.0 if clean else 8.0
        return s


class QuieterMixTest(unittest.TestCase):
    def test_noise_score(self):
        self.assertEqual(at.NOISE_WEIGHTS, (1, 1, 2), "taken from the service's noise_weights")
        self.assertLess(at.noise_cost((45, 50, 60)), at.noise_cost((35, 40, 70)))
        self.assertLess(at.noise_cost((60, 60, 60)), at.noise_cost((50, 50, 70)))
        self.assertEqual(at.rebalanced((35, 40, 70), 10), (45, 50, 60))
        self.assertEqual(at.rebalanced((35, 40, 70), 20), (55, 60, 50))
        self.assertIsNone(at.rebalanced((80, 80, 80), 10), "spreading an even mix is louder")
        self.assertIsNone(at.rebalanced((60, 60, 70), 10), "an equal score is not quieter")
        self.assertIsNone(at.rebalanced((0, 0, 0), 10))
        self.assertEqual(at.rebalanced((95, 100, 70), 10, weights=(1, 1, 10)), (100, 100, 60), "capped at 100 %")

    def test_keeps_the_quietest_clean_mix_and_stops_at_a_stall(self):
        # 160 W needs 280: ladder clean at 35/40/70 (285), 25/30/60 stalls (235).
        #   10 points -> 45/50/60 (275): stalls, keep 35/40/70.
        # 120 W needs 270: same ladder result; 45/50/60 (275) clean, 55/60/50 (265) stalls.
        # 40 W needs 0: the ladder reaches 0/0/0 and nothing quieter exists.
        m = MiddleHeavyMachine({160: 280, 120: 270, 40: 0})
        events = []
        t = tuner(m, "160:4,120:3,40:2", events=events, rebalance=at.REBALANCE)
        table = t.run()
        r160, r120, r40 = t.results
        self.assertEqual((r160["floors"], r160["rebalanced"]), ([35, 40, 70], False))
        self.assertEqual([x["stalled"] for x in r160["tried"]], [True])
        self.assertEqual((r120["floors"], r120["ladder_floors"], r120["rebalanced"]),
                         ([45, 50, 60], [35, 40, 70], True))
        self.assertEqual([(x["floors"], x["stalled"]) for x in r120["tried"]],
                         [([45, 50, 60], False), ([55, 60, 50], True)])
        self.assertEqual((r40["floors"], r40["tried"]), ([0, 0, 0], []))
        self.assertLess(r120["noise"], at.noise_cost((35, 40, 70)))
        # The table uses the chosen mixes. Floors still never fall as power rises, so the 160 W row
        # takes the higher of each fan from the 120 W mix and its own (never less airflow).
        self.assertEqual(table, [{"min_gpu_w": 0, "floors": [0, 0, 0]},
                                 {"min_gpu_w": 35, "floors": [45, 50, 60]},
                                 {"min_gpu_w": 115, "floors": [45, 50, 70]}])
        fand.validate_table(table)
        # A cooler level still starts from the hotter level's clean ladder rung.
        self.assertEqual([s["rung"] for s in t.steps if s["level"] == 2][0], r160["rung"])
        mixes = [s for s in t.steps if s["mix"]]
        self.assertEqual(len(mixes), 3)
        self.assertTrue(all(s["name"].startswith("Level") and "quieter mix" in s["name"] for s in mixes))
        self.assertIn("moved from 35/40/70", [e for e in events if e["kind"] == "level-result"][1]["message"])

    def test_safe_curve_is_the_chosen_mix(self):
        m = MiddleHeavyMachine({120: 270, 40: 0})
        t = tuner(m, "120:3,40:2", rebalance=at.REBALANCE)
        t.run()
        stall = m.log.index(("floors", (55, 60, 50)))
        self.assertEqual(m.log[stall + 1:stall + 3], [("floors", (45, 50, 60)), ("stop",)])

    def test_even_airflow_model_keeps_shifting(self):
        # With the plain model (middle counts twice) a shift keeps airflow equal, so both are clean.
        m = FakeMachine({80: 215})
        t = tuner(m, "80:2", rebalance=at.REBALANCE)
        t.run()
        self.assertEqual(t.results[0]["ladder_floors"], [35, 40, 70])
        self.assertEqual(t.results[0]["floors"], [55, 60, 50])

    def test_measured_noise_decides_with_a_microphone(self):
        # Both shifts stay clean (plain airflow model). Measured: 45/50/60 is 1 dB quieter than the
        # ladder step and kept; 55/60/50 is only 0.2 dB quieter than that and not kept.
        noise = {(35, 40, 70): -40.0, (45, 50, 60): -41.0, (55, 60, 50): -41.2}
        m = FakeMachine({80: 215})
        m.override = lambda machine: {"noise_db": noise.get(machine.floors)}
        events = []
        t = tuner(m, "80:2", events=events, rebalance=at.REBALANCE)
        t.run()
        r = t.results[0]
        self.assertEqual((r["ladder_floors"], r["floors"], r["noise_db"]), ([35, 40, 70], [45, 50, 60], -41.0))
        self.assertEqual([(x["floors"], x["kept"], x["noise_db"]) for x in r["tried"]],
                         [([45, 50, 60], True, -41.0), ([55, 60, 50], False, -41.2)])
        self.assertIn("-41.0 dB(A)", [e for e in events if e["kind"] == "level-result"][0]["message"])
        self.assertTrue(any("noise -41.2 dB(A)" in e["message"] for e in events if e["kind"] == "result"))
        rows = list(csv.DictReader(io.StringIO(",".join(at.COLUMNS) + "\n" + t.csv.getvalue())))
        self.assertIn("-41.0", [r["noise_db"] for r in rows])

    def test_louder_by_measurement_is_not_kept(self):
        noise = {(35, 40, 70): -40.0, (45, 50, 60): -39.0, (55, 60, 50): -38.0}
        m = FakeMachine({80: 215})
        m.override = lambda machine: {"noise_db": noise.get(machine.floors)}
        t = tuner(m, "80:2", rebalance=at.REBALANCE)
        t.run()
        self.assertEqual(t.results[0]["floors"], [35, 40, 70])
        self.assertFalse(t.results[0]["rebalanced"])

    def test_plan_mentions_the_microphone(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            at.main(["--plan", "--mic", "alsa_input.usb-Mic-00.mono"])
        self.assertIn("measured from alsa_input.usb-Mic-00.mono", out.getvalue())

    def test_switch_off(self):
        m = MiddleHeavyMachine({120: 270})
        t = tuner(m, "120:3")
        t.run()
        self.assertEqual((t.results[0]["floors"], t.results[0]["tried"]), ([35, 40, 70], []))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(at.main(["--plan", "--no-rebalance"]), 0)
        self.assertIn("Quieter-mix search: off", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(at.main(["--plan"]), 0)
        self.assertIn("move 10 then 20 points from the middle fan", out.getvalue())


class SafetyTest(unittest.TestCase):
    def stop_reason(self, **values):
        m = FakeMachine({160: 0})
        m.override = lambda m: values if m.clock >= 10 else {}
        with self.assertRaises(at.SafetyStop) as caught:
            tuner(m, "160:4").run()
        self.assertEqual(m.clock, 10)
        return str(caught.exception)

    def test_hard_stops(self):
        self.assertIn("CPU reached 95 C", self.stop_reason(cpu_c=95.0))
        self.assertIn("GPU reached 85 C", self.stop_reason(gpu_c=85))
        self.assertIn("Board sensor reached 90 C", self.stop_reason(board_c=90.0))
        self.assertIn("battery", self.stop_reason(ac=False))
        self.assertIn("mains", self.stop_reason(ac=None))
        self.assertIn("Quiet profile", self.stop_reason(profile="quiet"))

    def test_just_below_the_limits_keeps_going(self):
        m = FakeMachine({160: 300})
        m.override = lambda m: dict(cpu_c=94.9, gpu_c=84, board_c=89.9)
        tuner(m, "160:4").run()

    def test_missing_temperature_stops_after_three_samples(self):
        m = FakeMachine({160: 0})
        m.override = lambda m: dict(board_c=None) if 10 <= m.clock else {}
        with self.assertRaisesRegex(at.SafetyStop, "unavailable"):
            tuner(m, "160:4").run()
        self.assertEqual(m.clock, 14)
        m = FakeMachine({160: 300})
        m.override = lambda m: dict(gpu_c=None) if m.clock in (10, 12) else {}
        tuner(m, "160:4").run()

    def test_time_limit(self):
        with self.assertRaisesRegex(at.SafetyStop, "longer than 10 minutes"):
            tuner(FakeMachine({160: 0}), "160:4", max_s=600).run()


class TableAndInputTest(unittest.TestCase):
    def test_rows_merge_round_down_and_never_fall(self):
        results = [dict(gpu_w=79.7, floors=[0, 0, 40]), dict(gpu_w=39.2, floors=[0, 0, 50]),
                   dict(gpu_w=121.0, floors=[60, 60, 70])]
        self.assertEqual(at.build_table(results), [{"min_gpu_w": 0, "floors": [0, 0, 50]},
                                                   {"min_gpu_w": 75, "floors": [60, 60, 70]}])

    def test_levels_and_ladder(self):
        self.assertEqual([lv.gpu_w for lv in at.parse_levels("40:2,160:4")], [160, 40])
        for bad in ("", "160", "160:4,160:2", "300:4", "160:-1", "a:b"):
            with self.subTest(levels=bad), self.assertRaises(ValueError):
                at.parse_levels(bad)
        self.assertEqual(at.parse_ladder("100,100,100;0,0,0"), [(100, 100, 100), (0, 0, 0)])
        for bad in ("100,100,100", "100,100,100;100,100,100", "80,80,80;90,70,70", "101,0,0;0,0,0", "x"):
            with self.subTest(ladder=bad), self.assertRaises(ValueError):
                at.parse_ladder(bad)
        at.check_ladder(at.LADDER)

    def test_floor_curves_match_the_service(self):
        base = fand.config()["base_curve"]
        for floors in at.LADDER:
            self.assertEqual(at.floor_curves(base, floors), {
                fan: [tuple(p) for p in points] for fan, points in fand.build_curves(base, floors).items()})


class HardwareTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.stop = threading.Event()
        self.fans = unittest.mock.Mock()
        base = {f: [(40 + 6 * i, 30 * i) for i in range(8)] for f in at.FANS}
        self.hw = at.Hardware(self.stop, (base, dict.fromkeys(at.FANS, True)), self.fans)
        self.addCleanup(self.hw.close)
        self.profile = stack.enter_context(patch.object(at.cal, "rd", return_value="performance"))
        self.service = stack.enter_context(patch.object(at.core, "fan_service"))

    def status(self, t, *devices, error=None):
        return "Running", dict(t=t, sensors=dict(cpu_w=33.0), usb=dict(error=error, devices=list(devices)))

    def test_floors_write_changed_fans_only(self):
        self.hw.set_floors((0, 0, 70))
        self.assertEqual([c.args[0] for c in self.fans.set.call_args_list], list(at.FANS))
        self.assertEqual(self.fans.set.call_args_list[2].args[1][0], (40, 179))
        self.fans.reset_mock()
        self.hw.set_floors((0, 0, 50))
        self.assertEqual([c.args[0] for c in self.fans.set.call_args_list], ["mid"])
        self.fans.command.assert_called_with("--enable-fan-curves", "true")
        self.assertTrue(self.hw.changed)
        self.profile.return_value = "balanced"
        with self.assertRaises(at.SafetyStop):
            self.hw.set_floors((10, 0, 50))

    def test_usb_drops_from_every_status_sample_once(self):
        dev = lambda skipped, warm=0: dict(packets=16000, skipped=skipped, warmup_skipped=warm)
        self.service.side_effect = [self.status("a", dev(3)), self.status("a", dev(3)),
                                    self.status("b", dev(5, 5)), self.status("c", dev(2))]
        for _ in range(4):
            self.hw.poll_status()
        self.assertEqual(self.hw.drops, 5)          # a once, b only start-up skips, c
        self.assertEqual(self.hw.cpu_w, 33.0)

    def test_status_problems_stop_the_run(self):
        self.service.return_value = ("Stale", None)
        with self.assertRaisesRegex(RuntimeError, "Stale"):
            self.hw.poll_status()
        self.service.return_value = self.status("x", error="usbmon unavailable: denied")
        with self.assertRaisesRegex(RuntimeError, "usbmon"):
            self.hw.poll_status()
        self.service.side_effect = [self.status(str(i), dict(packets=0, skipped=0)) for i in range(5)]
        with self.assertRaisesRegex(RuntimeError, "No USB audio traffic"):
            for _ in range(5):
                self.hw.poll_status()

    def test_load_generator_exit(self):
        class Proc:
            def __init__(self, code, text=""):
                self.code, self.err = code, io.StringIO(text)

            def poll(self):
                return self.code
        self.hw.loads = [("GPU", Proc(3))]
        with self.assertRaisesRegex(at.SafetyStop, "85 C"):
            self.hw.check_loads()
        self.hw.loads = [("CPU", Proc(None)), ("GPU", Proc(1, "trace\nCUDA failed\n"))]
        with self.assertRaisesRegex(RuntimeError, "GPU load generator stopped .exit 1.: CUDA failed"):
            self.hw.check_loads()
        self.hw.loads = []

    def test_mains_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.profile.side_effect = lambda p, default=None: Path(p).read_text().strip() \
                if Path(p).exists() else default
            self.assertIsNone(at.mains_online(root))
            for name, kind, online in (("BAT0", "Battery", "1"), ("ADP0", "Mains", "0")):
                (root / name).mkdir()
                (root / name / "type").write_text(kind)
                (root / name / "online").write_text(online)
            self.assertFalse(at.mains_online(root))
            (root / "ADP0" / "online").write_text("1")
            self.assertTrue(at.mains_online(root))


class FakeHardware(FakeMachine):
    """Stands in for Hardware inside run(): records the order of the cleanup steps."""
    instances = []

    def __init__(self, stop, saved, fans, **kw):
        super().__init__({160: 300, 40: 0}, log=[])
        self.stop, self.changed = stop, False
        self.fail_at = None
        FakeHardware.instances.append(self)

    def wait(self, seconds):
        self.clock += seconds
        return self.stop.is_set()

    def start(self):
        self.log.append(("hw-start",))

    def set_floors(self, floors):
        self.changed = True
        super().set_floors(floors)

    def sample(self):
        if self.fail_at is not None and self.clock >= self.fail_at:
            raise RuntimeError("Lost the PCH stall counter")
        return super().sample()

    def close(self):
        self.log.append(("close",))


class RunLifecycleTest(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.out = tmp / "logs" / "autotune.csv"
        self.args = argparse.Namespace(out=str(self.out), model_out=None, step=20, judge=10, warmup=4,
                                       max_minutes=60, usb="Volt 2", panel_json=True)
        self.saved = ({f: [(40, 0)] * 8 for f in at.FANS}, dict.fromkeys(at.FANS, True))
        FakeHardware.instances = []
        stack.enter_context(patch.object(at, "Hardware", FakeHardware))
        stack.enter_context(patch.object(at, "preflight"))
        stack.enter_context(patch.object(at.cal, "calibration_lease", contextlib.nullcontext))
        stack.enter_context(patch.object(at.cal, "rd", return_value="performance"))
        stack.enter_context(patch.object(at.cal, "snapshot", return_value=self.saved))
        self.restore = stack.enter_context(patch.object(at.cal, "restore", side_effect=lambda fans, saved:
                                                        FakeHardware.instances[-1].log.append(("restore",))))
        self.output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stop = threading.Event()

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def session(self, setup=lambda hw: None):
        original = FakeHardware.__init__

        def init(hw, *a, **kw):
            original(hw, *a, **kw)
            setup(hw)
        with patch.object(FakeHardware, "__init__", init):
            return at.run(self.args, at.parse_levels("160:4,40:2"), at.LADDER, self.stop)

    def test_complete_run_publishes_log_and_proposal(self):
        table, _, outcome = self.session()
        self.assertEqual(outcome, dict(cancelled=False, safety=None))
        log = FakeHardware.instances[-1].log
        self.assertEqual(log[-3:], [("stop",), ("restore",), ("close",)])
        self.restore.assert_called_once()
        self.assertEqual(self.restore.call_args.args[1], self.saved)
        with self.out.open() as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(list(rows[0]), list(at.COLUMNS))
        self.assertEqual({r["phase"] for r in rows}, {"warmup", "settle", "judge"})
        proposal = json.loads(self.out.with_suffix(".model.json").read_text())
        self.assertEqual(proposal["table"], table)
        self.assertEqual(proposal["log"], self.out.name)
        events = self.events()
        self.assertEqual([e["kind"] for e in events[-3:]], ["restore", "proposal", "done"])
        self.assertEqual(events[-2]["table"], table)
        self.assertFalse(list(self.out.parent.glob(".autotune-*")))

    def test_safety_stop_ends_load_restores_and_reports(self):
        def hot(hw):
            hw.override = lambda m: dict(gpu_c=86) if m.clock >= 30 else {}
        table, _, outcome = self.session(hot)
        self.assertIsNone(table)
        self.assertIn("GPU reached 86 C", outcome["safety"])
        self.assertEqual(FakeHardware.instances[-1].log[-3:], [("stop",), ("restore",), ("close",)])
        kinds = [e["kind"] for e in self.events()]
        self.assertEqual(kinds[-3:], ["safety", "restore", "done"])
        self.assertNotIn("proposal", kinds)
        self.assertFalse(self.out.with_suffix(".model.json").exists())
        self.assertTrue(self.out.is_file())

    def test_cancel_restores_and_writes_no_proposal(self):
        def cancel(hw):
            hw.override = lambda m: (self.stop.set(), {})[1] if m.clock >= 20 else {}
        table, _, outcome = self.session(cancel)
        self.assertTrue(outcome["cancelled"])
        self.restore.assert_called_once()
        self.assertTrue(self.events()[-1]["cancelled"])
        self.assertFalse(self.out.with_suffix(".model.json").exists())

    def test_error_still_restores_and_keeps_the_partial_log(self):
        def broken(hw):
            hw.fail_at = 12
        with self.assertRaisesRegex(RuntimeError, "stall counter"):
            self.session(broken)
        self.assertEqual(FakeHardware.instances[-1].log[-3:], [("stop",), ("restore",), ("close",)])
        self.assertTrue(self.out.is_file())

    def test_plan_changes_nothing(self):
        with patch.object(at, "run") as run, patch.object(at, "check_load") as check:
            self.assertEqual(at.main(["--plan", "--out", str(self.out)]), 0)
        run.assert_not_called()
        check.assert_not_called()
        self.assertIn("100/100/100", self.output.getvalue())
        self.assertFalse(self.out.exists())


class GpuLoadTest(unittest.TestCase):
    def test_kernel_and_controller(self):
        ptx = gl.kernel_ptx(2)
        self.assertIn(".visible .entry burn", ptx)
        self.assertEqual(ptx.count("fma.rn.f32"), 2 * 4 * 2)
        c = gl.Controller(120)
        watts = 20.0
        for _ in range(60):              # a plant of 20 W idle + 160 W per unit duty
            c.update(watts)
            watts = 20 + 160 * c.duty
        self.assertAlmostEqual(watts, 120, delta=1)
        c = gl.Controller(200)
        for _ in range(30):
            c.update(175)
        self.assertTrue(c.saturated)

    def test_stops_at_temperature_limit_without_cuda(self):
        class FakeCuda:
            name, bus, sms = "fake", "0000:01:00.0", 2
            launches = closed = 0

            def launch(self, iters):
                FakeCuda.launches += 1

            def close(self):
                FakeCuda.closed += 1

        class FakeNvml:
            closed = 0

            def sample(self, now):
                return 100.0, 90

            def close(self):
                FakeNvml.closed += 1
        events = []
        args = argparse.Namespace(watts=100.0, duty=None, seconds=30, max_temp=85)
        with patch.object(gl, "calibrate", return_value=10):
            code = gl.run(args, threading.Event(), lambda **e: events.append(e), FakeCuda, FakeNvml)
        self.assertEqual(code, 3)
        self.assertEqual(events[-1]["kind"], "stop")
        self.assertEqual((FakeCuda.closed, FakeNvml.closed), (1, 1))


if __name__ == "__main__":
    unittest.main()
