#!/usr/bin/env python3
"""Fan auto-tune: find the quietest fan floors that keep audio clean at several heat levels and
propose a GPU-power table for rog-fand. Unattended, about 60-90 minutes, loud.

    python3 tools/autotune.py                    # full run, log in data/logs/autotune-<time>.csv
    python3 tools/autotune.py --plan             # print the plan and change nothing
    python3 tools/autotune.py --check-load 30    # hold each heat level 30 s; fans untouched

Heat: GPU load from tools/gpuload.py (held at a target power on the NVIDIA GPU), CPU load from
tools/loadgen.c (compiled into a temporary directory at start). Levels run hottest first. At each
level the fan floors step from loud to quiet along a fixed ladder, 180 s per step, judged on the
last 120 s. The descent stops at the first combination that stalls: more than 1 stall/s on the
internal counter, or any USB drop in the service's status. The fans then go straight back to the
last clean combination (the safe curve between levels) and the next, cooler level starts from it.
If a level's first combination already stalls, it steps back up until one is clean. Floors are
applied the way the service applies them: each point of the saved curve raised to the floor.

Needs the Performance profile, the fan service in Off (Auto tab), mains power, a free internal
sound device (the stall counter plays silence on it, so the laptop speakers are blocked) and the
USB audio interface, which is kept busy with silence through PipeWire so USB drops can be counted.
Hard stops: CPU 95 C, GPU 85 C, board sensor 90 C, battery power, or the profile leaving
Performance. Any stop, cancel or error ends the load and restores the exact saved curves.

A complete run ends with a proposed table in the format of service/config.example.json, saved
beside the CSV as <name>.model.json. Nothing is written to /etc: the panel's Calibrate tab shows
the proposal next to the current table and installs it through pkexec.
"""
import argparse
import contextlib
import csv
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("fan_calibrate", HERE / "fan-calibrate.py")
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)
core = cal.core
FANS = cal.FANS
PROFILE = "/sys/firmware/acpi/platform_profile"

# Heat levels as GPU watts:CPU threads, run hottest first. On the G814JZ (2026-11-17) one loadgen
# thread is about 15 W of CPU package power; 4 threads about 57 W at 77 C on their own.
LEVELS = "160:4,120:3,80:2,40:2"
# Fan floors (cpu, gpu, middle %), loud to quiet. Every fan only ever goes down, so a cooler level
# can start where a hotter one ended. Weighted by the noise cost (middle fan twice), each rung is
# quieter than the one before. Earlier calibration: 80/80/80 clean at 150 W GPU, 70/70/70
# marginal; middle 70 clean at 63-84 W.
LADDER = ((100, 100, 100), (90, 90, 90), (80, 80, 80), (75, 75, 75), (70, 70, 75), (60, 60, 70),
          (50, 50, 70), (35, 40, 70), (25, 30, 60), (0, 0, 50), (0, 0, 0))
LIMITS = {"cpu_c": 95, "gpu_c": 85, "board_c": 90}
STALL_LIMIT = 1.0       # stalls per second on the internal counter, mean of the judged window
PERIOD = 2              # seconds between samples
MISSING_LIMIT = 3       # consecutive samples without a temperature before stopping
STATUS_POLL = 0.5       # seconds between reads of the service status while waiting
COLUMNS = ("t", "level", "target_gpu_w", "cpu_threads", "step", "cpu_floor", "gpu_floor", "mid_floor",
           "phase", "elapsed_s", "stalls_per_s", "usb_drops", "cpu_w", "gpu_w", "cpu_c", "gpu_c",
           "board_c", "cpu_rpm", "gpu_rpm", "mid_rpm")


class Cancelled(Exception):
    pass


class SafetyStop(Exception):
    pass


@dataclass(frozen=True)
class Level:
    gpu_w: float
    cpu_threads: int


