#!/bin/bash
# Set the middle fan to a flat PWM value (0-255) from 70 C upward in the Performance profile,
# then log stalls for N seconds.   usage: tools/fan-step.sh <pwm> [seconds]
cd "$(dirname "$0")/.." || exit 1
PWM=$1; SECS=${2:-150}; mid=$(( PWM * 6 / 10 )); (( mid < 84 )) && mid=84
asusctl fan-curve --mod-profile performance --fan mid --data "40c:35,58c:58,62c:71,66c:${mid},70c:${PWM},74c:${PWM},78c:${PWM},88c:${PWM}" >/dev/null
asusctl fan-curve --mod-profile performance --enable-fan-curves true >/dev/null
OUT=data/logs/06-mid-fan-pwm-${PWM}.csv
python3 -I tools/stall-temp-log.py "$SECS" "$OUT" >/dev/null || { echo "logger failed (internal sound device busy?)"; exit 1; }
python3 -I - "$OUT" "$PWM" <<'PY'
import sys, csv
r = list(csv.DictReader(open(sys.argv[1]))); pwm = int(sys.argv[2])
def avg(rows, k): return sum(float(x[k]) for x in rows) / max(1, len(rows))
last = r[-20:]
e = [float(x["err_per_s"]) for x in r]
per30 = [sum(e[i:i+15]) / len(e[i:i+15]) for i in range(0, len(e), 15)]
print(f"pwm {pwm} ({100*pwm//255}%): mid fan {avg(last,'mid_fan'):.0f} rpm | last 40 s: {avg(last,'err_per_s'):5.1f} stalls/s (max {max(float(x['err_per_s']) for x in last):.1f}) | SEN3 {avg(last,'sen3'):.0f}C CPU {avg(last,'cpu'):.0f}C GPU {avg(last,'gpu_temp'):.0f}C {avg(last,'gpu_w'):.0f}W")
print("   stalls/s per 30 s:", " ".join(f"{x:.1f}" for x in per30))
PY
