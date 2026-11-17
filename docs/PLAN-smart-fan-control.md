# Smart fan control for the ROG Strix G18 (G814JZ) — plan

Status: agreed with the owner on 2026-11-17, not started. Build in three phases, each its own branch
and review. Background and all measurements: `../README.md`.

## Why

Audio crackle on this laptop (USB audio interface and internal speakers) comes from the Intel
chipset stalling for about 1 ms at a time when the area around it is hot. Each stall makes the
chipset's USB controller skip audio packets and the internal HD-audio controller underrun. Airflow
fixes it; the measured threshold depends on heat input and on how the airflow is spread over the
three fans.

The fixed curve (`tools/fans-balanced.sh`, validated 2026-11-16) works, but the laptop's embedded
controller (EC) drives each fan from CPU or GPU temperature, and neither tracks the part that
stalls. Light load is the weak case: CPU in the 60s, fans slow, chipset area still hot. A small
controller can act on what matters instead: the heat going in, and whether audio packets are
actually being dropped.

## Facts the design relies on (verified on this machine)

| Fact | Detail |
|---|---|
| Fan control | Only through asusd: per profile, per fan (cpu, gpu, mid), an 8-point curve of (temperature C, pwm 0-255). `asusctl fan-curve --mod-profile performance --fan <f> --data "40c:20,...,88c:216"` then `--enable-fan-curves true`. Works as the desktop user and as root. The sysfs files (`asus_custom_fan_curve` hwmon) are root-only. No direct fan-speed control. |
| Fan speeds | hwmon device named `asus`: `fan1_input` cpu, `fan2_input` gpu, `fan3_input` mid (rpm). Max about 6300 / 6900 / 7400 rpm. |
| Which temperature drives each fan | Assumed: CPU temperature for cpu and mid fans, GPU temperature for the gpu fan. Consistent with the data, not proven. The load test should verify it. |
| GPU power and temperature | NVML through `ctypes` (`libnvidia-ml.so.1`): `nvmlDeviceGetPowerUsage`, `nvmlDeviceGetTemperature`. Works as the user, no subprocess. |
| CPU power | RAPL `/sys/class/powercap/intel-rapl:0/energy_uj`, root only. |
| USB audio drops | usbmon text interface `/sys/kernel/debug/usb/usbmon/0u`, root only. Isochronous OUT completions with per-packet status -18 (-EXDEV) and length 0 are skipped packets. Parser and output contract: `root-tests/usb-drop-count.sh`. Reproduces the reference capture: 9.47 % skipped, 84 events/s. |
| Rootless stall counter | Play silence on `hw:CARD=PCH,DEV=0` (period 8192) and count extra interrupts on the `/proc/interrupts` line `snd_hda_intel:<card>` (card from `readlink /proc/asound/PCH`). Normal rate 48000/8192 = 5.86/s; anything above is a stall. Holds the internal speakers while running. ALSA card numbers change between boots: always resolve by name. |
| Board sensor | ACPI thermal zone type `SEN3`. Poor proxy for the stalling part: stalls at 70 C at light load, none at 79-84 C at heavy load with full fans. |
| Profiles | Only the Performance profile has custom curves enabled. Balanced/Quiet use factory curves (middle fan max 40 % / 32 %). |
| Load generators | `stress-ng` is installed and has a `--gpu` stressor (not yet tried on the NVIDIA render node). `vkmark` and `glmark2` are in the Fedora repos. CPU load: `tools/loadgen.c`. |

## Calibration data so far (2026-11-16)

Light load (Rocksmith, GPU 63-84 W): middle 70 % (5800 rpm) clean with CPU/GPU fans low;
middle 60 % 7.2 stalls/s; middle 60 % + either other fan at full: clean.
Heavy load (Crimson Desert, GPU 125-168 W): 80/80/80 % clean; middle 100 % with others on curve clean;
70/70/70 % 1.4 stalls/s; 80/80/60 % 3.2/s; 60/60/60 % 39/s.
Logs: `data/logs/10-*.csv`, `11-*.csv`, `12-*.csv`, validation `13-*.csv`.
Owner's noise priority: the high-pitched middle fan near 7300 rpm is what bothers; the calibration
steps up to about 6400 rpm were "not bad".

## Architecture

```
            root                                         desktop user
 ┌───────────────────────────────┐            ┌──────────────────────────────┐
 │ rog-fand (systemd service)    │  status    │ ROG Settings Panel (PySide6) │
 │  sensors: RAPL, NVML, temps,  │──json────▶ │  Auto tab: live state, mode  │
 │  fan rpm, usbmon drops        │            │  Calibrate tab: runs, results│
 │  control: heat → fan floors   │◀──mode──── │                              │
 │  feedback: drops → margin     │  control   └──────────────────────────────┘
 │  applies via asusctl          │                     ▲
 └───────────────────────────────┘                     │ model.json
             ▲                                         │
             │ model.json            tools/autotune.py (load test, unattended)
```

