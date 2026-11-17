#!/usr/bin/env python3
"""Find discontinuity clicks in a digital mono 16-bit 48 kHz capture.
A dropped or repeated chunk of samples is flat right up to 24 kHz, where real programme material
has almost nothing, so look for 0.25 ms spikes in a 4th-difference (top-octave) residual."""
import sys, wave, array
def run(path):
    w = wave.open(path, "rb"); sr = w.getframerate(); n = w.getnframes()
    a = array.array("h"); a.frombytes(w.readframes(n)); w.close()
    peak = max(abs(x) for x in a) if n else 0
    hp = [0.0] * n
    for i in range(4, n): hp[i] = a[i] - 4 * a[i-1] + 6 * a[i-2] - 4 * a[i-3] + a[i-4]
    F = 12; nf = n // F                       # 0.25 ms frames
    e = [0.0] * nf
    for f in range(nf):
        b = f * F; e[f] = max(abs(hp[i]) for i in range(b, b + F))
    # signal level per frame (to know whether audio was playing)
    lvl = [0] * nf
    for f in range(nf):
        b = f * F; lvl[f] = max(abs(a[i]) for i in range(b, b + F))
    W = 200                                   # +-50 ms background
    ev = []; f = W
    while f < nf - W:
        loc = sorted(e[f - W:f - 8] + e[f + 9:f + W]); med = loc[len(loc) // 2]; p90 = loc[int(len(loc) * 0.9)]
        if e[f] > 8 * max(med, 4.0) and e[f] > 3 * max(p90, 4.0) and e[f] >= max(e[f - 4:f + 5]):
            ev.append((f * F / sr, e[f], e[f] / max(med, 4.0), lvl[f])); f += 8
        else: f += 1
    dur = n / sr
    active = sum(1 for x in lvl if x > 200) / max(1, nf)
    print(f"{path.split('/')[-1]:10s} {dur:5.1f}s  peak={peak/32768:.3f}  audio present {active*100:3.0f}% of the time   top-octave spikes: {len(ev)} ({len(ev)/dur:.2f}/s)")
    if ev:
        per = [0] * (int(dur) + 1)
        for t, *_ in ev: per[int(t)] += 1
        print("    per second:", " ".join(map(str, per)))
        for t, amp, ratio, lv in sorted(ev, key=lambda x: -x[2])[:6]:
            i = int(t * sr); seg = " ".join(str(a[j]) for j in range(max(0, i - 4), min(n, i + 14)))
            print(f"    t={t:6.3f}s  x{ratio:5.0f} over background, signal level {lv}:  {seg}")
for p in sys.argv[1:]: run(p)
