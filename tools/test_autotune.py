"""Auto-tune tests: a fake machine (clock, sensors, fans and load), never real load or fan writes."""
import argparse
import contextlib
import csv
import importlib.util
import io
import json
import math
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


def tuner(machine, levels="160:4,120:3,80:2,40:2", events=None, ladder=at.LADDER, **kw):
    events = events if events is not None else []
    kw.setdefault("rebalance", ())     # the ladder alone; QuieterMixTest turns the search on
    for key, value in (("warmup_s", 0), ("settle_s", 120), ("settle_max_s", 240), ("soak_s", 480),
                       ("judge_s", 120)):
        kw.setdefault(key, value)
    out = io.StringIO()
    t = at.Tuner(machine, at.parse_levels(levels), ladder,
                 lambda kind, message, **v: events.append(dict(kind=kind, message=message, **v)),
                 csv.writer(out), clock=lambda: "12:00:00", **kw)
    t.started = machine.now()
    t.csv = out
    return t


class SteppingTest(unittest.TestCase):
    def test_climbs_from_quiet_per_level_and_builds_table(self):
        m = FakeMachine({160: 300, 120: 260, 80: 215, 40: 0})
        events = []
        t = tuner(m, events=events)
        table = t.run()
        self.assertEqual([lv.gpu_w for lv in t.levels], [40, 80, 120, 160], "coolest first")
        self.assertEqual([r["floors"] for r in t.results], [[0, 0, 0], [35, 40, 70], [60, 60, 70], [75, 75, 75]])
        for level in (1, 2, 3, 4):
            steps = [s for s in t.steps if s["level"] == level]
            # Each level soaks at the quietest floors first, then only ever goes louder.
            self.assertEqual((steps[0]["floors"], steps[0]["soak"]), ([0, 0, 0], True))
            self.assertFalse(any(s["soak"] for s in steps[1:]))
            rungs = [s["rung"] for s in steps]
            self.assertEqual(rungs, list(range(len(at.LADDER) - 1, len(at.LADDER) - 1 - len(rungs), -1)))
            self.assertEqual([s["stalled"] for s in steps], [True] * (len(steps) - 1) + [False])
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
        self.assertEqual({e["phase"] for e in events if e["kind"] == "sample"}, {"soak", "settle", "judge"})
        rows = list(csv.reader(io.StringIO(t.csv.getvalue())))
        self.assertEqual(len(rows), sum(1 for e in events if e["kind"] == "sample"))

    def test_safe_curve_between_levels_before_the_load_changes(self):
        m = FakeMachine({160: 300, 40: 0})
        t = tuner(m, "160:4,40:2")
        t.run()
        hot = m.log.index(("start", 160.0))
        # The cooler level ends on its clean floors, then its load stops before the next one starts.
        self.assertEqual(m.log[hot - 2:hot], [("floors", (0, 0, 0)), ("stop",)])
        self.assertEqual(m.log[hot + 1], ("floors", (0, 0, 0)), "and the next level starts quiet again")
        self.assertEqual(m.log[-2:], [("floors", (75, 75, 75)), ("stop",)])

    def test_steps_settle_then_judge_and_stalls_are_decided_while_settling(self):
        m = FakeMachine({160: 300})
        t = tuner(m, "160:4")
        t.run()
        stalled, clean = t.steps[0], t.steps[-1]
        self.assertTrue(stalled["stalled"] and stalled["early"])
        self.assertEqual((stalled["settle_s"], stalled["samples"], stalled["mean"]), (120, 60, 8.0),
                         "stalling and not cooling after the minimum settle: no judged window needed")
        self.assertFalse(clean["stalled"] or clean["early"])
        self.assertEqual((clean["settle_s"], clean["settled"], clean["samples"]), (120, True, 60),
                         "a steady board settles at the minimum, then the next 120 s are judged")

    def test_one_usb_drop_in_the_judged_part_is_a_stall(self):
        m = FakeMachine({160: 0})
        m.usb_at = {150.0}                              # inside the first step's judged window
        t = tuner(m, "160:4")
        t.run()
        self.assertTrue(t.steps[0]["stalled"] and t.steps[0]["early"])
        self.assertEqual(t.steps[0]["usb_drops"], 1)
        self.assertEqual(t.results[0]["floors"], [0, 0, 50], "the next louder step is clean")

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

    def test_never_clean_uses_full_fans(self):
        m = FakeMachine({160: 1000})
        t = tuner(m, "160:4")
        table = t.run()
        self.assertFalse(t.results[0]["clean"])
        self.assertEqual(len(t.steps), len(at.LADDER))
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


