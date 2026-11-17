#!/bin/bash
# Makes the "Performance" power profile run the CPU cooler without touching fans or GPU power.
#
#   sudo bash root-tests/setup-cool-performance.sh           install (cap 55 %)
#   sudo CAP=70 bash root-tests/setup-cool-performance.sh    install with another cap
#   sudo bash root-tests/setup-cool-performance.sh undo      put everything back
#
# What changes: Fedora maps the Performance profile to tuned's "throughput-performance", which
# uses the `performance` governor and so holds every core at the highest allowed frequency.
# This installs a small tuned profile, "rog-performance", that keeps the ASUS Performance
# platform profile (fans, GPU power) but uses the `powersave` governor with a
# balance_performance preference and the CPU cap, then maps Performance to it.
# Nothing here affects the fan curves; those live in asusd.

set -u
[ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
CAP=${CAP:-55}
MODE=${1:-install}
DIR=/etc/tuned/profiles/rog-performance
PPD=/etc/tuned/ppd.conf
P=/sys/devices/system/cpu/intel_pstate
OLDUNIT=intel-pstate-70.service
MARK=/etc/tuned/rog-performance.disabled-old-cap-unit

state() {
  echo "  tuned profile : $(tuned-adm active 2>/dev/null | sed 's/Current active profile: //')"
  echo "  governor      : $(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)   preference: $(cat /sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference 2>/dev/null)"
  echo "  CPU cap       : max $(cat $P/max_perf_pct) %  min $(cat $P/min_perf_pct) %"
  echo "  ASUS profile  : $(cat /sys/firmware/acpi/platform_profile 2>/dev/null)"
}

case "$MODE" in
install)
  [ -f /usr/lib/tuned/profiles/throughput-performance/tuned.conf ] || { echo "tuned's throughput-performance profile not found; nothing changed."; exit 1; }
  grep -q '^performance=' "$PPD" || { echo "$PPD has no 'performance=' line; nothing changed."; exit 1; }
  echo "Before:"; state
  mkdir -p "$DIR"
  cat > "$DIR/tuned.conf" <<EOF
[main]
summary=ROG G18: Performance fans and GPU power, CPU on the powersave governor capped at ${CAP} %
include=throughput-performance

[cpu]
governor=powersave
energy_performance_preference=balance_performance
energy_perf_bias=normal
min_perf_pct=10
max_perf_pct=${CAP}
EOF
  [ -f "$PPD.before-rog-performance" ] || cp -p "$PPD" "$PPD.before-rog-performance"
  sed -i 's/^performance=.*/performance=rog-performance/' "$PPD"
  # The older boot unit writes a fixed 55 % at every boot and would race with tuned's value,
  # so tuned becomes the single place the cap is set. "undo" re-enables the unit.
  if [ "$(systemctl is-enabled "$OLDUNIT" 2>/dev/null)" = enabled ]; then
    systemctl disable "$OLDUNIT" >/dev/null 2>&1 && touch "$MARK" && echo "Disabled $OLDUNIT (tuned now sets the cap)."
  fi
  systemctl restart tuned-ppd
  tuned-adm profile rog-performance
  sleep 2
  echo "After:"; state
  ok=1
  [ "$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor)" = powersave ] || ok=0
  [ "$(cat $P/max_perf_pct)" = "$CAP" ] || ok=0
  [ "$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)" = performance ] || ok=0
  if [ $ok = 1 ]; then echo "OK. It is re-applied whenever the Performance power profile is selected, including after a reboot."
  else echo "Something did not apply as expected. Run:  sudo bash $0 undo   and show Claude this output."; fi
  ;;
undo)
  if [ -f "$PPD.before-rog-performance" ]; then mv -f "$PPD.before-rog-performance" "$PPD"; fi
  if [ -f "$MARK" ]; then systemctl enable "$OLDUNIT" >/dev/null 2>&1 && echo "Re-enabled $OLDUNIT."; fi
  systemctl restart tuned-ppd
  tuned-adm profile throughput-performance
  rm -rf "$DIR"
  sleep 2
  echo "Reverted:"; state
  ;;
*)
  echo "Usage: sudo bash $0 [install|undo]"; exit 1 ;;
esac
