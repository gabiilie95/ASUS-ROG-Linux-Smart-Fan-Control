#!/bin/bash
# Counts isochronous OUT packets that the USB host controller skipped (status -18, -EXDEV) for
# every USB device that is playing audio, over N seconds. Read-only; needs root for usbmon.
#
#   sudo bash root-tests/usb-drop-count.sh [seconds]        (default 10, max 120)
#
# Output contract (parsed by the settings panel; keep it stable):
#   USBDROP bus=1 dev=12 path=1-1.4.3 name="Volt 2" packets=239990 skipped=22718 pct=9.47 events=2522 events_per_s=84.07
#   USBDROP none                       no isochronous OUT traffic during the check
#   USBDROP error msg="..."            could not capture
# Counts are estimates: usbmon's text interface prints only the first 5 packet descriptors of each
# URB, so a URB whose shown packets are all skipped is counted as fully skipped, and a URB whose
# last shown packet is skipped is assumed skipped to its end.

set -u
SECS=${1:-10}
case "$SECS" in ''|*[!0-9]*) echo 'USBDROP error msg="seconds must be a whole number"'; exit 2 ;; esac
(( SECS >= 1 && SECS <= 120 )) || { echo 'USBDROP error msg="seconds must be 1-120"'; exit 2; }
[ "$(id -u)" = 0 ] || { echo 'USBDROP error msg="needs root"'; exit 1; }

mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug 2>/dev/null
M=/sys/kernel/debug/usb/usbmon/0u
[ -e "$M" ] || modprobe usbmon 2>/dev/null
[ -e "$M" ] || { echo 'USBDROP error msg="usbmon not available"'; exit 1; }

TMP=$(mktemp) || { echo 'USBDROP error msg="mktemp failed"'; exit 1; }
trap 'rm -f "$TMP"' EXIT
# keep only completions of isochronous OUT transfers; drop the payload words
timeout "$SECS" cat "$M" | grep -E '^[0-9a-f]+ [0-9]+ C Zo:' | sed -E 's/ = [0-9a-f ]+$//' > "$TMP"

python3 -I - "$TMP" "$SECS" <<'EOF'
import sys, glob, os
path, secs = sys.argv[1], float(sys.argv[2])
names = {}
for d in glob.glob("/sys/bus/usb/devices/*"):
    try:
        b = int(open(d + "/busnum").read()); n = int(open(d + "/devnum").read())
    except Exception:
        continue
    try:
        prod = open(d + "/product").read().strip()
    except Exception:
        prod = "?"
    names[(b, n)] = (os.path.basename(d), prod.replace('"', "'"))
stats = {}
for line in open(path, errors="replace"):
    p = line.split()
    try:
        _, ts, typ, addr = p[0], int(p[1]), p[2], p[3]
        _, bus, dev, ep = addr.split(":")
        nd = int(p[5])
        st = [int(x.split(":")[0]) for x in p[6:6 + min(nd, 5)]]
    except Exception:
        continue
    key = (int(bus), int(dev))
    s = stats.setdefault(key, {"packets": 0, "skipped": 0, "events": 0, "last_bad_ts": None})
    s["packets"] += nd
    bad = sum(1 for x in st if x != 0)
    if st and bad == len(st):
        est = nd
    elif st and st[-1] != 0:
        est = bad + (nd - len(st))
    else:
        est = bad
    s["skipped"] += est
    if est:
        # completions within 1.5 ms of the previous skipped one belong to the same stall
        if s["last_bad_ts"] is None or ts - s["last_bad_ts"] > 1500:
            s["events"] += 1
        s["last_bad_ts"] = ts
if not stats:
    print("USBDROP none")
    sys.exit(0)
for (bus, dev), s in sorted(stats.items()):
    pth, name = names.get((bus, dev), ("?", "?"))
    pct = 100.0 * s["skipped"] / s["packets"] if s["packets"] else 0.0
    print('USBDROP bus=%d dev=%d path=%s name="%s" packets=%d skipped=%d pct=%.2f events=%d events_per_s=%.2f'
          % (bus, dev, pth, name, s["packets"], s["skipped"], pct, s["events"], s["events"] / secs))
EOF
