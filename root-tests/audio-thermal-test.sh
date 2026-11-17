#!/bin/bash
# Audio crackle vs. CPU thermal throttling on the ROG G18 — test and fix helper.
#
#   sudo bash audio-thermal-test.sh            measure, cap the CPU at 55 %, wait, measure again
#                                              (runtime only: nothing survives a reboot)
#   sudo bash audio-thermal-test.sh who        show which program has rewritten the CPU cap since the test
#   sudo bash audio-thermal-test.sh persist    make the cap part of the "Performance" power profile (tuned)
#   sudo bash audio-thermal-test.sh undo       remove what "persist" installed and stop the watcher
#
# A different cap:  sudo CAP=50 bash audio-thermal-test.sh
#
# What it measures, all without touching PipeWire or the Volt 2:
#   - CPU package temperature
#   - package thermal-throttle events per second and the share of time spent throttled
#   - DMA underruns reported by the internal sound controller while it plays 15 s of pure
#     silence straight to the driver (inaudible; this is the kernel-level glitch counter)

set -u
[ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }

CAP=${CAP:-55}
MODE=${1:-test}
P=/sys/devices/system/cpu/intel_pstate
TT=/sys/devices/system/cpu/cpu0/thermal_throttle
TR=/sys/kernel/tracing/instances/capwatch
PROFILE_DIR=/etc/tuned/profiles/rog-capped
PPD=/etc/tuned/ppd.conf
SECS=15

pkgtemp() {
  local z
  for z in /sys/class/thermal/thermal_zone*; do
    if [ "$(cat "$z/type" 2>/dev/null)" = x86_pkg_temp ]; then echo $(( $(cat "$z/temp") / 1000 )); return; fi
  done
  echo "?"
}

state() {
  echo "  max_perf_pct=$(cat $P/max_perf_pct)  min_perf_pct=$(cat $P/min_perf_pct)  governor=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)  EPP=$(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
  echo "  platform_profile=$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)  tuned: $(tuned-adm active 2>/dev/null | sed 's/Current active profile: //')  package temp=$(pkgtemp)°C"
}

# Sets M_TEMP M_EV M_PCT M_HDA
measure() {
  local a b a2 b2 t0 start rc log logged supp
  a=$(cat $TT/package_throttle_count); b=$(cat $TT/package_throttle_total_time_ms 2>/dev/null || echo 0)
  t0=$(date '+%F %T'); start=$SECONDS
  timeout $((SECS + 5)) aplay -q -D hw:CARD=PCH,DEV=0 -f S16_LE -r 48000 -c 2 --period-size=1024 --buffer-size=8192 -d $SECS /dev/zero 2>/dev/null
  rc=$?
  while (( SECONDS - start < SECS )); do sleep 1; done
  a2=$(cat $TT/package_throttle_count); b2=$(cat $TT/package_throttle_total_time_ms 2>/dev/null || echo 0)
  M_TEMP=$(pkgtemp)
  M_EV=$(( (a2 - a) / SECS ))
  M_PCT=$(( (b2 - b) / (SECS * 10) ))
  sleep 1
  if [ $rc -ne 0 ]; then
    M_HDA="n/a"          # internal sound device was busy, could not run the silent playback
    return
  fi
  log=$(journalctl -k --since "$t0" --no-pager 2>/dev/null)
  logged=$(echo "$log" | grep -c "dma error")
  supp=$(echo "$log" | grep -oE "handle_stream_irq: [0-9]+ callbacks suppressed" | awk '{s+=$2} END{print s+0}')
  # The kernel prints at most 10 of these per 5 s and carries the "suppressed" count of the
  # previous burst into the next one, so only trust it when the limiter was actually saturated.
  if (( logged >= 10 * (SECS / 5) )); then M_HDA=$(( (logged + supp) / SECS )); else M_HDA=$(( logged / SECS )); fi
}

watch_start() {
  [ -d /sys/kernel/tracing/instances ] || return 1
  [ -d "$TR" ] || mkdir "$TR" 2>/dev/null || return 1
  echo 0 > "$TR/tracing_on"
  echo 'store_max_perf_pct store_min_perf_pct' > "$TR/set_ftrace_filter" 2>/dev/null || { rmdir "$TR" 2>/dev/null; return 1; }
  echo function > "$TR/current_tracer" 2>/dev/null || { rmdir "$TR" 2>/dev/null; return 1; }
  echo > "$TR/trace"
  echo 1 > "$TR/tracing_on"
}

