#!/bin/bash
# Removes (or restores) the boot options that keep the CPU out of its deeper idle states:
#   intel_idle.max_cstate=1 processor.max_cstate=1        (carried over from Ubuntu)
# Without them the CPU can rest properly when idle, which means less heat on the board. Deeper idle
# states can also add wake-up latency, which is why such options are sometimes used against audio
# dropouts, so re-check with the stall monitor after rebooting.
#
#   sudo bash root-tests/cstates.sh remove     takes effect at the next reboot
#   sudo bash root-tests/cstates.sh restore    puts them back (also after the next reboot)
#   bash root-tests/cstates.sh status          shows what is configured and what is running

set -u
ARGS="intel_idle.max_cstate=1 processor.max_cstate=1"
FILES=(/etc/default/grub /etc/kernel/cmdline)
BK=.before-rog-cstates

status() {
  echo "running now: $(grep -oE 'intel_idle.max_cstate=[0-9]+|processor.max_cstate=[0-9]+' /proc/cmdline | tr '\n' ' ')"
  echo "idle states the CPU may use now: $(for s in /sys/devices/system/cpu/cpu0/cpuidle/state*; do printf '%s ' "$(cat $s/name)"; done)"
  for f in "${FILES[@]}"; do
    if [ -r "$f" ]; then
      found=$(grep -oE 'intel_idle.max_cstate=[0-9]+|processor.max_cstate=[0-9]+' "$f" | tr '\n' ' ')
      echo "$f: ${found:-(none)}"
    fi
  done
  if [ "$(id -u)" = 0 ]; then
    echo "boot entries (grubby):"; grubby --info=ALL 2>/dev/null | grep -E '^(title|args)=' | sed -E 's/root=UUID=[^ ]+/root=UUID=../' | sed 's/^/  /'
  fi
}

case "${1:-status}" in
status) status ;;
remove)
  [ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
  for f in "${FILES[@]}"; do
    [ -f "$f" ] || continue
    [ -f "$f$BK" ] || cp -p "$f" "$f$BK"
    sed -i -E 's/ ?intel_idle\.max_cstate=1//g; s/ ?processor\.max_cstate=1//g' "$f"
  done
  grubby --update-kernel=ALL --remove-args="$ARGS"
  echo "Removed. They stay active until you reboot."; status
  ;;
restore)
  [ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
  for f in "${FILES[@]}"; do
    [ -f "$f$BK" ] && mv -f "$f$BK" "$f"
  done
  grubby --update-kernel=ALL --args="$ARGS"
  echo "Restored. Takes effect after the next reboot."; status
  ;;
*) echo "usage: sudo bash $0 remove|restore   or   bash $0 status"; exit 1 ;;
esac
