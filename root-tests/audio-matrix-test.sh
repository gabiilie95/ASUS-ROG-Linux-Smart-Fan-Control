#!/bin/bash
# Changes one chipset-level setting at a time, and under each one counts the DMA underruns that
# the internal sound controller reports while it plays pure silence straight to the driver
# (inaudible, bypasses PipeWire). A line that drops to near 0 names the culprit.
# Every change is put back before the next line and again on exit (also on Ctrl-C).
#
#   sudo bash audio-matrix-test.sh        takes about 4 minutes, asks nothing; run it with the game running
#
# Expect short fps dips during the "CPU capped at 30%" and "service stopped" lines.

set -u
MODE=${1:-run}
if [ "$MODE" != selftest ] && [ "$(id -u)" != 0 ]; then echo "Run this with sudo."; exit 1; fi

UNDO=()
APLAY_PID=
undo_all() {
  local i
  [ -n "$APLAY_PID" ] && kill "$APLAY_PID" 2>/dev/null
  for ((i = ${#UNDO[@]} - 1; i >= 0; i--)); do eval "${UNDO[$i]}" 2>/dev/null; done
  UNDO=()
}
trap 'undo_all' EXIT
trap 'echo; echo "Interrupted, restoring settings."; exit 130' INT TERM

hda_irqs() { awk -v l="snd_hda_intel:$(basename "$(readlink /proc/asound/PCH)")" '$0 ~ l { s = 0; for (i = 2; i <= NF; i++) if ($i ~ /^[0-9]+$/) s += $i; print s }' /proc/interrupts; }
pkgtemp() {
  local z
  for z in /sys/class/thermal/thermal_zone*; do
    if [ "$(cat "$z/type" 2>/dev/null)" = x86_pkg_temp ]; then echo $(( $(cat "$z/temp") / 1000 )); return; fi
  done
  echo "?"
}
# MSR 0x34 = count of System Management Interrupts (firmware stealing the CPU)
smi() { dd if=/dev/cpu/0/msr bs=1 skip=52 count=8 2>/dev/null | od -An -tu8 | tr -d ' \n'; }
gpu() { nvidia-smi --query-gpu=utilization.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null | head -1 | awk -F', ' '{printf "%s%% %.0fW", $1, $2}'; }

measure() {
  local label=$1 a b s0 s1 err smir
  aplay -q -D hw:CARD=PCH,DEV=0 -f S16_LE -r 48000 -c 2 --period-size=8192 --buffer-size=32768 -d 12 /dev/zero 2>/dev/null &
  APLAY_PID=$!
  sleep 1.5
  if ! kill -0 "$APLAY_PID" 2>/dev/null; then
    APLAY_PID=
    printf "  %-46s %s\n" "$label" "n/a (internal sound device busy)"
    return
  fi
  a=$(hda_irqs); s0=$(smi)
  sleep 8
  b=$(hda_irqs); s1=$(smi)
  kill "$APLAY_PID" 2>/dev/null; wait "$APLAY_PID" 2>/dev/null; APLAY_PID=
  err=$(( (b - a - 47) / 8 ))            # 47 = normal period interrupts in 8 s at this buffer size
  (( err < 0 )) && err=0
  if [ -n "$s0" ] && [ -n "$s1" ]; then smir=$(( (s1 - s0) / 8 )); else smir="?"; fi
  printf "  %-46s %5s /s   SMI %4s /s   CPU %s°C   GPU %s\n" "$label" "$err" "$smir" "$(pkgtemp)" "$(gpu)"
  sleep 1
}

if [ "$MODE" = selftest ]; then measure "selftest (no root, nothing changed)"; exit 0; fi

modprobe msr 2>/dev/null
echo "Sound-chip DMA underruns per second under each change (lower is better):"
echo
measure "baseline (nothing changed)"

# --- uncore (ring / memory-controller clock) frequency changes ---
U=$(ls -d /sys/devices/system/cpu/intel_uncore_frequency/package_*_die_* 2>/dev/null | head -1)
if [ -n "$U" ]; then
  omin=$(cat "$U/min_freq_khz"); omax=$(cat "$U/max_freq_khz")
  UNDO+=("echo $omax > $U/max_freq_khz; echo $omin > $U/min_freq_khz")
  echo "$omax" > "$U/min_freq_khz"; sleep 1
  measure "uncore clock pinned high ($((omax / 1000)) MHz)"
  echo "$omin" > "$U/min_freq_khz"; echo 2400000 > "$U/max_freq_khz"; echo 2400000 > "$U/min_freq_khz"; sleep 1
  measure "uncore clock pinned at 2400 MHz"
  echo "$omin" > "$U/min_freq_khz"; echo "$omax" > "$U/max_freq_khz"
fi

# --- CPU idle states ---
if exec 9> /dev/cpu_dma_latency; then
  printf '\0\0\0\0' >&9
  measure "CPU idle states fully off"
  exec 9>&-
fi

# --- PCIe link power management ---
A=/sys/module/pcie_aspm/parameters/policy
if [ -e "$A" ]; then
  cur=$(grep -o '\[[a-z]*\]' "$A" | tr -d '[]')
  if echo performance > "$A" 2>/dev/null; then
    UNDO+=("echo $cur > $A")
    measure "PCIe link power saving off"
    echo "$cur" > "$A"
  else
    printf "  %-46s %s\n" "PCIe link power saving off" "skipped (firmware keeps control of ASPM)"
  fi
fi

# --- sound controller power management ---
H=/sys/module/snd_hda_intel/parameters
D=/sys/bus/pci/devices/0000:00:1f.3/power/control
ops=$(cat $H/power_save); opc=$(cat $H/power_save_controller); od_=$(cat $D)
UNDO+=("echo $ops > $H/power_save; echo $opc > $H/power_save_controller; echo $od_ > $D")
echo 0 > $H/power_save; echo N > $H/power_save_controller; echo on > $D; sleep 1
measure "sound chip power saving off"
echo "$ops" > $H/power_save; echo "$opc" > $H/power_save_controller; echo "$od_" > $D

# --- Intel iGPU (its clock is tied to the sound controller) ---
G=/sys/bus/pci/devices/0000:00:02.0/power/control
if [ -e "$G" ]; then
  og=$(cat $G); UNDO+=("echo $og > $G")
  echo on > $G; sleep 2
  measure "Intel iGPU kept awake"
  echo "$og" > $G
fi

# --- CPU frequency ---
P=/sys/devices/system/cpu/intel_pstate
ont=$(cat $P/no_turbo); omx=$(cat $P/max_perf_pct); omn=$(cat $P/min_perf_pct)
UNDO+=("echo $ont > $P/no_turbo; echo $omx > $P/max_perf_pct; echo $omn > $P/min_perf_pct")
echo 1 > $P/no_turbo; sleep 1
measure "turbo off"
echo "$ont" > $P/no_turbo
echo 1 > $P/min_perf_pct; echo 30 > $P/max_perf_pct; sleep 3
measure "CPU capped at 30%"
echo "$omx" > $P/max_perf_pct; echo "$omn" > $P/min_perf_pct

# --- kernel knobs ---
for kv in kernel.split_lock_mitigate=0 kernel.nmi_watchdog=0; do
  k=${kv%=*}; v=${kv#*=}
  o=$(sysctl -n "$k" 2>/dev/null) || continue
  UNDO+=("sysctl -qw $k=$o")
  sysctl -qw "$k=$v"
  measure "$k=$v"
  sysctl -qw "$k=$o"
done

# --- background services that talk to the firmware / embedded controller ---
for s in nvidia-powerd thermald intel_lpmd irqbalance asusd supergfxd; do
  if systemctl is-active -q "$s"; then
    UNDO+=("systemctl start $s")
    systemctl stop "$s"; sleep 2
    measure "service stopped: $s"
    systemctl start "$s"
  fi
done

measure "baseline again"

undo_all
echo
echo "All settings restored:  max_perf_pct=$(cat $P/max_perf_pct)  min_perf_pct=$(cat $P/min_perf_pct)  governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)"
echo "Done. Leave this output in the terminal; Claude can read it."