watch_report() {
  if [ ! -f "$TR/trace" ]; then echo "  (watcher is not running — run the test first)"; return; fi
  local lines
  lines=$(grep -v '^#' "$TR/trace" | grep -E "store_(max|min)_perf_pct")
  if [ -z "$lines" ]; then echo "  nothing has written the CPU cap since the watcher started"; return; fi
  echo "  writes by program (name-PID, which limit):"
  echo "$lines" | awk '{ for (i = 1; i <= NF; i++) if ($i ~ /^store_(max|min)_perf_pct$/) print "    " $1 "  " $i }' | sort | uniq -c
  echo "  last writes (time is seconds since boot; now = $(cut -d' ' -f1 /proc/uptime)):"
  echo "$lines" | tail -8 | sed 's/^/    /'
}

case "$MODE" in
test)
  echo "== Before =="; state
  watch_start && echo "  (watching for anything that rewrites the CPU cap)"
  echo "  measuring for ${SECS}s ..."
  measure; B_TEMP=$M_TEMP; B_EV=$M_EV; B_PCT=$M_PCT; B_HDA=$M_HDA

  echo; echo "== Capping CPU at ${CAP}% =="
  echo "$CAP" > $P/max_perf_pct
  for i in 45 30 15; do echo "  settling, ${i}s left ... package temp $(pkgtemp)°C"; sleep 15; done

  echo; echo "== After =="; state
  echo "  measuring for ${SECS}s ..."
  measure

  echo
  printf "%-34s %10s %10s\n" "" "before" "after"
  printf "%-34s %9s° %9s°\n" "CPU package temperature" "$B_TEMP" "$M_TEMP"
  printf "%-34s %10s %10s\n" "thermal throttle events / second" "$B_EV" "$M_EV"
  printf "%-34s %9s%% %9s%%\n" "time spent throttled" "$B_PCT" "$M_PCT"
  printf "%-34s %10s %10s\n" "sound-chip DMA underruns / second" "$B_HDA" "$M_HDA"
  echo
  if (( B_EV < 5 )); then
    echo "The CPU was not throttling during the 'before' window, so this run cannot tell. Re-run while the game is running and it crackles."
  elif (( M_EV * 10 > B_EV )); then
    echo "Still throttling at ${CAP}%. Try a lower cap:  sudo CAP=$((CAP - 10)) bash $0"
  elif [ "$M_HDA" = "n/a" ] || [ "$B_HDA" = "n/a" ]; then
    echo "Throttling stopped. The silent sound-chip test could not run, so judge by ear."
  elif (( M_HDA * 10 <= B_HDA )); then
    echo "Throttling stopped and the DMA underruns went with it: the crackle is the CPU thermal-throttling."
  else
    echo "Throttling stopped but the DMA underruns did not: throttling is not the (only) cause."
  fi
  echo
  echo "Cap changes seen during this run (the 'sh' or 'bash' entry is this script):"
  watch_report
  echo
  echo "The cap stays at ${CAP}% until reboot, or until something rewrites it."
  echo "Listen for crackle now. Later:  sudo bash $0 who     (names whatever rewrote the cap)"
  ;;

who)
  state; watch_report ;;

persist)
  [ -f /usr/lib/tuned/profiles/throughput-performance/tuned.conf ] || { echo "tuned's throughput-performance profile not found; not changing anything."; exit 1; }
  grep -q '^performance=' "$PPD" || { echo "$PPD has no 'performance=' mapping; not changing anything."; exit 1; }
  mkdir -p "$PROFILE_DIR"
  cat > "$PROFILE_DIR/tuned.conf" <<EOF
[main]
summary=ROG G18: Performance profile with the CPU capped at ${CAP}% so the package stays out of thermal throttling (audio crackle fix)
include=throughput-performance

[cpu]
governor=powersave
energy_performance_preference=performance
min_perf_pct=10
max_perf_pct=${CAP}
EOF
  [ -f "$PPD.before-rog-capped" ] || cp -p "$PPD" "$PPD.before-rog-capped"
  sed -i 's/^performance=.*/performance=rog-capped/' "$PPD"
  systemctl restart tuned-ppd
  tuned-adm profile rog-capped
  sleep 2
  echo "Installed $PROFILE_DIR/tuned.conf and mapped the Performance power profile to it."
  state
  if [ "$(cat $P/max_perf_pct)" = "$CAP" ]; then echo "OK: cap is ${CAP}% and will be re-applied whenever the Performance profile is selected."
  else echo "WARNING: max_perf_pct is not ${CAP}. Run '$0 undo' to revert and tell Claude what this printed."; fi
  ;;

undo)
  if [ -f "$PPD.before-rog-capped" ]; then mv -f "$PPD.before-rog-capped" "$PPD"; systemctl restart tuned-ppd; tuned-adm profile throughput-performance; fi
  rm -rf "$PROFILE_DIR"
  [ -d "$TR" ] && { echo 0 > "$TR/tracing_on"; rmdir "$TR"; }
  echo "Reverted."; state
  ;;

*)
  echo "Usage: sudo bash $0 [test|who|persist|undo]"; exit 1 ;;
esac