class ThermalMachine(FakeMachine):
    """The board relaxes toward an equilibrium set by heat and airflow (time constant `tau`) and the
    chipset stalls above 70 C. Each fan runs at the higher of the saved curve's speed for the level
    and its floor; the middle fan counts double. The saved curve runs the fans fast only when the
    GPU is hot, so light load with quiet floors heats the board more than heavy load does."""
    STALL_C = 70.0
    BASE = {40: (20, 20, 30), 80: (30, 30, 40), 150: (90, 95, 95)}
    HEAT = {40: 45, 80: 80, 150: 120}

    def __init__(self, board=50.0, tau=180.0):
        super().__init__({})
        self.board, self.tau = board, tau

    def equilibrium(self):
        if self.level is None or self.floors is None:
            return 45.0
        c, g, m = (max(b, f) for b, f in zip(self.BASE[self.level.gpu_w], self.floors))
        return 40 + self.HEAT[self.level.gpu_w] - 0.25 * (c + g + 2 * m)

    def wait(self, seconds):
        target = self.equilibrium()
        self.board = target + (self.board - target) * math.exp(-seconds / self.tau)
        return super().wait(seconds)

    def sample(self):
        s = dict(stalls_per_s=8.0 if self.board > self.STALL_C else 0.03, usb_drops=0, cpu_w=40.0,
                 gpu_w=(self.level.gpu_w - 1) if self.level else 15.0, gpu_c=70, cpu_c=80,
                 board_c=round(self.board, 2), dimm_a_c=round(self.board - 3, 2), profile="performance",
                 ac=True, cpu_rpm=4000, gpu_rpm=4200, mid_rpm=5000)
        s.update(self.override(self))
        return s


