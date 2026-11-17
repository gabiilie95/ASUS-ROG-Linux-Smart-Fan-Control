#!/usr/bin/env python3
"""60 s: per-second HDA error interrupts vs GPU power/util and other interrupt lines."""
import subprocess, time, sys
import os as _os
# The laptop's own sound chip is ALSA card "PCH"; its number changes between boots
# (it was card 0 until 2026-11-16, then card 1), so look it up instead of assuming card 0.
HDA_LABEL = "snd_hda_intel:" + (_os.path.basename(_os.readlink("/proc/asound/PCH"))
                                if _os.path.islink("/proc/asound/PCH") else "card0")
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 60
PERIOD, RATE = 8192, 48000
def irqs():
    out = {}
    with open("/proc/interrupts") as f:
        ncpu = len(f.readline().split())
        for line in f:
            p = line.split()
            if not p: continue
            nums = []
            for x in p[1:1 + ncpu]:
                if x.isdigit(): nums.append(int(x))
                else: break
            name = " ".join(p[1 + len(nums):]); tot = sum(nums); key = p[0].rstrip(":")
            if HDA_LABEL in name: out["hda"] = tot
            elif name.endswith("nvidia"): out["nvidia_irq"] = out.get("nvidia_irq", 0) + tot
            elif "xhci_hcd" in name: out["xhci_irq"] = out.get("xhci_irq", 0) + tot
            elif "nvme" in name: out["nvme_irq"] = out.get("nvme_irq", 0) + tot
            elif "i915" in name: out["i915_irq"] = out.get("i915_irq", 0) + tot
            elif key in ("CAL", "TLB", "RES", "LOC", "NMI", "IWI"): out[key] = tot
            elif any(s in name for s in ("iwlwifi", "enp", "eth", "r8169")): out["net_irq"] = out.get("net_irq", 0) + tot
    return out
def stat():
    p = open("/proc/stat").readline().split()[1:8]; v = list(map(int, p)); return sum(v), v[3] + v[4]
ap = subprocess.Popen(["aplay", "-q", "-D", "hw:CARD=PCH,DEV=0", "-f", "S16_LE", "-r", str(RATE), "-c", "2", f"--period-size={PERIOD}", f"--buffer-size={PERIOD*4}", "-d", str(SECS + 4), "/dev/zero"], stderr=subprocess.DEVNULL)
time.sleep(1)
if ap.poll() is not None: print("aplay failed"); sys.exit(1)
rows = []; prev = irqs(); pt, pi = stat(); t0 = time.time()
for s in range(SECS):
    g = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,power.draw,utilization.memory,memory.used", "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout.strip().split(", ")
    time.sleep(max(0, t0 + s + 1 - time.time()))
    cur = irqs(); t, i = stat()
    r = {k: cur.get(k, 0) - prev.get(k, 0) for k in cur}
    r["hda"] = r.get("hda", 0) - RATE / PERIOD
    r["cpu_busy%"] = 100 * (1 - (i - pi) / max(1, t - pt))
    try: r["gpu_util%"], r["gpu_W"], r["gpu_membus%"], r["vram_MiB"] = float(g[0]), float(g[1]), float(g[2]), float(g[3])
    except Exception: pass
    rows.append(r); prev = cur; pt, pi = t, i
ap.terminate()
def corr(a, b):
    n = len(a); ma = sum(a) / n; mb = sum(b) / n
    va = sum((x - ma) ** 2 for x in a); vb = sum((y - mb) ** 2 for y in b)
    return None if va == 0 or vb == 0 else sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (va * vb) ** 0.5
h = [r["hda"] for r in rows]
print(f"HDA errors/s over {SECS}s: min={min(h):.0f} mean={sum(h)/len(h):.0f} max={max(h):.0f}")
print("per second:", " ".join(f"{x:.0f}" for x in h))
if "gpu_W" in rows[0]: print("GPU watts: ", " ".join(f"{r.get('gpu_W',0):.0f}" for r in rows))
res = []
for k in sorted(rows[0]):
    if k == "hda": continue
    s = [r.get(k, 0) for r in rows]; c = corr(h, s)
    res.append((abs(c) if c is not None else -1, k, c, min(s), sum(s) / len(s), max(s)))
print("correlation with HDA errors (1 s bins), strongest first:")
for _, k, c, lo, me, hi in sorted(res, reverse=True):
    print(f"  {k:12s} r={'n/a ' if c is None else format(c, '+.2f')}   min={lo:.0f} mean={me:.0f} max={hi:.0f}")
