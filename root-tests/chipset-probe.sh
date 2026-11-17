#!/bin/bash
# Why does the chipset stall ~84 times a second?  About one minute, run with the game playing:
#
#   sudo bash chipset-probe.sh
#
# Part 1 (read-only, 8 s): samples the CPU's own "why am I being limited" registers about 2000
#   times a second and counts how often each reason switches on, alongside the stall counter.
# Part 2 (4 x 10 s): changes one clock setting at a time (uncore clock pinned, turbo off) and
#   re-counts stalls. Each change is put back straight away and again on exit.
#
# The stall counter is the internal sound chip's DMA-underrun interrupt count while it plays
# silence straight to the driver; the USB capture showed the same events hitting the Volt 2.

set -u
[ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
modprobe msr 2>/dev/null

UNDO=(); APLAY_PID=
undo_all() {
  local i
  [ -n "$APLAY_PID" ] && kill "$APLAY_PID" 2>/dev/null
  for ((i = ${#UNDO[@]} - 1; i >= 0; i--)); do eval "${UNDO[$i]}" 2>/dev/null; done
  UNDO=()
}
trap 'undo_all' EXIT
trap 'echo; echo "Interrupted, restoring settings."; exit 130' INT TERM

hda_irqs() { awk -v l="snd_hda_intel:$(basename "$(readlink /proc/asound/PCH)")" '$0 ~ l { s = 0; for (i = 2; i <= NF; i++) if ($i ~ /^[0-9]+$/) s += $i; print s }' /proc/interrupts; }
temps() {
  local z t o=""
  for z in /sys/class/thermal/thermal_zone*; do
    t=$(cat "$z/type" 2>/dev/null)
    case "$t" in SEN3|x86_pkg_temp) o="$o $t=$(( $(cat "$z/temp") / 1000 ))C" ;; esac
  done
  echo "$o"
}
start_silence() {
  aplay -q -D hw:CARD=PCH,DEV=0 -f S16_LE -r 48000 -c 2 --period-size=8192 --buffer-size=32768 -d "$1" /dev/zero 2>/dev/null &
  APLAY_PID=$!
  sleep 1.5
  kill -0 "$APLAY_PID" 2>/dev/null || { APLAY_PID=; return 1; }
}
stop_silence() { [ -n "$APLAY_PID" ] && { kill "$APLAY_PID" 2>/dev/null; wait "$APLAY_PID" 2>/dev/null; APLAY_PID=; }; }
measure() {
  local label=$1 a b err
  if ! start_silence 12; then printf "  %-40s %s\n" "$label" "n/a (internal sound device busy)"; return; fi
  a=$(hda_irqs); sleep 8; b=$(hda_irqs)
  stop_silence
  err=$(( (b - a - 47) / 8 )); (( err < 0 )) && err=0
  printf "  %-40s %4s stalls/s  %s\n" "$label" "$err" "$(temps)"
  sleep 1
}

echo "== Part 1: what the CPU says is limiting it (8 s, read-only) =="
start_silence 14 || echo "  (internal sound device busy: no stall count for this part)"
A=$(hda_irqs)
python3 -I - <<'EOF'
import os, time, sys
try:
    fd = os.open("/dev/cpu/0/msr", os.O_RDONLY)
except Exception as e:
    print("  cannot open /dev/cpu/0/msr:", e); sys.exit(0)
def rd(msr):
    try: return int.from_bytes(os.pread(fd, 8, msr), "little")
    except Exception: return None
BITS = {0: "PROCHOT (external throttle signal)", 1: "thermal", 4: "residency regulation", 5: "running-average thermal", 6: "VR thermal alert (voltage regulator hot)",
        7: "VR current limit", 8: "electrical/other", 10: "power limit PL1", 11: "power limit PL2", 12: "max turbo limit", 13: "turbo transition attenuation"}
TH = {0: "package thermal status", 2: "PROCHOT#/FORCEPR# asserted", 4: "critical temperature", 6: "threshold 1", 8: "threshold 2", 10: "power-limit notification"}
REG = {"core limit reasons (0x64F)": (0x64F, BITS), "ring/uncore limit reasons (0x690)": (0x690, BITS), "graphics limit reasons (0x6B0)": (0x6B0, BITS), "package thermal status (0x1B1)": (0x1B1, TH)}
pc = rd(0x1FC); tt = rd(0x1A2)
if pc is not None: print(f"  power control 0x1FC = {pc:#x}  -> bidirectional PROCHOT {'ENABLED' if pc & 1 else 'disabled'}")
if tt is not None: print(f"  TjMax = {(tt >> 16) & 0xff} C, TCC offset = {(tt >> 24) & 0x3f} C")
stat = {n: {b: [0, 0] for b in bits} for n, (m, bits) in REG.items()}
last = {n: 0 for n in REG}
ring = []; core = []; smi0 = rd(0x34); n = 0
t0 = time.perf_counter(); DUR = 8.0
while time.perf_counter() - t0 < DUR:
    for name, (m, bits) in REG.items():
        v = rd(m)
        if v is None: continue
        for b in bits:
            on = (v >> b) & 1
            if on:
                stat[name][b][0] += 1
                if not (last[name] >> b) & 1: stat[name][b][1] += 1
        last[name] = v
    r = rd(0x621); c = rd(0x198)
    if r is not None: ring.append(r & 0x7f)
    if c is not None: core.append((c >> 8) & 0xff)
    n += 1
    time.sleep(0.0002)
smi1 = rd(0x34)
print(f"  {n} samples in {DUR:.0f}s ({n/DUR:.0f}/s)")
for name, (m, bits) in REG.items():
    v = rd(m)
    if v is None: print(f"  {name}: not readable"); continue
    active = [(bits[b], s[0], s[1]) for b, s in stat[name].items() if s[0] or s[1]]
    logged = [bits[b] for b in bits if (v >> (b + (16 if m != 0x1B1 else 1))) & 1]
    print(f"  {name}:")
    if not active: print("      no limit active in any sample")
    for label, cnt, edges in active:
        print(f"      {label:42s} on {100*cnt/n:5.1f}% of the time, switched on {edges/DUR:6.1f} times/s")
    if logged: print(f"      (sticky log since boot: {', '.join(logged)})")
def series(name, s, mult):
    if not s: print(f"  {name}: not readable"); return
    ch = sum(1 for i in range(1, len(s)) if s[i] != s[i-1])
    vals = sorted(set(s))
    print(f"  {name}: min {min(s)*mult} MHz, max {max(s)*mult} MHz, {len(vals)} different values, changed {ch/DUR:.0f} times/s")
series("uncore (ring) clock", ring, 100)
series("CPU 0 core clock", core, 100)
if smi0 is not None and smi1 is not None: print(f"  firmware interrupts (SMI): {(smi1 - smi0)/DUR:.1f} per second")
EOF
B=$(hda_irqs); stop_silence
[ -n "$A" ] && [ -n "$B" ] && echo "  stalls during this window: about $(( (B - A - 55) / 9 )) per second  $(temps)"

echo
echo "== Part 2: change one clock setting at a time =="
measure "baseline"
U=$(ls -d /sys/devices/system/cpu/intel_uncore_frequency/package_*_die_* 2>/dev/null | head -1)
if [ -n "$U" ]; then
  omin=$(cat "$U/min_freq_khz"); omax=$(cat "$U/max_freq_khz")
  UNDO+=("echo $omax > $U/max_freq_khz; echo $omin > $U/min_freq_khz")
  echo "$omax" > "$U/min_freq_khz"; sleep 1
  measure "uncore clock pinned at $((omax / 1000)) MHz"
  echo "$omin" > "$U/min_freq_khz"; echo 3000000 > "$U/max_freq_khz"; echo 3000000 > "$U/min_freq_khz"; sleep 1
  measure "uncore clock pinned at 3000 MHz"
  echo "$omin" > "$U/min_freq_khz"; echo "$omax" > "$U/max_freq_khz"
else
  echo "  (no uncore frequency control on this kernel)"
fi
P=/sys/devices/system/cpu/intel_pstate
ont=$(cat $P/no_turbo); UNDO+=("echo $ont > $P/no_turbo")
echo 1 > $P/no_turbo; sleep 2
measure "turbo off"
echo "$ont" > $P/no_turbo
measure "baseline again"

undo_all
echo
echo "Settings restored. Leave this in the terminal; Claude can read it."