### 1. `rog-fand` service (Phase 1)

Python 3 standard library plus `ctypes` for NVML. Runs as root under systemd.

Loop every 2 s:
- Read CPU W (RAPL delta), GPU W and temperature (NVML), CPU package temperature, SEN3, fan rpm,
  platform profile.
- A usbmon reader thread counts skipped isochronous OUT packets per USB device per second and marks
  "audio active" when isochronous OUT traffic was seen in the last 5 s.
- Heat H: smoothed CPU W + GPU W (rise fast, fall slow, for example EMA 6 s up / 60 s down).

Decision:
- Feed-forward: a table maps heat to minimum fan levels (cpu %, gpu %, mid %), the cheapest
  combination by a noise cost that weights the middle fan highest. Until the load test produces a
  model, use a default table built from the data above. Key it on GPU W until CPU W at these loads
  has been logged (it was never measured; RAPL needs root). Starting point:
  GPU < 50 W: no floor; 50-110 W: 35 / 40 / 70; 110-140 W: 60 / 60 / 75; >= 140 W: 80 / 80 / 80.
- Feedback: when audio is active and drops are seen in 2 consecutive samples, boost at once
  (middle +20 points, others +10, capped at 100) and raise that heat bin's margin by 5 points
  (cap 20). After 30 min clean in a bin, lower its margin by 2.5 (floor 0). Persist margins in
  `/var/lib/rog-fand/state.json`.
- No feedback when no USB audio is playing (nothing to protect on USB); feed-forward still applies.

Applying (fail-safe by construction):
- Never write a flat "fixed speed" curve. Write `curve_f(T) = max(base_f(T), floor_f)` for each of the
  8 points, where `base` is the balanced curve. If the service dies, the EC still ramps every fan to
  100 % by temperature.
- Only touch the Performance profile, and only while `platform_profile` is `performance`. If the owner
  switches profile, stop overriding and log it.
- Write only when a floor changes by at least 5 points. Up: immediately. Down: after a 60 s hold, at
  most 5 points per 20 s (no surging).
- On stop (SIGTERM, `ExecStopPost`) and on any unhandled error: restore the base curve exactly.
- If a sensor fails (NVML unavailable, RAPL unreadable), use a conservative floor (for example 60 / 60 / 80)
  and report it in the status.

Interfaces:
- `/run/rog-fand/status.json` (0644), rewritten atomically every 2 s: sensor values, heat, mode,
  floors, margins per bin, drops/s per device, audio active, last events, version.
- Mode control: `/run/rog-fand/mode`, owned by the configured desktop user, one line:
  `auto`, `off` (restore base, no overrides) or `preset:<name>` from a fixed list. Strictly validated;
  anything else is ignored and logged. Read every loop.
- Config `/etc/rog-fand/config.json` (root): base curve, default table, noise weights, thresholds,
  control user. Model `/etc/rog-fand/model.json` when present overrides the default table.
- Logs to the journal; optional CSV of every sample under `/var/log/rog-fand/`.

Modes for testing:
- `--dry-run`: read real sensors, print decisions, apply nothing, start no usbmon reader unless root.
- `--replay data/logs/<file>.csv`: run the decision logic offline over a recorded log (columns as
  written by `tools/stall-temp-log.py` / `tools/fan-calibrate.py`) and print the floors it would set.
  Unit tests use this with the existing logs.

Install: `service/install.sh install|undo|status` (sudo): copies files, creates the unit and the
state directory, enables and starts the service. `undo` stops it, restores the base curve, removes
everything.

### 2. Panel tabs (Phase 2)

In `ui/rog-panel-qt.py` (shared logic in `ui/rogcore.py`):
- Auto tab: service state (not installed / stopped / running / dry run), mode switch, a live chart of
  heat in, floors and actual rpm per fan, USB drops/s, margins per heat bin, the event log. Reads
  `status.json`; writes `mode`.
- Calibrate tab: the step runs of `tools/fan-calibrate.py` (presets: light, heavy, custom, observe)
  with live progress, a per-step results table and CSV export. Restores the exact curve at the end
  and on cancel, as the script does. Shows a warning when the internal sound device is busy.

### 3. Load test and model (Phase 3)

`tools/autotune.py`, unattended, about 60-90 minutes, loud:
- Heat levels: CPU load from `tools/loadgen.c`, GPU load from `stress-ng --gpu` on the NVIDIA render
  node (or `vkmark`), at 3-4 levels covering about 40-170 W on the GPU. Confirm first that the
  generator reaches those levels on the NVIDIA card.
