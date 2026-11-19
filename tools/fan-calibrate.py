#!/usr/bin/env python3
"""Fan calibration: hold fans at fixed speeds step by step and measure chipset stalls under the
current (light) load. Restores the exact fan curves it found, at the end, on Ctrl+C or on error.

    python3 tools/fan-calibrate.py                 # about 21-35 minutes, including settling
    python3 tools/fan-calibrate.py --step 30 --dry-run   # quick check, changes nothing

Stall counter: extra HD-audio interrupts per second while silence is playing on the PCH sound chip (hw:CARD=PCH,DEV=0) with a
period of 8192 frames. If the settings panel's monitor already holds the device that way it is
reused; otherwise this script starts its own silent player (the laptop speakers are then busy).
No root needed: fan curves are set with asusctl for the Performance profile.
"""
import argparse
import contextlib
import csv
from datetime import datetime, timezone
import fcntl
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ui"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rogcore as core
from fan_analysis import EXTENSION_LIMITS, analyze_step

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PCM = "/proc/asound/PCH/pcm0p/sub0"
FANS = ("cpu", "gpu", "mid")
FLAT_TEMPS = (40, 50, 58, 62, 66, 70, 78, 88)
APLAY = ["aplay", "-q", "-D", "hw:CARD=PCH,DEV=0", "-f", "S16_LE", "-r", "48000", "-c", "2",
         "--period-size=8192", "--buffer-size=32768", "/dev/zero"]
NORMAL = 48000 / 8192

# (label, {fan: percent or None = leave on its saved curve})
STEPS = [
    ("all fans full (reference)",        {"cpu": 100, "gpu": 100, "mid": 100}),
    ("middle 90 %, others on curve",     {"mid": 90}),
    ("middle 80 %, others on curve",     {"mid": 80}),
    ("middle 70 %, others on curve",     {"mid": 70}),
    ("middle 60 %, others on curve",     {"mid": 60}),
    ("middle 60 % + GPU fan full",       {"mid": 60, "gpu": 100}),
    ("middle 60 % + CPU fan full",       {"mid": 60, "cpu": 100}),
]

def rd(p, default=None):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return default

def hwmon(name):
    for d in sorted(os.listdir("/sys/class/hwmon")):
        if rd("/sys/class/hwmon/%s/name" % d) == name:
            return "/sys/class/hwmon/" + d
    return None

def zone(t):
    for d in os.listdir("/sys/class/thermal"):
        if d.startswith("thermal_zone") and rd("/sys/class/thermal/%s/type" % d) == t:
            v = rd("/sys/class/thermal/%s/temp" % d)
            return int(v) / 1000 if v else None
    return None

def hda_irqs():
    with open("/proc/interrupts") as f:
        for line in f:
            if core.HDA_LABEL and core.HDA_LABEL in line:
                return sum(int(x) for x in line.split()[1:] if x.isdigit())
    return None

def counter_ready():
    st, hp = rd(PCM + "/status", ""), rd(PCM + "/hw_params", "")
    return "RUNNING" in st and "period_size: 8192" in hp

def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        t, w = [float(x) for x in out.strip().splitlines()[0].split(",")]
        return t, w
    except Exception:
        return None, None

def read_curves(hc):
    return {f: [(int(rd("%s/pwm%d_auto_point%d_temp" % (hc, i, p))), int(rd("%s/pwm%d_auto_point%d_pwm" % (hc, i, p))))
                for p in range(1, 9)] for i, f in enumerate(FANS, 1)}

def data(points):
    return ",".join("%dc:%d" % tp for tp in points)

class Fans:
    def __init__(self, dry):
        self.dry = dry

    def command(self, *args):
        cmd = ["asusctl", "fan-curve", "--mod-profile", "performance", *args]
        if self.dry:
            print("dry run:", " ".join(cmd))
        else:
            subprocess.run(cmd, check=True, timeout=20, stdout=subprocess.DEVNULL)

    def set(self, fan, points):
        self.command("--fan", fan, "--data", data(points))


