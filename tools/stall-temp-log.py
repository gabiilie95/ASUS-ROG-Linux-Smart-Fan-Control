#!/usr/bin/env python3
"""Log sound-chip DMA underruns against temperatures and GPU power every 2 s. usage: templog.py <seconds> <csv>"""
import subprocess, sys, time, glob
import os as _os
# The laptop's own sound chip is ALSA card "PCH"; its number changes between boots
# (it was card 0 until 2026-11-16, then card 1), so look it up instead of assuming card 0.
HDA_LABEL = "snd_hda_intel:" + (_os.path.basename(_os.readlink("/proc/asound/PCH"))
                                if _os.path.islink("/proc/asound/PCH") else "card0")
SECS = int(sys.argv[1]); OUT = sys.argv[2]; STEP = 2; PERIOD, RATE = 8192, 48000
zones = {}
for z in glob.glob("/sys/class/thermal/thermal_zone*"):
    try: zones[open(z + "/type").read().strip()] = z + "/temp"
    except Exception: pass
fans = {}
for h in glob.glob("/sys/class/hwmon/hwmon*"):
    try:
        if open(h + "/name").read().strip() == "asus":
            for f in glob.glob(h + "/fan*_input"):
                fans[open(f.replace("_input", "_label")).read().strip()] = f
    except Exception: pass
def rd(p):
    try: return int(open(p).read())
    except Exception: return 0
def hda():
    for line in open("/proc/interrupts"):
        if HDA_LABEL in line:
            return sum(int(x) for x in line.split()[1:] if x.isdigit())
    return 0
def thr(): return rd("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")
ap = subprocess.Popen(["aplay", "-q", "-D", "hw:CARD=PCH,DEV=0", "-f", "S16_LE", "-r", str(RATE), "-c", "2", f"--period-size={PERIOD}", f"--buffer-size={PERIOD*4}", "-d", str(SECS + 6), "/dev/zero"], stderr=subprocess.DEVNULL)
time.sleep(1)
if ap.poll() is not None: print("aplay failed (internal sound device busy)"); sys.exit(1)
rows = []; ph = hda(); pt = thr(); t0 = time.time()
with open(OUT, "w") as f:
    f.write("t,err_per_s,sen3,cpu,gpu_temp,gpu_w,gpu_util,cpu_fan,gpu_fan,throttle_per_s,cap,mid_fan\n")
    n = SECS // STEP
    for i in range(n):
        g = subprocess.run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip().split(", ")
        time.sleep(max(0, t0 + (i + 1) * STEP - time.time()))
        h = hda(); t = thr()
        err = (h - ph) / STEP - RATE / PERIOD; th = (t - pt) / STEP; ph, pt = h, t
        try: gt, gw, gu = float(g[0]), float(g[1]), float(g[2])
        except Exception: gt = gw = gu = -1
        row = (time.strftime("%H:%M:%S"), round(err, 1), rd(zones.get("SEN3", "")) // 1000, rd(zones.get("x86_pkg_temp", "")) // 1000, gt, gw, gu,
               rd(fans.get("cpu_fan", "")), rd(fans.get("gpu_fan", "")), round(th, 1), rd("/sys/devices/system/cpu/intel_pstate/max_perf_pct"), rd(fans.get("mid_fan", "")))
        rows.append(row); f.write(",".join(str(x) for x in row) + "\n"); f.flush()
ap.terminate()
def corr(a, b):
    n = len(a); ma = sum(a) / n; mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    return None if va == 0 or vb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5
e = [r[1] for r in rows]
print(f"{len(rows)} samples over {SECS}s. errors/s min={min(e)} mean={sum(e)/len(e):.1f} max={max(e)}")
for name, idx in (("SEN3 board temp", 2), ("CPU package temp", 3), ("GPU temp", 4), ("GPU watts", 5), ("GPU util", 6), ("throttle events", 9)):
    s = [r[idx] for r in rows]; c = corr(e, s)
    print(f"  {name:18s} range {min(s)}..{max(s)}   r={'n/a' if c is None else format(c, '+.2f')}")
print("mean errors/s by SEN3 temperature:")
by = {}
for r in rows: by.setdefault(r[2], []).append(r[1])
for k in sorted(by): print(f"  {k}°C: {sum(by[k])/len(by[k]):5.1f}/s  ({len(by[k])} samples)")
