#!/usr/bin/env python3
"""Fan calibration: hold fans at fixed speeds step by step and measure chipset stalls under the
current (light) load. Restores the exact fan curves it found, at the end, on Ctrl+C or on error.

    python3 tools/fan-calibrate.py                 # about 20 minutes, writes data/logs/10-...csv
    python3 tools/fan-calibrate.py --step 30 --dry-run   # quick check, changes nothing

Stall counter: extra HD-audio interrupts per second while silence is playing on the PCH sound chip (hw:CARD=PCH,DEV=0) with a
period of 8192 frames. If the settings panel's monitor already holds the device that way it is
reused; otherwise this script starts its own silent player (the laptop speakers are then busy).
No root needed: fan curves are set with asusctl for the Performance profile.
"""
import argparse, csv, os, signal, subprocess, sys, time
import os as _os
# The laptop's own sound chip is ALSA card "PCH"; its number changes between boots
# (it was card 0 until 2026-11-16, then card 1), so look it up instead of assuming card 0.
HDA_LABEL = "snd_hda_intel:" + (_os.path.basename(_os.readlink("/proc/asound/PCH"))
                                if _os.path.islink("/proc/asound/PCH") else "card0")

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
            if HDA_LABEL in line:
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
    def set(self, fan, points):
        cmd = ["asusctl", "fan-curve", "--mod-profile", "performance", "--fan", fan, "--data", data(points)]
        if self.dry:
            print("    dry run:", " ".join(cmd)); return
        subprocess.run(cmd, capture_output=True, timeout=20)
    def enable(self):
        cmd = ["asusctl", "fan-curve", "--mod-profile", "performance", "--enable-fan-curves", "true"]
        if not self.dry:
            subprocess.run(cmd, capture_output=True, timeout=20)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=180, help="seconds per step (default 180)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=os.path.join(ROOT, "data/logs/10-fan-calibration-light.csv"))
    ap.add_argument("--steps", help='custom steps, e.g. "mid=60,cpu=70,gpu=70; mid=50,cpu=70,gpu=70" '
                                    "(percent per fan; fans not named stay on their saved curve)")
    ap.add_argument("--observe", type=int, metavar="SECONDS",
                    help="change nothing: log stalls with the current curve for SECONDS, then print a per-minute summary")
    a = ap.parse_args()
    global STEPS
    if a.observe:
        STEPS = [("observe current curve", {})]
        a.step = a.observe
    elif a.steps:
        custom = []
        for part in a.steps.split(";"):
            spec = {}
            for kv in part.split(","):
                if not kv.strip():
                    continue
                k, v = kv.split("=")
                k, v = k.strip(), int(v)
                if k not in FANS or not 0 <= v <= 100:
                    sys.exit("bad step entry: %r (fans: cpu, gpu, mid; percent 0-100)" % kv)
                spec[k] = v
            if spec:
                custom.append((" + ".join("%s %d %%" % (f, spec[f]) for f in FANS if f in spec), spec))
        STEPS = custom

    hc, hf = hwmon("asus_custom_fan_curve"), hwmon("asus")
    if not hc or not hf:
        sys.exit("ASUS fan interfaces not found")
    saved = read_curves(hc)
    fans = Fans(a.dry_run)
    player = None

    def restore(*_):
        for f in FANS:
            fans.set(f, saved[f])
        fans.enable()
        if player and player.poll() is None:
            player.terminate()
            try:
                player.wait(3)
            except subprocess.TimeoutExpired:
                player.kill()
        if not a.dry_run:
            ok = read_curves(hc) == saved
            print("\nYour fan curves are restored%s." % ("" if ok else " (CHECK: live curve differs!)"), flush=True)

    def on_signal(sig, _frm):
        print("\nInterrupted.", flush=True)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, on_signal)
    print("Saved your current Performance curves:")
    for f in FANS:
        print("  %-3s %s" % (f, data(saved[f])))

    if not counter_ready():
        if "RUNNING" in rd(PCM + "/status", ""):
            sys.exit("The internal sound device is in use by something else; cannot count stalls.")
        player = subprocess.Popen(APLAY, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3)
        print("Started a silent player for the stall counter (pid %d)." % player.pid)
    else:
        print("Using the settings panel's running stall monitor.")

    rows, summary = [], []
    try:
        with open(a.out, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["t", "step", "label", "stalls_per_s", "board_c", "cpu_c", "gpu_c", "gpu_w",
                        "cpu_rpm", "gpu_rpm", "mid_rpm", "counter_ok"])
            for si, (label, spec) in enumerate(STEPS, 1):
                print("\nStep %d/%d: %s  (%d s)" % (si, len(STEPS), label, a.step), flush=True)
                for f in FANS:
                    pct = spec.get(f)
                    pts = saved[f] if pct is None else [(t, round(pct * 255 / 100)) for t in FLAT_TEMPS]
                    fans.set(f, pts)
                fans.enable()
                t0 = time.time(); last_i, last_t = hda_irqs(), time.monotonic(); step_rows = []
                while time.time() - t0 < a.step:
                    time.sleep(2)
                    i, now = hda_irqs(), time.monotonic()
                    ok = counter_ready()
                    rate = max(0.0, (i - last_i) / (now - last_t) - NORMAL) if (ok and i is not None and last_i is not None) else None
                    last_i, last_t = i, now
                    gt, gw = gpu()
                    r = [time.strftime("%H:%M:%S"), si, label, None if rate is None else round(rate, 2),
                         zone("SEN3"), zone("x86_pkg_temp"), gt, gw,
                         rd(hf + "/fan1_input"), rd(hf + "/fan2_input"), rd(hf + "/fan3_input"), int(ok)]
                    w.writerow(r); fh.flush(); step_rows.append(r)
                    left = a.step - (time.time() - t0)
                    print("\r  %3.0f s left | stalls %5s/s | board %s C cpu %s C gpu %s W | fans %s/%s/%s rpm   "
                          % (max(0, left), "--" if rate is None else "%.1f" % rate, r[4], r[5],
                             "--" if gw is None else "%.0f" % gw, r[8], r[9], r[10]), end="", flush=True)
                # judge each step only after its first minute (the stalls lag airflow by ~40 s)
                tail = [x for x in step_rows if x[3] is not None][-int(max(1, (a.step - 60) / 2)):]
                vals = [x[3] for x in tail]
                summary.append((si, label, (sum(vals) / len(vals)) if vals else None, max(vals) if vals else None,
                                tail[-1][8:11] if tail else ("?",) * 3,
                                (sum(x[7] for x in tail if x[7]) / max(1, sum(1 for x in tail if x[7]))) if tail else None,
                                tail[-1][4] if tail else None))
                print()
    except KeyboardInterrupt:
        pass
    finally:
        restore()

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
        print("\nLog: " + a.out)
        return
    print("\nSummary (average of each step after its first minute):")
    print("  step  stalls/s (max)   fans cpu/gpu/mid rpm   GPU W  board C   setting")
    for si, label, avg, mx, rpm, gw, board in summary:
        print("  %d     %5s (%5s)     %5s/%5s/%5s     %5s   %5s     %s" % (
            si, "--" if avg is None else "%.1f" % avg, "--" if mx is None else "%.1f" % mx,
            rpm[0], rpm[1], rpm[2], "--" if gw is None else "%.0f" % gw, board, label))
    print("\nLog: " + a.out)

if __name__ == "__main__":
    main()