- At each level: step through fan combinations from loud to quiet, 180 s each, judged on the last
  120 s; stop descending at the first combination that stalls (> 1/s). Restore a safe curve between
  levels.
- Measure: stalls (rootless counter), USB drops (from the service's status, with inaudible silence
  played to the Volt 2 through PipeWire so the link is exercised), temperatures, CPU W (from the
  service), GPU W.
- Also run a short "which sensor drives which fan" check: heat only the CPU, then only the GPU, and
  watch which fans respond to the curve.
- Output `model.json`: per heat level the cheapest clean combination plus fitted per-fan cooling
  effect; and a static fallback curve derived from it.

### 4. Learning as it goes (Phase 4, after the load test)

Goal: the service learns, from normal use, which fan mix keeps the chipset area cool enough at the
lowest noise, instead of relying only on the fixed table.

Approach: small, explainable models in the standard library, not deep learning.
- **Thermal response model.** Recursive least squares, updated every sample, predicting the board
  sensor and CPU/GPU temperatures from CPU W, GPU W and the three fan speeds. It learns "how much each
  fan cools per 1000 rpm at this heat" from ordinary variation, with no deliberate experiments.
- **Stall-risk model.** Logistic regression predicting USB drops in the next 30 s from heat, fan
  speeds and temperatures. Bootstrap it from the calibration logs (`data/logs/10-13`, about 2000
  labelled samples across loads and fan mixes) and the Phase 3 load test; refine it with the
  service's own drop observations.
- **Choice.** Each decision picks the quietest fan mix (cost weighted per fan, middle fan highest,
  steeper above about 6000 rpm) whose predicted drop risk is below a threshold. The existing table is
  the fallback whenever the model is unsure (few samples in this heat range, or poor recent
  accuracy).
- **Exploration, kept safe.** Never jump: try a quieter mix only one 5-point step at a time, only
  when the model is confident, at most every 10 minutes, and revert at once on any drop event. The
  existing drop boost and per-bin margins stay as the safety net.
- **Your ear in the loop.** An "it's too loud" / "crackle heard" button in the panel's Auto tab adds
  labels the sensors can't: noise tolerance and stalls on the internal speakers.

Guard rails: never below the base curve (already true); floors only within [table floor - 15,
100]; model and learned data in `/var/lib/rog-fand/learned.json` with a version and a reset
button; the panel shows what was learned (for example "middle fan: -2.1 C per 1000 rpm at
heavy load") and the model's recent accuracy. Enable `log_csv` with rotation to keep training data.

Done when: offline, the model trained on logs 10-12 predicts the stalls in log 13 better than the
table; live, a week of normal use shows equal or fewer drop events than the table with lower
average fan speed, and the owner prefers it.

## Phases and acceptance

| Phase | Done when |
|---|---|
| 1 service | Replay tests over `data/logs/10-13*.csv` give sensible floors (light-load log: middle >= 70 %; heavy: about 80/80/80). Dry run on the real machine for 10 minutes of gaming prints plausible decisions and writes nothing. Install/undo script reviewed. Owner then installs it; stop restores the base curve exactly; no writes while the profile is not Performance; a real session shows zero USB drops or a logged boost. |
| 2 panel | Offscreen self-test extended and passing; owner checks the tabs on screen. |
| 4 learning | Offline: beats the table on log 13 when trained on logs 10-12. Live: a week of normal use with no more drop events than the table and lower average fan speed. |
| 3 load test | Generator levels verified; one unattended run produces `model.json`; a 20-minute validation in two games shows 0 stalls with the model active and the highest fan speed reported. |

## Rules for whoever builds it

- Nothing that changes the machine without the owner's explicit go-ahead in the conversation: no
  real fan writes, no `sudo`/`pkexec`, no service install, no package install, no load tests.
  Use `--dry-run`, `--replay`, offscreen Qt (`QT_QPA_PLATFORM=offscreen`) and unit tests.
- Do not touch running programs or the panel's port 8765. Stop your own processes by exact PID;
  `pgrep -f` / `pkill -f` match their own shell command line here.
- Resolve the laptop sound chip by name (`/proc/asound/PCH`), never `card0` or `hw:0,0`.
- Standard library only (PySide6 for the panel). Argument lists for every subprocess, never
  `shell=True`. Root code reads only fixed paths and the strictly validated mode file.
- One branch per phase; the PR description lists what was tested, how, and what is unverified.

## Open questions

1. Which temperature drives each fan (to be measured in Phase 3).
2. CPU W at light and heavy gaming load (log it in Phase 1 dry runs before keying on total heat).
3. Noise cost weights: start with middle fan 2x the others above 6000 rpm; tune with the owner.
4. Whether to offer the rootless stall counter as a second feedback source for the internal speakers.
