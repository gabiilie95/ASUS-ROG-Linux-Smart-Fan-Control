# Test fixtures

`usbmon-volt-isoc-out-completions-30s.txt.gz` — the isochronous OUT completion lines (`C Zo:`) from a
30 s usbmon text capture of the Universal Audio Volt 2 (bus 1, device 12) on 2026-11-16, taken while
the crackle was audible. Submissions, other endpoints and all audio payload words were removed; what
is left is only what the drop counter parses. Expected result of `root-tests/usb-drop-count.sh`'s
parser with 30 s: `packets=239990 skipped=22718 pct=9.47 events=2522 events_per_s=84.07`.
