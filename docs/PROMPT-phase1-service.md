You are building Phase 1 of a smart fan controller for an ASUS ROG Strix G18 (G814JZ) laptop running
Fedora 45 (KDE Plasma 6, Wayland, NVIDIA RTX 4080, Intel HM770 chipset). The repository is
https://github.com/gabiilie95/ASUS-ROG-Linux-Smart-Fan-Control (public); on this machine it is checked
out at `/home/gabe/Documents/Projects/ROG Audio Stutter Fedora`. Branch from `main`.

Read first, in this order:
1. `docs/PLAN-smart-fan-control.md` — the agreed design. Phase 1 is section "1. rog-fand service".
2. `README.md` — the investigation and all measurements behind it.
3. `root-tests/usb-drop-count.sh` (usbmon parsing and output contract), `ui/rogcore.py` (sensor
   readers, presets, validation, how fan curves are applied with asusctl), `tools/fans-balanced.sh`
   (the base curve), `tools/stall-temp-log.py` and `tools/fan-calibrate.py` (CSV log formats).

Task: implement Phase 1 only, on a new branch `feat/rog-fand-service`:
- `service/rog_fand.py`: the root service described in the plan — sensors (RAPL, NVML via ctypes,
  thermal zones by type, asus hwmon by name, platform profile), a usbmon reader thread counting
  skipped isochronous OUT packets per device, heat smoothing, the default feed-forward table, the
  drop-driven boost and per-bin margins with persistence, fail-safe application as
  `max(base curve, floor)` per point through asusctl, rate limiting and slow ramp-down, Performance
  profile only, exact base-curve restore on stop and on error, `status.json` and the validated
  `mode` file, journald logging.
- `--dry-run` (real sensors, apply nothing) and `--replay <csv>` (offline decisions over a recorded
  log) modes.
- `service/rog-fand.service` (systemd unit, `ExecStopPost` restore), `service/install.sh
  install|undo|status`, and `service/config.example.json`.
- Unit tests (`service/test_rog_fand.py`, standard library `unittest`) covering: the decision table,
  boost and margin decay, the ramp-down limiter, `max(base, floor)` curve building and its
  validation (8 points, increasing temperatures, non-decreasing pwm), mode-file validation, usbmon
  line parsing (reuse the known-good logic; the fixture
  `data/fixtures/usbmon-volt-isoc-out-completions-30s.txt.gz`, see `data/fixtures/README.md`, must
  give packets=239990 skipped=22718), and replay over `data/logs/10-13*.csv`.
- A short "Smart fan service" section in `README.md`.

Hard rules (the owner uses this machine while you work):
- Do not change the machine: no real fan-curve writes, no asusctl writes, no sudo/pkexec, no
  service install, no package install, no load tests, nothing that kills or restarts other programs.
  Reading sysfs, NVML and `/etc/asusd/fan_curves.ron` is fine. `--dry-run` as the normal user is
  fine (RAPL and usbmon will be unavailable; handle that gracefully and say so in the status).
- Do not touch the running settings panel or port 8765. Stop your own processes by exact PID;
  `pgrep -f`/`pkill -f` match their own shell here.
- Standard library only. Argument lists for every subprocess, never `shell=True`. Atomic writes
  (temp file + rename) for every file you produce.
- Resolve sound and sensor devices by name, never by number.

Everything you commit is public: no personal data, credentials or raw recordings.
Commit in small, readable steps and push the branch. Open a pull request if a remote is configured.
The PR description must list: files added, every test you ran with its result, a sample of
`--replay` output for one light-load and one heavy-load log, a 2-minute `--dry-run` sample from the
real machine, and an explicit "Not verified" list (everything that needs root or a real install).
Phases 2 (panel tabs) and 3 (load test) are separate later tasks; do not start them.
