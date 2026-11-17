#!/usr/bin/env python3
"""Play silence straight to the internal HDA output and count its error interrupts in 50 ms bins.

Every FIFO/DMA error raises an interrupt on the controller's IRQ line, so the IRQ count minus the
expected period interrupts is an un-rate-limited error counter. Other interrupt lines and a few
sysfs values are sampled alongside to see what the errors line up with.
"""
import subprocess, sys, time, re, glob
import os as _os
# The laptop's own sound chip is ALSA card "PCH"; its number changes between boots
# (it was card 0 until 2026-11-16, then card 1), so look it up instead of assuming card 0.
HDA_LABEL = "snd_hda_intel:" + (_os.path.basename(_os.readlink("/proc/asound/PCH"))
                                if _os.path.islink("/proc/asound/PCH") else "card0")

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 20
LABEL = sys.argv[2] if len(sys.argv) > 2 else ""
BIN = 0.05
PERIOD = 8192
RATE = 48000

def read_irqs():
    out = {}
    with open("/proc/interrupts") as f:
        ncpu = len(f.readline().split())
        for line in f:
            p = line.split()
            if not p:
                continue
            key = p[0].rstrip(":")
            nums = []
            for x in p[1:1 + ncpu]:
                if x.isdigit():
                    nums.append(int(x))
                else:
                    break
            name = " ".join(p[1 + len(nums):])
            tot = sum(nums)
            if HDA_LABEL in name: out["hda"] = tot
            elif name.endswith("nvidia"): out["nvidia_irq"] = out.get("nvidia_irq", 0) + tot
            elif "xhci_hcd" in name: out["xhci_irq"] = out.get("xhci_irq", 0) + tot
            elif key in ("TRM", "THR", "NMI", "PMI", "MCP", "RES", "CAL", "TLB", "LOC", "IWI"): out[key] = tot
            elif "nvme" in name: out["nvme_irq"] = out.get("nvme_irq", 0) + tot
            elif "i915" in name or "xe" == name.split()[-1:] : out["igpu_irq"] = out.get("igpu_irq", 0) + tot
    return out

def rd(path, default=0):
    try:
        return int(open(path).read())
    except Exception:
        return default

zones = {}
for z in glob.glob("/sys/class/thermal/thermal_zone*"):
    try:
        zones[open(z + "/type").read().strip()] = z + "/temp"
    except Exception:
        pass

p = subprocess.Popen(["aplay", "-q", "-D", "hw:CARD=PCH,DEV=0", "-f", "S16_LE", "-r", str(RATE), "-c", "2",
                      f"--period-size={PERIOD}", f"--buffer-size={PERIOD*4}", "-d", str(int(SECS) + 2), "/dev/zero"],
                     stderr=subprocess.PIPE)
time.sleep(0.8)
if p.poll() is not None:
    print("aplay failed:", p.stderr.read().decode()[:200]); sys.exit(1)

rows = []
t0 = time.perf_counter(); nxt = t0
prev = read_irqs(); prev_thr = rd("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")
while True:
    nxt += BIN
    d = nxt - time.perf_counter()
    if d > 0:
        time.sleep(d)
    now = time.perf_counter()
    if now - t0 > SECS:
        break
    cur = read_irqs(); thr = rd("/sys/devices/system/cpu/cpu0/thermal_throttle/package_throttle_count")
    row = {k: cur.get(k, 0) - prev.get(k, 0) for k in cur}
    row["pkg_throttle"] = thr - prev_thr
    rows.append(row); prev = cur; prev_thr = thr
p.terminate()

n = len(rows)
dur = n * BIN
hda = [r.get("hda", 0) for r in rows]
expected = RATE / PERIOD * dur
err_total = max(0, sum(hda) - expected)
print(f"[{LABEL}] {dur:.1f}s  HDA irq total={sum(hda)}  period irqs expected={expected:.0f}  => DMA/FIFO errors ~{err_total:.0f} ({err_total/dur:.1f}/s)")
temps = "  ".join(f"{k}={rd(v)//1000}C" for k, v in sorted(zones.items()) if k in ("x86_pkg_temp", "SEN3", "TCPU", "TCPU_PCI"))
print(f"   temps: {temps}   max_perf_pct={rd('/sys/devices/system/cpu/intel_pstate/max_perf_pct')}  governor={open('/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor').read().strip()}  platform={open('/sys/firmware/acpi/platform_profile').read().strip()}")
# burst structure
zero = sum(1 for x in hda if x == 0); small = sum(1 for x in hda if 1 <= x <= 3); mid = sum(1 for x in hda if 4 <= x <= 10); big = sum(1 for x in hda if x > 10)
longest_quiet = cur_q = 0
for x in hda:
    cur_q = cur_q + 1 if x <= 1 else 0
    longest_quiet = max(longest_quiet, cur_q)
print(f"   50ms bins: quiet(0)={zero}  1-3={small}  4-10={mid}  >10={big}  max in one bin={max(hda)}  longest quiet stretch={longest_quiet*BIN:.2f}s")
# per-second profile
per_sec = [sum(hda[i:i + 20]) for i in range(0, n, 20)]
print("   errors per second:", " ".join(str(x) for x in per_sec))

def corr(a, b):
    m = len(a); ma = sum(a) / m; mb = sum(b) / m
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    if va == 0 or vb == 0:
        return None
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5

keys = sorted(k for k in rows[0] if k != "hda")
parts = []
for k in keys:
    s = [r.get(k, 0) for r in rows]
    c = corr(hda, s)
    parts.append(f"{k}: {sum(s)/dur:.0f}/s r={'n/a' if c is None else format(c, '+.2f')}")
print("   other lines (rate, correlation with HDA errors per 50ms bin):")
for i in range(0, len(parts), 4):
    print("     " + "   ".join(parts[i:i + 4]))