def parse_levels(text):
    levels = []
    for part in text.split(","):
        try:
            watts, threads = part.split(":")
            level = Level(float(watts), int(threads))
        except ValueError:
            raise ValueError("Levels look like 160:4,120:3 (GPU watts:CPU threads)") from None
        if not 10 <= level.gpu_w <= 175 or not 0 <= level.cpu_threads <= 32:
            raise ValueError("GPU watts must be 10-175 and CPU threads 0-32")
        levels.append(level)
    if not 1 <= len(levels) <= 8 or len({lv.gpu_w for lv in levels}) != len(levels):
        raise ValueError("Use one to eight levels with different GPU watts")
    return sorted(levels, key=lambda lv: -lv.gpu_w)


def parse_ladder(text):
    try:
        ladder = [tuple(int(v) for v in part.split(",")) for part in text.split(";")]
    except ValueError:
        raise ValueError("A ladder looks like 100,100,100;80,80,80;0,0,0") from None
    return check_ladder(ladder)


def check_ladder(ladder):
    if len(ladder) < 2 or any(len(f) != 3 or not all(0 <= v <= 100 for v in f) for f in ladder):
        raise ValueError("A ladder needs at least two cpu,gpu,mid steps of 0-100 %")
    for loud, quiet in zip(ladder, ladder[1:]):
        if quiet == loud or any(q > lo for q, lo in zip(quiet, loud)):
            raise ValueError("Each ladder step must lower at least one fan and raise none")
    return [tuple(f) for f in ladder]


def floor_curves(base, floors):
    """The saved curve with every point raised to the floor, as rog-fand's build_curves does."""
    return {fan: [(t, max(p, math.ceil(pct * 255 / 100))) for t, p in base[fan]]
            for fan, pct in zip(FANS, floors)}


def safety_reason(s, limits=LIMITS, performance=True):
    if s.get("ac") is False:
        return "The laptop is on battery power"
    if s.get("ac") is None:
        return "Cannot confirm mains power"
    if performance and s.get("profile") != "performance":
        return "The %s profile replaced Performance" % core.profile_name(s.get("profile"))
    for key, name in (("cpu_c", "CPU"), ("gpu_c", "GPU"), ("board_c", "Board sensor")):
        value = s.get(key)
        if value is not None and value >= limits[key]:
            return "%s reached %.0f C (limit %d C)" % (name, value, limits[key])
    return None


def judge(window, threshold=STALL_LIMIT):
    rates = [r["stalls_per_s"] for r in window if r["stalls_per_s"] is not None]
    drops = sum(r["usb_drops"] or 0 for r in window)
    mean = sum(rates) / len(rates) if rates else None
    return dict(samples=len(window), mean=mean, peak=max(rates) if rates else None, usb_drops=drops,
                stalled=bool(drops) or (mean is not None and mean > threshold))


def stall_certain(window, expected, threshold=STALL_LIMIT):
    """True once the judged window must end stalled, whatever its remaining samples show."""
    return (any(r["usb_drops"] for r in window)
            or sum(r["stalls_per_s"] or 0 for r in window) > threshold * (expected + 1))


def mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def build_table(results):
    """Rows in service/config.example.json's format, one per level, coolest first.

    A level's floors cover GPU power from the power of the next cooler level up to its own, so every
    row was verified at or above the power it is used for (up to the hottest level's). Floors never
    fall as power rises, and a row identical to the one below it is merged into it."""
    table = []
    start = 0
    below = (0, 0, 0)
    for r in sorted(results, key=lambda r: r["gpu_w"]):
        floors = [max(a, b) for a, b in zip(r["floors"], below)]
        if not table or floors != table[-1]["floors"]:
            table.append({"min_gpu_w": start, "floors": floors})
        below = floors
        start = max(start + 5, int(r["gpu_w"] // 5 * 5))
    return table


class Tuner:
    """The stepping logic. `machine` provides now(), wait(seconds) -> cancelled, sample(),
    set_floors(floors), start_load(level) and stop_load(); tests pass a fake one."""

    def __init__(self, machine, levels, ladder, report, writer, *, step_s=180, judge_s=120,
                 warmup_s=120, period=PERIOD, threshold=STALL_LIMIT, limits=LIMITS, max_s=None,
                 clock=lambda: time.strftime("%H:%M:%S")):
        if not period <= judge_s <= step_s:
            raise ValueError("The judged part must be at least one sample and at most the step")
        self.machine, self.levels, self.ladder = machine, levels, check_ladder(ladder)
        self.report, self.writer, self.clock = report, writer, clock
        self.step_s, self.judge_s, self.warmup_s, self.period = step_s, judge_s, warmup_s, period
        self.threshold, self.limits, self.max_s = threshold, limits, max_s
        self.results, self.steps = [], []
        self.missing = self.step_no = 0
        self.started = None
        self.level_steps = 0
        self.level_index = 0

    def tick(self):
        """Wait a period, then sample; raise on cancel or any hard stop."""
        if self.machine.wait(self.period):
            raise Cancelled()
        s = self.machine.sample()
        reason = safety_reason(s, self.limits)
        if reason:
            raise SafetyStop(reason)
        if any(s.get(k) is None for k in self.limits):
            self.missing += 1
            if self.missing >= MISSING_LIMIT:
                raise SafetyStop("A temperature reading is unavailable")
        else:
            self.missing = 0
        if self.max_s is not None and self.machine.now() - self.started > self.max_s:
            raise SafetyStop("Auto-tune ran longer than %d minutes" % (self.max_s // 60))
        return s

    def record(self, s, level, floors, phase, elapsed, label):
        row = dict(t=self.clock(), level=self.level_index, target_gpu_w=level.gpu_w,
                   cpu_threads=level.cpu_threads, step=self.step_no,
                   cpu_floor=floors[0], gpu_floor=floors[1], mid_floor=floors[2], phase=phase,
                   elapsed_s=round(elapsed, 1),
                   stalls_per_s=None if s["stalls_per_s"] is None else round(s["stalls_per_s"], 2),
                   usb_drops=s["usb_drops"],
                   **{k: None if s.get(k) is None else round(s[k], 1)
                      for k in ("cpu_w", "gpu_w", "cpu_c", "gpu_c", "board_c")},
                   **{f + "_rpm": s.get(f + "_rpm") for f in FANS})
        self.writer.writerow([row[c] for c in COLUMNS])
        within = min(0.95, (max(0, self.level_steps - 1) + min(1, elapsed / self.step_s)) / 4)
        drops = "--" if s["usb_drops"] is None else s["usb_drops"]
        self.report("sample", "%s · %s · %.0f s · %s stalls/s · %s USB drops" % (
            self.level_label(level), label, elapsed, row["stalls_per_s"], drops),
            row=row, elapsed=elapsed, phase=phase,
            run_s=round(self.machine.now() - self.started, 1),
            progress=round((self.level_index - 1 + within) / len(self.levels), 3))

    def level_label(self, level):
        return "Level %d of %d (GPU %.0f W)" % (self.level_index, len(self.levels), level.gpu_w)

    def hold(self, level, floors, seconds, phase):
        self.machine.set_floors(floors)
        start = self.machine.now()
        while True:
            s = self.tick()
            elapsed = self.machine.now() - start
            self.record(s, level, floors, phase, elapsed, "warming up at %d/%d/%d %%" % floors)
            if elapsed >= seconds:
                return

    def step(self, level, rung, watts):
        floors = self.ladder[rung]
        self.step_no += 1
        self.level_steps += 1
        label = "%d/%d/%d %%" % floors
        self.report("step", "%s · fans %s" % (self.level_label(level), label), level=self.level_index,
                    index=self.step_no, rung=rung, floors=list(floors))
        self.machine.set_floors(floors)
        start = self.machine.now()
        expected = self.judge_s / self.period
        window = []
        early = False
        while True:
            s = self.tick()
            elapsed = self.machine.now() - start
            judged = elapsed > self.step_s - self.judge_s
            self.record(s, level, floors, "judge" if judged else "settle", elapsed, label)
            if judged:
                window.append(s)
                watts.append(s.get("gpu_w"))
                if stall_certain(window, expected, self.threshold):
                    early = True
                    break
            if elapsed >= self.step_s:
                break
        verdict = judge(window, self.threshold)
        verdict.update(name="Level %d · %s" % (self.level_index, label), level=self.level_index,
                       step=self.step_no, rung=rung, floors=list(floors), early=early,
                       watts=mean(r.get("gpu_w") for r in window), cpu_w=mean(r.get("cpu_w") for r in window),
                       board_c=window[-1].get("board_c") if window else None)
        self.steps.append(verdict)
        self.report("result", "%s · %s · mean %s · peak %s stalls/s · %s USB drops%s" % (
            verdict["name"], "stalls" if verdict["stalled"] else "clean",
            fmt(verdict["mean"]), fmt(verdict["peak"]), verdict["usb_drops"],
            " (decided early)" if early else ""), **verdict)
        return verdict

    def level(self, level, start):
        self.level_steps = 0
        self.report("level", "%s, CPU %d threads" % (self.level_label(level), level.cpu_threads),
                    index=self.level_index, total=len(self.levels), gpu_w=level.gpu_w,
                    cpu_threads=level.cpu_threads)
        self.machine.start_load(level)
        if self.level_index == 1 and self.warmup_s:
            self.hold(level, self.ladder[start], self.warmup_s, "warmup")
        watts = []
        clean = None
        rung = start
        while rung < len(self.ladder):
            if self.step(level, rung, watts)["stalled"]:
                break
            clean = rung
            rung += 1
        if clean is None:
            # Even the starting combination stalls at this level: go louder until one is clean.
            for rung in range(start - 1, -1, -1):
                if not self.step(level, rung, watts)["stalled"]:
                    clean = rung
                    break
        floors = self.ladder[clean] if clean is not None else (100, 100, 100)
        # The safe curve between levels: back to the last clean combination at once.
        self.machine.set_floors(floors)
        self.machine.stop_load()
        measured = mean(watts)
        result = dict(level=self.level_index, target_gpu_w=level.gpu_w, cpu_threads=level.cpu_threads,
                      gpu_w=round(measured if measured is not None else level.gpu_w, 1),
                      floors=list(floors), rung=clean, clean=clean is not None, steps=self.level_steps)
        self.results.append(result)
        self.report("level-result", "%s: %s" % (
            self.level_label(level), "quietest clean fans %d/%d/%d %%" % floors if clean is not None
            else "not clean even at the loudest combination tried; table uses 100/100/100 %"), **result)
        return result

    def run(self):
        self.started = self.machine.now()
        start = 0
        for index, level in enumerate(self.levels, 1):
            self.level_index = index
            result = self.level(level, start)
            start = result["rung"] if result["clean"] else 0
        return build_table(self.results)


def fmt(value):
    return "--" if value is None else "%.2f" % value


def mains_online(root=Path("/sys/class/power_supply")):
    """True when a mains adapter is online, False when all are offline, None when none is found."""
    found = None
    for supply in sorted(root.glob("*")):
        if cal.rd(str(supply / "type")) == "Mains":
            online = cal.rd(str(supply / "online"))
            if online == "1":
                return True
            if online == "0":
                found = False
    return found


def die_with_parent():
    """Child processes get SIGTERM if this process dies, so load never outlives a crash."""
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def usb_sink(name):
    """PipeWire object serial of the USB audio interface's sink, matched by ALSA card name."""
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=15, check=True).stdout
    for obj in json.loads(out):
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("media.class") == "Audio/Sink" and props.get("alsa.card_name") == name:
            return str(props.get("object.serial"))
    return None


class Hardware:
    """The real machine: fan curves through asusctl, sensors, the stall counter, the USB link and
    the two load generators. Single-threaded on purpose (children are started with preexec_fn)."""

    def __init__(self, stop, saved=None, fans=None, *, counter=True, usb_name="Volt 2", limits=LIMITS,
                 bound_s=4 * 3600):
        self.stop, self.saved, self.fans = stop, saved, fans
        self.counter, self.usb_name, self.limits, self.bound_s = counter, usb_name, limits, bound_s
        self.changed = False
        self.curves = None
        self.player = self.silence = None
        self.loads = []
        self.irq = None
        self.status_t = None
        self.drops = 0
        self.cpu_w = None
        self.quiet = 0
        self.workdir = tempfile.TemporaryDirectory(prefix="rog-autotune-")
        self.loadgen = None

    # -- setup and teardown -----------------------------------------------------------------
    def build_loadgen(self):
        self.loadgen = Path(self.workdir.name) / "loadgen"
        try:
            subprocess.run(["gcc", "-O2", "-pthread", "-o", str(self.loadgen), str(HERE / "loadgen.c")],
                           check=True, timeout=120, capture_output=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Could not build tools/loadgen.c with gcc: %s" % exc) from None

    def start(self):
        self.build_loadgen()
        if self.counter and not cal.counter_ready():
            self.player = subprocess.Popen(cal.APLAY, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                           preexec_fn=die_with_parent)
            if self.stop.wait(3):
                raise Cancelled()
            if self.player.poll() is not None or not cal.counter_ready():
                raise RuntimeError("Could not start the PCH stall counter")
        if self.usb_name:
            serial = usb_sink(self.usb_name)
            if not serial:
                raise RuntimeError("No PipeWire sink for the %s; connect it or use --no-usb" % self.usb_name)
            argv = ["pw-cat", "--playback", "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
                    "--media-role", "Production", "-P", '{ media.name = "rog-fand auto-tune silence" }',
                    "--target", serial, "-"]
            with open("/dev/zero", "rb") as zero:
                self.silence = subprocess.Popen(argv, stdin=zero, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL, preexec_fn=die_with_parent)
            if self.stop.wait(2):
                raise Cancelled()
            if self.silence.poll() is not None:
                raise RuntimeError("Could not play silence to the %s through PipeWire" % self.usb_name)
        if self.counter:
            self.irq = (cal.hda_irqs(), time.monotonic())

    def close(self):
        try:
            self.stop_load()
        finally:
            for proc in (self.silence, self.player):
                end(proc)
            self.silence = self.player = None
            self.workdir.cleanup()

    # -- load -------------------------------------------------------------------------------
    def start_load(self, level):
        self.stop_load()
        bound = str(int(self.bound_s))
        gpu = [sys.executable, "-I", str(HERE / "gpuload.py"), "--watts", "%g" % level.gpu_w,
               "--seconds", bound, "--max-temp", str(self.limits["gpu_c"]), "--json"]
        self.loads.append(("GPU", self.spawn(gpu, "gpu")))
        if level.cpu_threads:
            self.loads.append(("CPU", self.spawn([str(self.loadgen), "cpu", str(level.cpu_threads), bound], "cpu")))

    def spawn(self, argv, name):
        err = open(Path(self.workdir.name) / (name + ".err"), "w+")
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=err, preexec_fn=die_with_parent)
        except BaseException:
            err.close()
            raise
        proc.err = err
        return proc

    def stop_load(self):
        loads, self.loads = self.loads, []
        for _, proc in loads:
            try:
                end(proc)
            finally:
                proc.err.close()

    def check_loads(self):
        for name, proc in self.loads:
            code = proc.poll()
            if code is None:
                continue
            if name == "GPU" and code == 3:
                raise SafetyStop("GPU reached the %d C limit" % self.limits["gpu_c"])
            proc.err.seek(0)
            detail = proc.err.read().strip().splitlines()
            raise RuntimeError("The %s load generator stopped (exit %s)%s" % (
                name, code, ": " + detail[-1][:200] if detail else ""))

    # -- fans -------------------------------------------------------------------------------
    def set_floors(self, floors):
        curves = floor_curves(self.saved[0], floors)
        previous, self.curves = self.curves, None
        for fan in FANS:
            if self.stop.is_set():
                raise Cancelled()
            if cal.rd(PROFILE) != "performance":
                raise SafetyStop("The Performance profile changed")
            if previous is None or curves[fan] != previous[fan]:
                self.changed = True
                self.fans.set(fan, curves[fan])
        self.fans.command("--enable-fan-curves", "true")
        self.curves = curves

    # -- sampling ---------------------------------------------------------------------------
    def now(self):
        return time.monotonic()

    def wait(self, seconds):
        """Wait, reading the service status often enough that no 2 s sample of it is missed."""
        until = time.monotonic() + seconds
        while True:
            self.poll_status()
            left = until - time.monotonic()
            if left <= 0:
                return self.stop.is_set()
            if self.stop.wait(min(STATUS_POLL, left)):
                return True

    def poll_status(self):
        state, s = core.fan_service()
        if not self.usb_name:
            # CPU power is only reported, so a missing service just leaves it empty.
            self.cpu_w = s["sensors"].get("cpu_w") if s else None
            return
        if state != "Running":
            raise RuntimeError("Lost the fan service status (needed for USB drops and CPU power): %s" % state)
        self.cpu_w = s["sensors"].get("cpu_w")
        if s["t"] == self.status_t:
            return
        if s["usb"].get("error"):
            raise RuntimeError("USB monitoring is unavailable: %s" % s["usb"]["error"])
        self.status_t = s["t"]
        devices = s["usb"]["devices"]
        self.quiet = 0 if any(d.get("packets") for d in devices) else self.quiet + 1
        if self.quiet >= 5:
            raise RuntimeError("No USB audio traffic: is the %s connected?" % self.usb_name)
        # Skips in a stream's first second are start-up artefacts, as in the service's feedback.
        self.drops += sum(max(0, d.get("skipped", 0) - d.get("warmup_skipped", 0)) for d in devices)

    def sample(self):
        rate = None
        if self.counter:
            irq, now = cal.hda_irqs(), time.monotonic()
            if not cal.counter_ready() or (self.player is not None and self.player.poll() is not None):
                raise RuntimeError("Lost the PCH stall counter")
            last = self.irq
            if irq is not None and last and last[0] is not None and now > last[1]:
                rate = max(0.0, (irq - last[0]) / (now - last[1]) - cal.NORMAL)
            self.irq = (irq, now)
        if self.silence is not None and self.silence.poll() is not None:
            raise RuntimeError("The silent stream to the %s stopped" % self.usb_name)
        self.check_loads()
        drops, self.drops = self.drops, 0
        gpu_c, gpu_w = cal.gpu()
        rpm = core.read_fans()
        return dict(stalls_per_s=rate, usb_drops=drops if self.usb_name else None, cpu_w=self.cpu_w,
                    gpu_w=gpu_w, gpu_c=gpu_c, cpu_c=cal.zone("x86_pkg_temp"), board_c=cal.zone("SEN3"),
                    profile=cal.rd(PROFILE), ac=mains_online(), **{f + "_rpm": rpm[f + "_rpm"] for f in FANS})


def end(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


class Reporter:
    def __init__(self, panel_json):
        self.panel_json = panel_json

    def __call__(self, kind, message, **values):
        if self.panel_json:
            print(json.dumps(dict(kind=kind, message=message, **values)), flush=True)
        elif kind != "sample" or values.get("elapsed", 0) % 30 < PERIOD:
            print(message, flush=True)


def atomic_json(path, data):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix="." + path.name + "-",
                                     delete=False) as out:
        json.dump(data, out, indent=1)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(out.name, path)


def preflight(a):
    if not core.HDA_LABEL:
        raise ValueError("The internal PCH sound device is unavailable")
    if "RUNNING" in cal.rd(cal.PCM + "/status", "") and not cal.counter_ready():
        raise ValueError("The internal sound device is busy")
    if cal.rd(PROFILE) != "performance":
        raise ValueError("Select the Performance profile for auto-tune")
    if mains_online() is not True:
        raise ValueError("Connect the power adapter for auto-tune")
    state = core.fan_service()[0]
    if a.usb and state != "Running":
        raise ValueError("Auto-tune reads USB drops from the fan service; it is %s" % state.lower())


def run(a, levels, ladder, stop):
    report = Reporter(a.panel_json)
    preflight(a)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    model_out = Path(a.model_out) if a.model_out else out.with_suffix(".model.json")
    outcome = dict(cancelled=False, safety=None)
    table = None
    with contextlib.ExitStack() as stack:
        stack.enter_context(cal.calibration_lease())
        if cal.rd(PROFILE) != "performance":
            raise ValueError("Select the Performance profile for auto-tune")
        saved = cal.snapshot()
        fans = cal.Fans(False)
        hw = Hardware(stop, saved, fans, usb_name=a.usb or None, bound_s=a.max_minutes * 60 + 600)
        # Keep partial samples private until the CSV can be published atomically.
        fh = stack.enter_context(tempfile.NamedTemporaryFile(mode="w", newline="", dir=out.parent,
                                                              prefix=".autotune-", delete=False))
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        tuner = Tuner(hw, levels, ladder, report, writer, step_s=a.step, judge_s=a.judge,
                      warmup_s=a.warmup, max_s=a.max_minutes * 60)
        try:
            hw.start()
            table = tuner.run()
        except Cancelled:
            outcome["cancelled"] = True
        except SafetyStop as exc:
            outcome["safety"] = str(exc)
            report("safety", "Stopped: %s. Load ended; restoring the saved curves." % exc, reason=str(exc))
        finally:
            try:
                hw.stop_load()
            finally:
                try:
                    if hw.changed:
                        cal.restore(fans, saved)
                        report("restore", "Saved Performance curves and enable flags restored.")
                finally:
                    try:
                        hw.close()
                    finally:
                        fh.flush()
                        os.fsync(fh.fileno())
                        fh.close()
                        os.replace(fh.name, out)
        outcome["cancelled"] = outcome["cancelled"] or stop.is_set()
        if table is not None and not outcome["cancelled"]:
            proposal = dict(version=1, source="tools/autotune.py",
                            created=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            table=table, levels=tuner.results, steps=tuner.steps,
                            ladder=[list(f) for f in tuner.ladder], step_s=a.step, judge_s=a.judge,
                            stall_limit=STALL_LIMIT, log=out.name)
            atomic_json(model_out, proposal)
            report("proposal", "Proposed table saved to %s" % model_out, table=table, levels=tuner.results,
                   path=str(model_out), log=str(out))
        message = ("Cancelled." if outcome["cancelled"] else
                   "Stopped for safety: %s." % outcome["safety"] if outcome["safety"] else "Completed.")
        report("done", message, log=str(out), **outcome)
    return table, tuner, outcome


def check_load(a, levels, stop):
    """Hold each level's load for a while and report the power it reaches. Fans are not touched."""
    report = Reporter(a.panel_json)
    if mains_online() is not True:
        raise ValueError("Connect the power adapter first")
    hw = Hardware(stop, counter=False, usb_name=None)
    try:
        hw.build_loadgen()
        for index, level in enumerate(levels, 1):
            hw.start_load(level)
            start = hw.now()
            rows = []
            while hw.now() - start < a.check_load:
                if hw.wait(PERIOD):
                    report("done", "Cancelled.", cancelled=True)
                    return
                s = hw.sample()
                reason = safety_reason(s, performance=False)
                if reason:
                    report("safety", "Stopped: %s." % reason, reason=reason)
                    return
                rows.append(s)
            hw.stop_load()
            tail = rows[len(rows) // 2:]
            values = dict(target_gpu_w=level.gpu_w, cpu_threads=level.cpu_threads,
                          gpu_w=mean(r["gpu_w"] for r in tail), cpu_w=mean(r["cpu_w"] for r in tail),
                          gpu_c=max((r["gpu_c"] for r in rows if r["gpu_c"] is not None), default=None),
                          cpu_c=max((r["cpu_c"] for r in rows if r["cpu_c"] is not None), default=None))
            report("check", "GPU target %.0f W: %s W measured, CPU %d threads: %s W, max GPU %s C, CPU %s C" % (
                level.gpu_w, fmt(values["gpu_w"]), level.cpu_threads, fmt(values["cpu_w"]),
                values["gpu_c"], values["cpu_c"]), index=index, **values)
        report("done", "Completed.", cancelled=False)
    finally:
        hw.close()


def plan(a, levels, ladder):
    print("Levels (hottest first): " + ", ".join("GPU %g W + %d CPU threads" % (lv.gpu_w, lv.cpu_threads)
                                                  for lv in levels))
    print("Fan ladder (cpu/gpu/middle %), loud to quiet: " + "  ".join("%d/%d/%d" % f for f in ladder))
    print("Each step %d s, judged on the last %d s; first level warms up %d s; stops after %d minutes."
          % (a.step, a.judge, a.warmup, a.max_minutes))
    print("Limits: CPU %(cpu_c)d C, GPU %(gpu_c)d C, board %(board_c)d C; mains power; Performance profile."
          % LIMITS)
    print("Stall rule: mean above %.1f/s on the internal counter, or any USB drop%s." % (
        STALL_LIMIT, "" if a.usb else " (USB not measured: --no-usb)"))
    print("Log: %s" % a.out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--levels", default=LEVELS, help="GPU watts:CPU threads, comma separated (default %(default)s)")
    ap.add_argument("--ladder", help="fan floors loud to quiet, cpu,gpu,mid;... (default: built-in ladder)")
    ap.add_argument("--step", type=int, default=180, help="seconds per fan combination")
    ap.add_argument("--judge", type=int, default=120, help="judge each step on its last N seconds")
    ap.add_argument("--warmup", type=int, default=120, help="seconds of heat before the first step")
    ap.add_argument("--max-minutes", type=int, default=150, help="stop if the run takes longer")
    ap.add_argument("--usb", default="Volt 2", help="ALSA card name of the USB audio interface")
    ap.add_argument("--no-usb", dest="usb", action="store_const", const="", help="do not measure USB drops")
    ap.add_argument("--out", default=str(core.log_dir() / time.strftime("autotune-%Y%m%d-%H%M.csv")))
    ap.add_argument("--model-out", help="proposal JSON (default: beside the CSV, .model.json)")
    ap.add_argument("--panel-json", action="store_true", help="emit progress as JSON lines")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--plan", "--dry-run", action="store_true", help="print the plan; change nothing")
    mode.add_argument("--check-load", type=int, metavar="SECONDS", help="hold each level; fans untouched")
    a = ap.parse_args(argv)
    try:
        levels = parse_levels(a.levels)
        ladder = parse_ladder(a.ladder) if a.ladder else check_ladder(LADDER)
    except ValueError as exc:
        ap.error(str(exc))
    if not PERIOD <= a.judge <= a.step or a.warmup < 0 or a.max_minutes <= 0:
        ap.error("need 2 <= --judge <= --step, --warmup >= 0 and a positive --max-minutes")
    if a.check_load is not None and a.check_load < 2 * PERIOD:
        ap.error("--check-load needs at least 4 seconds")
    if a.plan:
        plan(a, levels, ladder)
        return 0
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    if a.check_load is not None:
        check_load(a, levels, stop)
        return 0
    table, tuner, outcome = run(a, levels, ladder, stop)
    if not a.panel_json:
        print("\nLevel results:")
        for r in tuner.results:
            print("  GPU %5.1f W (target %g), CPU %d threads: %s" % (
                r["gpu_w"], r["target_gpu_w"], r["cpu_threads"],
                "%d/%d/%d %%" % tuple(r["floors"]) if r["clean"] else "never clean, 100/100/100 %"))
        if table:
            print("\nProposed table (service/config.example.json format):")
            print(json.dumps({"table": table}, indent=1))
            print("Review and install it from the panel's Calibrate tab.")
        print("Log:", a.out)
    # 3: stopped by a safety limit (reported, curves restored); errors exit with 1 and a message.
    return 3 if outcome["safety"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
