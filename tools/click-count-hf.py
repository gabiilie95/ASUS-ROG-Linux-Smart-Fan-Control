#!/usr/bin/env python3
"""Count 0.5 ms energy spikes in an already high-passed (>17.5 kHz) mono 16-bit WAV.
Programme audio from Jellyfin/the game has nothing up there, so spikes in this band are clicks that
were added after the source material was decoded."""
import sys, wave, array
def run(path, label):
    w = wave.open(path, "rb"); sr = w.getframerate(); n = w.getnframes()
    a = array.array("h"); a.frombytes(w.readframes(n)); w.close()
    F = sr // 2000; nf = n // F
    e = [0.0] * nf
    for f in range(nf):
        b = f * F; e[f] = (sum(x * x for x in a[b:b + F]) / F) ** 0.5
    se = sorted(e); floor = se[len(se) // 2]; p99 = se[int(len(se) * 0.99)]
    W = 80; ev = []; f = W
    while f < nf - W:
        loc = sorted(e[f - W:f - 4] + e[f + 5:f + W]); med = loc[len(loc) // 2]
        if e[f] > 6 * max(med, floor, 1.0) and e[f] >= max(e[f - 3:f + 4]):
            ev.append(f * F / sr); f += 6
        else: f += 1
    dur = n / sr; per = [0] * (int(dur) + 1)
    for t in ev: per[int(t)] += 1
    print(f"{label:34s} {dur:5.1f}s  >17.5 kHz floor={floor:6.1f} p99={p99:7.1f}  spikes={len(ev):3d} ({len(ev)/dur:4.1f}/s)  per second: {' '.join(map(str, per))}")
run(sys.argv[1], sys.argv[2])