def snapshot():
    profiles = core.read_stored_curves()
    if not profiles:
        raise ValueError("Cannot read saved Performance curves and enable flags")
    record = profiles["performance"]
    points = {f: [tuple(p) for p in record[f]["points"]] for f in FANS}
    hc = hwmon("asus_custom_fan_curve")
    if hc and rd("/sys/firmware/acpi/platform_profile") == "performance":
        try:
            points = read_curves(hc)
        except (TypeError, ValueError, OSError):
            pass  # Saved asusd curves are available to the desktop user.
    return points, {f: record[f]["enabled"] for f in FANS}


def restore(fans, saved):
    points, enabled = saved
    errors = []
    for fan in FANS:
        commands = (("--fan", fan, "--data", data(points[fan])),
                    ("--fan", fan, "--enable-fan-curve", str(enabled[fan]).lower()))
        for command in commands:
            try:
                fans.command(*command)
            except (OSError, subprocess.SubprocessError) as exc:
                errors.append(str(exc))
    if errors or (not fans.dry and snapshot() != saved):
        raise RuntimeError("Performance curve restore failed. Check saved curves. " + " ".join(errors))


@contextlib.contextmanager
def calibration_lease():
    # Persist through restore, including if a mode request arrives during the run.
    problem = core.manual_fan_problem()
    if problem:
        raise ValueError(problem)
    try:
        fd = os.open(core.FAND_RUN / "control", os.O_DIRECTORY)
    except FileNotFoundError:
        if core.fan_service()[0] == "Running":
            raise ValueError("Update the fan service to use calibration from the panel")
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Fan control is busy. Retry after the current operation.") from exc
        problem = core.manual_fan_problem(holding_lease=True)
        if problem:
            raise ValueError(problem)
        yield
    finally:
        os.close(fd)


COLUMNS = ("t", "step", "label", "stalls_per_s", "board_c", "cpu_c", "gpu_c", "gpu_w",
           "cpu_rpm", "gpu_rpm", "mid_rpm", "counter_ok", "elapsed_s", "cpu_w", "cpu_w_age_s",
           "cpu_pct", "gpu_pct", "mid_pct", "observe")


def cpu_power():
    """Use fresh service telemetry; unavailable package power is explicitly unknown."""
    state, sample = core.fan_service()
    if state not in ("Running", "Dry run") or not sample:
        return None, None
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(sample["t"])).total_seconds()
        watts = sample["sensors"].get("cpu_w")
        if (type(watts) in (int, float) and math.isfinite(watts) and 0 <= watts <= 1000
                and 0 <= age <= 6):
            return watts, round(age, 3)
    except (KeyError, TypeError, ValueError):
        pass
    return None, None


def analyze_csv(path):
    """Analyze saved samples without opening audio devices or changing fan settings."""
    groups = []
    with open(path, newline="") as source:
        reader = csv.DictReader(source)
        if not {"t", "step", "label"} <= set(reader.fieldnames or []):
            raise ValueError("Calibration CSV needs t, step and label columns")
        previous_step = None
        previous_stamp = elapsed = 0
        for raw in reader:
            step = raw["step"]
            stamp = datetime.strptime(raw["t"], "%H:%M:%S")
            seconds = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
            if step != previous_step:
                groups.append(dict(index=step, name=raw["label"], readings=[], observe=raw.get("observe") == "1"))
                elapsed = 0
            else:
                elapsed += (seconds - previous_stamp) % 86400
            previous_step, previous_stamp = step, seconds
            row = {}
            for key, value in raw.items():
                if key not in ("t", "step", "label"):
                    try:
                        row[key] = float(value) if value else None
                    except (ValueError, TypeError):
                        row[key] = None
            at = row.get("elapsed_s")
            groups[-1]["readings"].append((elapsed if at is None else at, row))
    return [dict(index=g["index"], name=g["name"],
                 analysis=analyze_step(g["readings"], observe=g["observe"])) for g in groups]


