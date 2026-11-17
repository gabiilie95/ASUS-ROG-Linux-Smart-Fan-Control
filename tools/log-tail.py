#!/usr/bin/env python3
"""Summarise the last N seconds of a stall-temp-log CSV.  usage: log-tail.py <csv> [seconds] [label]"""
import sys, csv
r = list(csv.DictReader(open(sys.argv[1]))); n = int(sys.argv[2]) // 2 if len(sys.argv) > 2 else 30
label = sys.argv[3] if len(sys.argv) > 3 else ""
w = r[-n:]
def a(k): return sum(float(x[k]) for x in w) / len(w)
e = [float(x["err_per_s"]) for x in w]
print(f"{label:34s} {w[0]['t']}-{w[-1]['t']}  stalls/s mean {a('err_per_s'):5.1f} max {max(e):5.1f} | board {a('sen3'):.0f}C CPU {a('cpu'):.0f}C GPU {a('gpu_temp'):.0f}C {a('gpu_w'):.0f}W | fans {a('cpu_fan'):.0f}/{a('gpu_fan'):.0f}/{a('mid_fan'):.0f} rpm | cap {w[-1]['cap']}")
