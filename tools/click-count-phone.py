#!/usr/bin/env python3
"""Count short broadband transients (clicks) in a mono 16-bit 48 kHz WAV."""
import sys, wave, array, statistics
def run(path):
    w = wave.open(path, "rb"); sr = w.getframerate(); n = w.getnframes()
    a = array.array("h"); a.frombytes(w.readframes(n)); w.close()
    # second difference = crude high-pass that keeps mostly >8 kHz content
    hp = [0.0] * n
    for i in range(2, n): hp[i] = a[i] - 2 * a[i - 1] + a[i - 2]
    F = sr // 2000                      # 0.5 ms frames
    nf = n // F
    e = [0.0] * nf
    for f in range(nf):
        s = 0.0; b = f * F
        for i in range(b, b + F): s += hp[i] * hp[i]
        e[f] = (s / F) ** 0.5
    W = 60                              # +-30 ms local background
    events = []; f = W
    floor = statistics.median(e) * 0.5 + 1e-9
    while f < nf - W:
        loc = sorted(e[f - W:f - 4] + e[f + 5:f + W]); med = loc[len(loc) // 2]
        if e[f] > 5 * max(med, floor) and e[f] >= max(e[f - 3:f + 4]):
            # width: frames around the peak still above half the peak
            l = f; r = f
            while l > 0 and e[l - 1] > e[f] * 0.5: l -= 1
            while r < nf - 1 and e[r + 1] > e[f] * 0.5: r += 1
            if (r - l + 1) <= 6:        # shorter than 3 ms: a click, not a musical onset
                events.append((f * F / sr, e[f] / max(med, floor), (r - l + 1) * 0.5))
            f = r + 4
        else:
            f += 1
    dur = n / sr
    print(f"{path.split('/')[-1]}: {dur:.1f}s  clicks detected: {len(events)}  ({len(events)/dur:.1f}/s)")
    if len(events) > 2:
        iv = [(events[i + 1][0] - events[i][0]) * 1000 for i in range(len(events) - 1)]
        s = sorted(iv)
        print(f"  gaps between clicks (ms): min={s[0]:.0f} p25={s[len(s)//4]:.0f} median={s[len(s)//2]:.0f} p75={s[3*len(s)//4]:.0f} max={s[-1]:.0f}")
        hist = {}
        for x in iv:
            k = "<5" if x < 5 else "5-10" if x < 10 else "10-20" if x < 20 else "20-50" if x < 50 else "50-100" if x < 100 else "100-300" if x < 300 else ">300"
            hist[k] = hist.get(k, 0) + 1
        print("  gap histogram:", "  ".join(f"{k}ms:{hist.get(k,0)}" for k in ("<5", "5-10", "10-20", "20-50", "50-100", "100-300", ">300")))
        per = [0] * (int(dur) + 1)
        for t, _, _ in events: per[int(t)] += 1
        print("  clicks per second:", " ".join(map(str, per)))
        st = sorted(x[1] for x in events); wd = sorted(x[2] for x in events)
        print(f"  strength over local background: median x{st[len(st)//2]:.0f}, max x{st[-1]:.0f};  width median {wd[len(wd)//2]:.1f} ms")
for p in sys.argv[1:]: run(p)