class HeatSoakTest(unittest.TestCase):
    def quick(self, machine, **kw):
        q = at.PRESETS["quick"]
        kw = dict(dict(ladder=q["ladder"], settle_s=q["settle"], settle_max_s=q["settle_max"],
                       soak_s=q["soak"], judge_s=q["judge"], rebalance=q["rebalance"]), **kw)
        return tuner(machine, q["levels"], **kw)

    def test_light_load_needs_more_than_heavy(self):
        """The case the first quick run (2026-11-20) missed: heavy load is clean at 0 % floors because
        the saved curve already runs the fans fast, while light load stalls once the board has
        heated up with slow fans."""
        m = ThermalMachine()
        t = self.quick(m)
        table = t.run()
        r40, r80, r150 = t.results
        self.assertEqual((r40["floors"], r150["floors"]), ([0, 0, 0], [0, 0, 0]))
        self.assertGreaterEqual(r80["floors"][2], 60, "light load needs the middle fan")
        quiet80 = [s for s in t.steps if s["level"] == 2][0]
        self.assertTrue(quiet80["soak"] and quiet80["stalled"],
                        "the heat soak at quiet floors finds the light-load stall")
        # Floors never fall as power rises: the heavy row keeps the light-load floors.
        self.assertEqual(table, [{"min_gpu_w": 0, "floors": [0, 0, 0]},
                                 {"min_gpu_w": 35, "floors": r80["floors"]}])
        self.assertLess(m.clock / 60, 50, "the quick run's estimate (about 30-50 minutes)")

    def test_cooling_stalls_are_not_decided_early(self):
        m = ThermalMachine()
        t = self.quick(m, rebalance=())
        t.run()
        cleared = [s for s in t.steps if s["level"] == 2][-1]
        self.assertEqual(cleared["floors"], [50, 50, 70])
        self.assertFalse(cleared["stalled"] or cleared["early"],
                         "stalls left over from the quieter step fade while the board cools")

    def test_soak_waits_for_the_board_to_settle(self):
        m = ThermalMachine()
        t = tuner(m, "40:2", ladder=at.QUICK_LADDER)
        t.run()
        soak = t.steps[0]
        self.assertTrue(soak["settled"])
        self.assertTrue(300 < soak["settle_s"] < 480, soak["settle_s"])
        self.assertLessEqual(abs(soak["board_slope"]), at.SETTLE_SLOPE)

    def test_soak_limit(self):
        m = ThermalMachine(board=20.0, tau=3000.0)       # still heating at about 0.8 C/min after 8 min
        t = tuner(m, "40:2", ladder=at.QUICK_LADDER)
        t.run()
        self.assertEqual((t.steps[0]["settle_s"], t.steps[0]["settled"]), (480, False),
                         "a board still heating at the limit is judged anyway")

    def test_settling_helpers(self):
        flat = [(t, dict(board_c=60.0, dimm_a_c=None)) for t in range(0, 122, 2)]
        self.assertTrue(at.settled(flat))
        rising = [(t, dict(board_c=50 + t / 60, dimm_a_c=40.0)) for t in range(0, 122, 2)]
        self.assertFalse(at.settled(rising), "1 C/min is still heating")
        falling = [(t, dict(board_c=60 - t / 60)) for t in range(0, 122, 2)]
        self.assertTrue(at.cooling(falling))
        self.assertFalse(at.settled([(t, dict(board_c=60.0)) for t in range(0, 40, 2)]), "too short to tell")
        self.assertFalse(at.settled([(t, {}) for t in range(0, 122, 2)]), "no board sensor: not settled")


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
        r40, r120, r160 = t.results
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
        # Every level starts again at the quietest floors.
        self.assertEqual([s["rung"] for s in t.steps if s["level"] == 3][0], len(at.LADDER) - 1)
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

    def test_board_temperatures_in_the_log(self):
        m = FakeMachine({80: 215})
        m.override = lambda machine: {"lan_c": 61.5, "dimm_a_c": 58.0}
        t = tuner(m, "80:2")
        t.run()
        rows = list(csv.DictReader(io.StringIO(",".join(at.COLUMNS) + "\n" + t.csv.getvalue())))
        self.assertTrue(rows and all(r["lan_c"] == "61.5" and r["dimm_a_c"] == "58.0" and r["ssd1_c"] == ""
                                     for r in rows))
        self.assertEqual(at.COLUMNS[-len(at.core.BOARD_TEMPS):], at.core.BOARD_TEMPS)

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

    def test_quick_preset(self):
        self.assertEqual(at.check_ladder(at.QUICK_LADDER), list(at.QUICK_LADDER))
        self.assertTrue(set(at.QUICK_LADDER) <= set(at.LADDER) | {(100, 100, 100)},
                        "quick steps are full-ladder steps, so both runs compare")
        quick = at.PRESETS["quick"]
        self.assertEqual([lv.gpu_w for lv in at.parse_levels(quick["levels"])], [40, 80, 150],
                         "coolest first; a low level keeps idle quiet")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(at.main(["--plan", "--preset", "quick"]), 0)
        text = out.getvalue()
        self.assertIn("Preset: quick (about 30-50 minutes", text)
        self.assertIn("climbed from quiet to loud: 0/0/0  0/0/50", text)
        self.assertIn("soaks up to 480 s", text)
        self.assertIn("Steps settle 120-240 s, then are judged on the next 90 s; stops after 75 minutes", text)
        self.assertIn("GPU 40 W + 2 CPU threads, GPU 80 W + 2 CPU threads, GPU 150 W", text)
        self.assertIn("move 10 points from the middle fan", text)
        self.assertIn("up to 1 extra step per level", text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(at.main(["--plan", "--preset", "quick", "--settle", "150", "--levels", "100:2"]), 0)
        self.assertIn("Steps settle 150-240 s", out.getvalue(), "explicit options win")
        self.assertIn("Levels (coolest first): GPU 100 W + 2 CPU threads", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(at.main(["--plan"]), 0)
        self.assertIn("Preset: full (about 60-120 minutes", out.getvalue())
        self.assertIn("Steps settle 180-360 s, then are judged on the next 120 s; stops after 180", out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            at.main(["--plan", "--settle", "300", "--settle-max", "200"])


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
            tuner(FakeMachine({160: 1000}), "160:4", max_s=600).run()


class TableAndInputTest(unittest.TestCase):
    def test_rows_merge_round_down_and_never_fall(self):
        results = [dict(gpu_w=79.7, floors=[0, 0, 40]), dict(gpu_w=39.2, floors=[0, 0, 50]),
                   dict(gpu_w=121.0, floors=[60, 60, 70])]
        self.assertEqual(at.build_table(results), [{"min_gpu_w": 0, "floors": [0, 0, 50]},
                                                   {"min_gpu_w": 75, "floors": [60, 60, 70]}])

    def test_levels_and_ladder(self):
        self.assertEqual([lv.gpu_w for lv in at.parse_levels("160:4,40:2")], [40, 160])
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
        self.args = argparse.Namespace(out=str(self.out), model_out=None, settle=20, settle_max=30, soak=40,
                                       judge=10, warmup=4, max_minutes=60, usb="Volt 2", panel_json=True)
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
        self.assertEqual({r["phase"] for r in rows}, {"warmup", "soak", "settle", "judge"})
        proposal = json.loads(self.out.with_suffix(".model.json").read_text())
        self.assertEqual(proposal["table"], table)
        self.assertEqual(proposal["preset"], "full", "a namespace without a preset is a full run")
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
