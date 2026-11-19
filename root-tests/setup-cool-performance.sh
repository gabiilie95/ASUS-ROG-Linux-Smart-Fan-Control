#!/bin/bash
# Persist a CPU ceiling in every TuneD/PPD mode, retaining each mode's other policies.
# sudo CAP=70 bash root-tests/setup-cool-performance.sh [install|undo]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run this with sudo." >&2; exit 1; }
CAP=${CAP:-55}
MODE=${1:-install}
HERE=$(cd -- "$(dirname -- "$0")" && pwd -P)
PPD=/etc/tuned/ppd.conf
BACKUP=/etc/tuned/ppd.conf.before-rog-cpu-cap
OLDUNIT=intel-pstate-70.service
MARK=/etc/tuned/rog-cpu-cap.disabled-old-cap-unit
stage=
trap 'if [[ -n $stage ]]; then rm -rf -- "$stage"; fi' EXIT

case "$MODE" in
install)
  [[ $CAP =~ ^(4[0-9]|[5-9][0-9]|100)$ ]] || { echo "CAP must be an integer from 40 to 100; nothing changed." >&2; exit 1; }
  command -v powerprofilesctl >/dev/null
  [ -f /sys/devices/system/cpu/intel_pstate/max_perf_pct ] || { echo "Intel P-State percentage control unavailable." >&2; exit 1; }
  selected=$(powerprofilesctl get)
  [[ $selected == performance || $selected == balanced || $selected == power-saver ]] || { echo "Unknown power profile: $selected" >&2; exit 1; }
  source=$PPD
  [ ! -f "$BACKUP" ] || source=$BACKUP
  # Resolve all inherited CPU policies before writing configuration or invoking a service.
  stage=$(mktemp -d /etc/tuned/.rog-cpu-cap.XXXXXXXX)
  /usr/bin/python3 -IB "$HERE/cpu_cap_profiles.py" "$source" "$stage/generated" "$CAP"
  [ -f "$BACKUP" ] || cp -p "$PPD" "$BACKUP"
  for dir in "$stage"/generated/rog-cpu-cap-*; do
    mkdir -p "/etc/tuned/profiles/$(basename "$dir")"
    install -m 0644 "$dir/tuned.conf" "/etc/tuned/profiles/$(basename "$dir")/tuned.conf"
  done
  install -m 0644 "$stage/generated/ppd.conf" "$PPD"
  if [ "$(systemctl is-enabled "$OLDUNIT" 2>/dev/null || true)" = enabled ]; then
    systemctl disable "$OLDUNIT"
    touch "$MARK"
  fi
  systemctl enable tuned.service tuned-ppd.service
  systemctl restart tuned-ppd
  powerprofilesctl set "$selected"
  # PPD skips an already-active TuneD profile. Force a reload when only CAP changed;
  # obtain the active mapping from TuneD so the battery-specific mapping is respected.
  active=$(tuned-adm active | sed 's/Current active profile: //')
  [[ $active == rog-cpu-cap-* && $active != *[[:space:]]* ]] || {
    echo "TuneD did not select a CPU cap overlay: $active" >&2; exit 1;
  }
  tuned-adm profile "$active"
  sleep 2
  actual=$(cat /sys/devices/system/cpu/intel_pstate/max_perf_pct)
  [[ $actual =~ ^[0-9]+$ ]] && (( actual <= CAP )) &&
    [ "$(powerprofilesctl get)" = "$selected" ] &&
    [ "$(cat /etc/tuned/ppd_base_profile)" = "$selected" ] || {
      echo "CPU cap verification failed; configuration is saved. Undo with: sudo bash $0 undo" >&2
      exit 1
    }
  echo "Saved CPU ceiling $CAP % for every power mode (current limit $actual %, mode $selected)."
  ;;
undo)
  legacy=0
  restore=$BACKUP
  if [ ! -f "$restore" ]; then
    restore=$PPD.before-rog-performance
    [ -f "$restore" ] || { echo "No saved CPU cap installation to undo."; exit 0; }
    legacy=1
  fi
  selected=$(powerprofilesctl get)
  cp -p "$restore" "$PPD"
  systemctl restart tuned-ppd
  powerprofilesctl set "$selected"
  if [ -f "$MARK" ]; then
    systemctl enable "$OLDUNIT"
    rm -f "$MARK"
  fi
  if (( legacy )); then
    legacy_mark=/etc/tuned/rog-performance.disabled-old-cap-unit
    if [ -f "$legacy_mark" ]; then
      systemctl enable "$OLDUNIT"
      rm -f "$legacy_mark"
    fi
    rm -rf /etc/tuned/profiles/rog-performance
  else
    # The legacy profile and backup may be needed by the restored mappings.
    rm -rf /etc/tuned/profiles/rog-cpu-cap-*
  fi
  rm -f "$restore"
  echo "Restored the previous power-profile mappings; selected mode remains $selected."
  ;;
*) echo "Usage: sudo CAP=55 bash $0 [install|undo]" >&2; exit 1 ;;
esac
