#!/bin/bash
# Installs the SEN3 board-sensor rule for thermald, or removes it again.
#
#   sudo bash root-tests/thermald-sen3/install.sh          install and restart thermald
#   sudo bash root-tests/thermald-sen3/install.sh undo     remove it, back to Fedora's default
#
# What it changes:
#   /etc/thermald/thermal-conf.xml                          the rule (copied from this folder)
#   /etc/systemd/system/thermald.service.d/rog-sen3.conf    runs thermald without --adaptive,
#                                                           because adaptive mode ignores rule files
# Turning adaptive mode off drops the BIOS (DPTF) tables thermald was following. Those did not keep
# SEN3 below 87-91 C on 2026-11-16; thermald's default CPU temperature control stays on.

set -u
[ "$(id -u)" = 0 ] || { echo "Run this with sudo."; exit 1; }
HERE=$(cd "$(dirname "$0")" && pwd)
CONF=/etc/thermald/thermal-conf.xml
DROPIN_DIR=/etc/systemd/system/thermald.service.d
DROPIN=$DROPIN_DIR/rog-sen3.conf
MARK=/etc/thermald/.rog-sen3-installed

show() {
  echo "  thermald: $(systemctl is-active thermald)   command: $(tr '\0' ' ' < /proc/$(systemctl show -p MainPID --value thermald)/cmdline 2>/dev/null)"
  local z s=0
  for z in /sys/class/thermal/thermal_zone*; do [ "$(cat "$z/type")" = SEN3 ] && s=$(cat "$z/temp"); done
  echo "  SEN3 now: $(( s / 1000 )) C   CPU power limit PL1: $(( $(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw) / 1000000 )) W   max_perf_pct: $(cat /sys/devices/system/cpu/intel_pstate/max_perf_pct)"
}

case "${1:-install}" in
install)
  python3 -c "import sys, xml.dom.minidom; xml.dom.minidom.parse(sys.argv[1])" "$HERE/thermal-conf.xml" \
    || { echo "thermal-conf.xml is not valid XML; nothing changed."; exit 1; }
  if [ -f "$CONF" ] && [ ! -f "$MARK" ]; then cp -p "$CONF" "$CONF.before-rog-sen3"; echo "Backed up existing $CONF"; fi
  install -m 644 "$HERE/thermal-conf.xml" "$CONF"
  mkdir -p "$DROPIN_DIR"
  printf '[Service]\nExecStart=\nExecStart=/usr/bin/thermald --systemd --dbus-enable --config-file %s\n' "$CONF" > "$DROPIN"
  touch "$MARK"
  systemctl daemon-reload
  systemctl restart thermald
  sleep 3
  echo "Installed."; show
  echo "Recent thermald messages:"; journalctl -u thermald --since "-20 s" --no-pager -o cat | tail -8
  systemctl is-active -q thermald || echo "thermald is NOT running. Run:  sudo bash $0 undo"
  ;;
undo)
  rm -f "$DROPIN"; rmdir "$DROPIN_DIR" 2>/dev/null
  if [ -f "$CONF.before-rog-sen3" ]; then mv -f "$CONF.before-rog-sen3" "$CONF"; else rm -f "$CONF"; fi
  rm -f "$MARK"
  systemctl daemon-reload
  systemctl restart thermald
  sleep 2
  echo "Removed; thermald is back to Fedora's default (--adaptive)."; show
  ;;
*) echo "usage: sudo bash $0 [install|undo]"; exit 1 ;;
esac