def run(a, steps, stop):
    def report(kind, message, **values):
        if a.panel_json:
            print(json.dumps(dict(kind=kind, message=message, **values)), flush=True)
        else:
            print(message, flush=True)

    if not core.HDA_LABEL:
        raise ValueError("The internal PCH sound device is unavailable")
    busy = "RUNNING" in rd(PCM + "/status", "")
    if busy and not counter_ready():
        raise ValueError("The internal sound device is busy")
    fans = Fans(a.dry_run)
    summary = []
    player = None
    changed = False
    out = Path(a.out)
    # Keep partial samples private until the CSV can be published atomically.
    with contextlib.ExitStack() as stack:
        if not a.observe and not a.dry_run:
            stack.enter_context(calibration_lease())
        if not a.observe and rd("/sys/firmware/acpi/platform_profile") != "performance":
            raise ValueError("Select the Performance profile for fan calibration")
        saved = snapshot() if not a.observe else None
        fh = stack.enter_context(tempfile.NamedTemporaryFile(mode="w", newline="", dir=out.parent,
                                                              delete=False))
        tmp = Path(fh.name)
        writer = csv.writer(fh)
        writer.writerow(COLUMNS)
        try:
            if not counter_ready():
                player = subprocess.Popen(APLAY, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                stop.wait(3)
                if not stop.is_set() and (player.poll() is not None or not counter_ready()):
                    raise ValueError("Could not start the PCH stall counter")
            for index, (name, spec) in enumerate(steps, 1):
                if stop.is_set():
                    break
                report("step", name, index=index, total=len(steps), duration_s=a.step)
                if not a.observe:
                    for fan in FANS:
                        if stop.is_set():
                            break
                        if rd("/sys/firmware/acpi/platform_profile") != "performance":
                            raise ValueError("Performance profile changed")
                        changed = True
                        pct = spec.get(fan)
                        pts = saved[0][fan] if pct is None else [(t, round(pct*255/100)) for t in FLAT_TEMPS]
                        fans.set(fan, pts)
                    if stop.is_set():
                        break
                    fans.command("--enable-fan-curves", "true")
                start = last_t = time.monotonic()
                last_i = hda_irqs()
                readings = []
                deadline = a.step
                extra = 0 if a.observe or a.dry_run else getattr(a, "settle_extra", 120)
                limit = a.step + extra
                while not stop.is_set():
                    if time.monotonic() - start >= deadline:
                        assessment = analyze_step(readings, observe=bool(a.observe))
                        if deadline >= limit or not assessment["can_extend"]:
                            break
                        deadline = min(limit, deadline + 30)
                        report("extend", "Temperatures still changing; measuring for up to 30 s longer.",
                               duration_s=deadline)
                    if stop.wait(2):
                        break
                    if not a.observe and rd("/sys/firmware/acpi/platform_profile") != "performance":
                        raise ValueError("Performance profile changed")
                    i, now = hda_irqs(), time.monotonic()
                    ok = counter_ready() and (player is None or player.poll() is None)
                    if not ok:
                        raise ValueError("Lost the PCH stall counter")
                    rate = (max(0, (i-last_i)/(now-last_t)-NORMAL)
                            if i is not None and last_i is not None else None)
                    last_i, last_t = i, now
                    gt, gw = gpu()
                    rpm = core.read_fans()
                    cw, cpu_age = cpu_power()
                    elapsed = time.monotonic()-start
                    row = dict(zip(COLUMNS, [time.strftime("%H:%M:%S"), index, name,
                               None if rate is None else round(rate, 2), zone("SEN3"),
                               zone("x86_pkg_temp"), gt, gw, *[rpm[f+"_rpm"] for f in FANS], int(ok),
                               round(elapsed, 3), cw, cpu_age, *[spec.get(f) for f in FANS], int(bool(a.observe))]))
                    writer.writerow(row.values())
                    readings.append((elapsed, row))
                    report("sample", f"{name} · {elapsed:.0f} s · {row['stalls_per_s']} stalls/s",
                           row=row, elapsed=elapsed, duration_s=deadline)
                    if elapsed > a.step and (any(row[key] is None or row[key] >= limit
                            for key, limit in EXTENSION_LIMITS.items())
                            or rate is None or rate > 1):
                        break  # Extra observation is useful only while temperatures and audio permit it.
                tail = [r for t, r in readings if t >= 60] or [r for _, r in readings]
                rates = [r["stalls_per_s"] for r in tail if r["stalls_per_s"] is not None]
                watts = [r["gpu_w"] for r in tail if r["gpu_w"] is not None]
                avg = sum(rates)/len(rates) if rates else None
                peak = max(rates) if rates else None
                mean_w = sum(watts)/len(watts) if watts else None
                summary.append((index, name, avg, peak, tail[-1]["board_c"] if tail else None,
                                mean_w, tuple(tail[-1][f+"_rpm"] for f in FANS) if tail else (None,)*3))
                assessment = analyze_step(readings, observe=bool(a.observe))
                if not a.panel_json:
                    report("analysis", assessment["reason"])
                report("result", f"{name} · mean {avg} · peak {peak} stalls/s", index=index,
                       name=name, samples=len(readings), mean=avg, peak=peak, watts=mean_w,
                       analysis=assessment)
        finally:
            try:
                if changed:
                    restore(fans, saved)
                    report("restore", "Saved Performance curves and enable flags restored.")
            finally:
                try:
                    if player and player.poll() is None:
                        player.terminate()
                        try:
                            player.wait(3)
                        except subprocess.TimeoutExpired:
                            player.kill()
                            player.wait()
                finally:
                    fh.flush()
                    os.fsync(fh.fileno())
                    fh.close()
                    os.replace(tmp, out)
        report("done", "Cancelled." if stop.is_set() else "Completed.", cancelled=stop.is_set())
    return summary


def print_summary(a, summary):
    if a.observe:
        print("\nPer minute:")
        print("  minute  stalls/s avg (max)   GPU W   CPU C  GPU C  board C   fans cpu/gpu/mid rpm")
        try:
            with open(a.out) as fh:
                rows = [r for r in csv.DictReader(fh)]
        except OSError:
            rows = []
        for m in range(0, len(rows), 30):
            chunk = rows[m:m + 30]
            v = [float(r["stalls_per_s"]) for r in chunk if r["stalls_per_s"]]
            w = [float(r["gpu_w"]) for r in chunk if r["gpu_w"]]
            num = lambda k: [float(r[k]) for r in chunk if r[k]]
            avg = lambda x: sum(x) / len(x) if x else float("nan")
            print("  %5d   %6.1f (%5.1f)       %5.0f   %5.0f  %5.0f  %6.0f    %s/%s/%s" % (
                m // 30 + 1, avg(v), max(v) if v else float("nan"), avg(w), avg(num("cpu_c")), avg(num("gpu_c")),
                avg(num("board_c")), chunk[-1]["cpu_rpm"], chunk[-1]["gpu_rpm"], chunk[-1]["mid_rpm"]))
        return
    print("\nSummary (average of each step after its first minute):")
    print("  step  stalls/s (max)   fans cpu/gpu/mid rpm   GPU W  board C   setting")
    for si, label, avg, mx, board, gw, rpm in summary:
        print("  %d     %5s (%5s)     %5s/%5s/%5s     %5s   %5s     %s" % (
            si, "--" if avg is None else "%.1f" % avg, "--" if mx is None else "%.1f" % mx,
            rpm[0], rpm[1], rpm[2], "--" if gw is None else "%.0f" % gw, board, label))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--step", type=int, default=180)
    ap.add_argument("--settle-extra", type=int, default=120, metavar="SECONDS",
                    help="maximum extra measurement time per active step (default: 120; 0 disables)")
    ap.add_argument("--analyze", metavar="CSV", help="analyze an existing calibration CSV as JSON; no hardware access")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data/logs/10-fan-calibration-light.csv"))
    ap.add_argument("--steps", help="semicolon-separated settings such as mid=70;cpu=80,gpu=80,mid=80")
    ap.add_argument("--observe", type=int, metavar="SECONDS")
    ap.add_argument("--panel-json", action="store_true", help="emit progress as JSON lines")
    a = ap.parse_args()
    if a.analyze:
        print(json.dumps(analyze_csv(a.analyze), indent=2, allow_nan=False))
        return
    steps = STEPS
    if a.observe is not None:
        a.step = a.observe
        steps = [("Observe current curve", {})]
    elif a.steps is not None:
        steps = core.calibration_steps(a.steps)
    if a.step <= 0:
        ap.error("duration must be positive")
    if not 0 <= a.settle_extra <= 600:
        ap.error("extra settling time must be between 0 and 600 seconds")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    summary = run(a, steps, stop)
    if not a.panel_json:
        print_summary(a, summary)
        print("Log:", a.out)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as exc:
        sys.exit(str(exc))
