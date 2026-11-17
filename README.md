# ROG G18 audio crackle (Fedora 45)

Machine: ASUS ROG Strix G814JZ, i9-13980HX, RTX 4080, HM770 chipset, BIOS 334.
Audio: Universal Audio Volt 2 on USB (via ThinkPad Thunderbolt 4 dock), internal speakers, NVIDIA HDMI.

## Status (2026-11-16, after the 13:10 freeze and reboot)

**Crackle: cause found and fixed with fan curves. Graphics memory: Jellyfin fixed (software-drawn web layer); the game still fills the card.**

The laptop's chipset stalls for about 1 ms at a time when the board gets hot. Each stall makes
the chipset's USB controller skip about 9 audio packets to the Volt 2 and makes the internal
sound controller underrun. That is the crackle. The original Performance fan curve topped out at
84 % / 69 % / 43 % (CPU / GPU / middle fan).

Fix in place: `tools/fans-max.sh` – Performance profile, all three fans 100 % from 70 °C.
asusd saved it in `/etc/asusd/fan_curves.ron`; it survived the reboot.
- Middle fan alone at 100 % (`tools/fans-mid-max.sh`) was clean at ~128 W GPU but left about
  16 stalls/s at ~153 W with the CPU uncapped (board 87 °C). All three at 100 % took that to 0.
- Undo: `tools/fans-original.sh`.
- Confirmed by ear at about 12:36 ("it's gone").

CPU side, applied 2026-11-16 ~14:05 with `root-tests/setup-cool-performance.sh`: tuned profile
`rog-performance` mapped to the Performance power profile. Verified right after: all 32 CPUs on
the `powersave` governor with `balance_performance`, cap 55 % (min 15 %), ASUS profile still
Performance, fan curves untouched. Undo: `sudo bash root-tests/setup-cool-performance.sh undo`.
Not yet verified: behaviour across a reboot, and temperatures / stall rate / fps under a
gaming load with this setup. Governor and cap do not affect the crackle directly; they reduce
the heat that does.

## Evidence

| Check | Result |
|---|---|
| Digital signal PipeWire sends to the Volt 2 (sink monitor) | no clicks above 17.5 kHz in 31 s |
| Phone recordings of the speakers, same minute | 30–87 clicks above 17.5 kHz each |
| USB capture of the Volt 2 (usbmon, 30 s) | 9.5 % of playback packets skipped by the chipset xHCI (status -18, 0 bytes), 84 stalls/s, ~9 packets each |
| Internal sound controller, silence played straight to `hw:0,0` | ~75 DMA underruns/s at the same time |
| NVIDIA HDMI audio controller, same test | 0 |
| Fans slowed 5500 → 4300 rpm, GPU power constant | stalls 75 → 90+/s within 20 s |
| All fans to maximum, GPU power constant | stalls 80 → 0/s in ~70 s |
| Only the middle fan at maximum | stalls stay at 0 (3 min; 10 min soak in `data/logs`) |

Ruled out (stall rate unchanged): CPU cap 55/70/100, turbo off, uncore clock pinned, governor
and power profile, thermal throttling of the CPU, split locks, sensor polling, PCM format and
period, synthetic CPU / memory / disk load, firmware interrupts (SMI rate 0).

## Fan-speed tuning (2026-11-16, game at 125–146 W GPU)

| Middle fan | rpm | Stalls per second |
|---|---|---|
| 43 % (original Performance curve) | 4300 | 75–90 |
| 70 % | 5700 | 35 and still rising after 150 s |
| 84 % | 6600 | settles around 20 (it read 0 only while the game was running light, ~100 W) |
| 100 % | 7400 | 0 |

So there is no quieter setting that holds under full load; the curve stays at 100 %.
The Balanced and Quiet profiles still cap the middle fan at 40 % and 32 %, so the crackle will
come back under load in those profiles.

10-minute soak at 100 % (`data/logs/07-soak-mid-fan-100.csv`, 12:51–13:01): 0 stalls throughout
(max 0.6/s in any 2 s sample), GPU 74–153 W (mean 128 W), board sensor 79–85 °C.
`05-soak-mid-fan-max.csv` is only 48 s; an earlier soak was spoiled when its silent player was
killed by mistake.

## Quieter curve with the cooler CPU profile (2026-11-16 ~14:10–14:25, cap 70 %, powersave)

| CPU / GPU / middle fan (rpm) | GPU load | Stalls per second |
|---|---|---|
| 6300 / 6900 / 7400 (`tools/fans-max.sh`) | up to 168 W | 0 |
| 4650 / 4900 / 7400 (CPU and GPU original) | 147 W, board 82 °C | 0 |
| same | 159 W, board 86 °C | 4, peaks of 13 |
| 4270 / 6090 / 6100 (all ramped) | 134 W, board 79 °C | 5 and rising |
| 4270 / 6090 / 7400 (`tools/fans-ramped.sh`, current) | 135 W, board 79 °C | 0 (3 min) |

The middle fan has to be at full speed under a gaming load; the CPU fan can stay on its original
curve; the GPU fan can ramp and reach 100 % at 80 °C. `fans-ramped.sh` is not yet verified at
peak load (GPU ~160 W, board 86 °C). With cap 55 the CPU ran 57–74 °C instead of 72–87 °C.

## CPU heat and the board sensor; thermald (2026-11-16 afternoon)

- thermald 2.5.13 is installed and running in `--adaptive` mode, and the BIOS provides the DPTF
  tables it uses (passive policy; the board sensor SEN3 is handed to user space with a passive trip
  at 65 C). Even so, SEN3 reached 87-91 C with the CPU power limit untouched at 105 W, so it is not
  effectively cooling this area. (The `PCIe_Port_Link_Speed_*` cooling devices showing a non-zero
  state only reflect idle links running slower; not thermal throttling.)
- CPU heat does raise the board sensor. From the logs, middle fan at full speed: cap 55 -> CPU
  69 C, board 75.1 C (GPU 153 W); cap 70 -> CPU 83 C, board 82.3 C (GPU 147 W). Rough fit:
  +10 C on the CPU package is about +3 C on the board. Observational, not a controlled test.
- Correction: the earlier "cap and governor do not affect the stall rate" came from 8-15 s tests,
  too short for the board to cool (about a minute). Directly they do nothing; through board heat
  they probably do. Cap 55 is the recommended setting for gaming.
- thermald rule drafted, not installed: `root-tests/thermald-sen3/` (`thermal-conf.xml` + `install.sh`,
  `install.sh undo`). When SEN3 passes 78 C it lowers the CPU power limit, then the frequency cap.
  thermald's adaptive mode disables rule files entirely (its source sets `parser_disabled = true`),
  so the installer runs thermald without `--adaptive`; that drops the BIOS DPTF tables, which were
  not keeping SEN3 down anyway. XML and script syntax checked only.
- Alternative not built: a small user-level watcher of SEN3 that switches the fan curve (the
  measured fix) only when the board is hot, e.g. full middle fan above 80 C, back below 74 C.
  Needs no root (asusctl works as the user). Generic fan tools (fancontrol, CoolerControl) cannot
  drive these fans directly: the EC runs them from curves, which only asusd sets.

## Light-load stalls (2026-11-16 ~17:30, Rocksmith via PipeASIO)

- About 23-30 stalls/s at light load (GPU about 60 W) with the board sensor at only 70 C. The fan
  curves follow CPU/GPU temperature, so the fans ran at 3000 / 3600 / 5100 rpm.
- All three fans at full (every curve point 255): 23 -> 0 stalls/s within 40 s, board 70 -> 65 C.
  So it is still heat, but the board sensor is a poor guide to it: under heavy load with full fans
  it read 79-84 C with no stalls. Airflow over the chipset area is what matters.
- PipeWire logged `spa.alsa: hw:V2p: follower ... resync` 3-6 times a second during the stalls and
  none once the fans were at full: a consequence of the dropped USB packets, not a separate fault.
- The GPU's PCIe link stayed at 16 GT/s throughout (link retraining ruled out).

## Fan calibration at light load (2026-11-16 ~18:05-18:26, Rocksmith songs, GPU 63-84 W)

`tools/fan-calibrate.py` (log `data/logs/10-fan-calibration-light.csv`), 3 min per step, judged
after the first minute:

| Step | Fans CPU / GPU / middle (rpm) | Stalls/s avg (max) |
|---|---|---|
| all full | 6200 / 6900 / 7300 | 0.0 |
| middle 90 %, others on curve | 2600 / 3100 / 6900 | 0.0 |
| middle 80 % | 3000 / 3700 / 6400 | 0.0 |
| middle 70 % | 3000 / 3600 / 5800 | 0.0 |
| middle 60 % | 3000 / 3700 / 5200 | 7.2 (10.4), crackle heard |
| middle 60 % + GPU fan full | 2600 / 6900 / 5200 | 0.0 |
| middle 60 % + CPU fan full | 6300 / 2600 / 5200 | 0.0 |

At this load the middle fan alone needs about 70 % (5800 rpm). Below that, either other fan at full
makes up the difference: total airflow is what counts, not the middle fan specifically. That means
the airflow can be spread over the three fans so none runs in its high-pitched top range.
Heavy load (about 150 W) still needs its own calibration.

## Fan calibration at heavy load (2026-11-16 ~18:29-18:45, Crimson Desert, GPU 125-168 W)

Log `data/logs/11-fan-calibration-heavy.csv`, 4 min per step, CPU cap 60:

| Step | Fans CPU / GPU / middle (rpm) | Board | Stalls/s |
|---|---|---|---|
| all 100 % | 6200 / 6900 / 7300 | 75 C | 0.0 |
| 80 / 80 / 80 % | 5300 / 5900 / 6300 | 80 C | 0.0 |
| middle 100 %, others on curve | 4300 / 6100 / 7300 | 79 C | 0.0 |
| 70 / 70 / 70 % | 4900 / 5400 / 5700 | 80-81 C | 0.0 for 3.5 min, then the game crashed |

The game crashed at 18:45:18 with NVIDIA Xid 13 (`SKEDCHECK23_CONSTANT_BUFFER_SIZE`), the same
signature as at 11:33 and 11:51. This time there were no graphics-memory errors before it and the
GPU was at 79 C, so neither memory nor the fan test explains it; it looks like a game / vkd3d-proton
/ driver fault. 
Second run (`data/logs/12-fan-calibration-heavy-2.csv`, 135-158 W, board 80-84 C):

| Step | Fans CPU / GPU / middle (rpm) | Stalls/s avg (max) |
|---|---|---|
| 70 / 70 / 70 % | 4800 / 5400 / 5700 | 1.4 (7.8) |
| 80 / 80 / 60 % | 5200 / 6000 / 5100 | 3.2 (6.5) |
| 60 / 60 / 60 % | 4500 / 4900 / 5200 | 39 (47) |

So heavy load needs about 80 / 80 / 80 % (clean in run 1) or a full middle fan; 70 / 70 / 70 was
clean for 3.5 minutes in run 1 but not over a full hotter run. Temperatures seen: light load CPU
57-68 C, GPU 54-66 C; heavy load CPU 66-77 C, GPU 70-79 C.

Recommended and applied: `tools/fans-balanced.sh` (the panel's "balanced" preset): middle about
70 % from 62 C, all three at 80 % from 72 C, full only from 86-88 C. Validated 2026-11-16
19:01-19:21 (`data/logs/13-fan-balanced-validation.csv`): 8 min Crimson Desert at 143-153 W, fans
5200-5400 / 5800 / 6200-6300 rpm; 9 min Rocksmith at about 74 W, fans 3300 / 3200 / 5700-5800 rpm;
0 stalls in every minute, no PipeWire re-syncs. The highest fan speed is now 6300 rpm, against
7300 rpm before.
Caveat: the middle fan follows CPU temperature. If the CPU runs cooler at light load (for example
after removing the C-state limit), the middle fan drops to 60 % at 58 C, which was not enough at
light load; then raise its 58 C point to 178 (70 %).

## After the reboot with the C-state limit removed (2026-11-16 19:29)

- The CPU now uses its deeper idle states (C2, C3); everything else survived the reboot.
- The ALSA card numbers changed: the Volt 2 became card 0 and the laptop's own chip ("PCH") card 1.
  All tools now find the chip by name (`hw:CARD=PCH,DEV=0`, `/proc/asound/PCH`, IRQ label looked
  up from it). Before this fix the stall counter silently measured nothing.
- Idle: 0 stalls in 20 s, CPU 54-57 C (60-63 C with the old limit).
- Still to check: light load (Rocksmith) with the cooler CPU, because the middle fan follows CPU
  temperature and may drop to 60 %, which was not enough at light load.

## Graphics memory (separate problem, same day)

The RTX 4080 has 12 GB and the laptop runs in dGPU-only mode (`supergfxctl -g` = AsusMuxDgpu),
so the compositor and every desktop app share it with the game. It ran out at 11:33, 11:51 and
12:35–12:40: the game froze or stuttered, Jellyfin lost its video and then crashed, and KWin
logged 306 allocation failures in one minute.

Typical split at 13:05: game 8.6 GB, Firefox 250 MB, plasmashell 240 MB, plasma-keyboard 230 MB,
KWin 230 MB, Sunshine 190 MB, Unity Hub 160 MB, Steam web helper 210 MB. Jellyfin had been
1.6–4.3 GB.

Tried and reverted: running Jellyfin on the Intel GPU.
- A Vulkan window on the Intel GPU displays correctly in dGPU-only mode; an OpenGL window is
  black. Jellyfin uses OpenGL there, so its window was black.
- OpenGL routed through Vulkan (zink) displayed a test window correctly, and was then applied to
  Jellyfin. The machine hard-froze at 13:10:55, seconds later, with nothing in the log. Cause
  not proven, but do not repeat this without a way to recover (e.g. SSH from another machine).
- Jellyfin is back on its original setup (only the PipeWire socket override remains).

Fix that held (14:29 onward): Jellyfin draws its web interface in software, so only the video
uses graphics memory.
- `flatpak override --user --env=QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu org.jellyfin.JellyfinDesktop`
- Confirmed working by the user at about 14:35.
- Jellyfin's graphics memory: 470 MB and flat over 3.5 minutes of playback (it used to climb to
  2.7 GB within minutes and 4.3 GB over longer). No context-lost or buffer errors since.
- Cost: about one CPU core for the web layer (renderer ~70 %, main process ~25 %).
- Undo: `flatpak override --user --unset-env=QTWEBENGINE_CHROMIUM_FLAGS org.jellyfin.JellyfinDesktop`
- `--force-gpu-mem-available-mb` / `--force-gpu-mem-discardable-limit-mb` did NOT help.
- The game still expands into free memory (7.8–8.6 GB); the kernel still logs NVIDIA
  "can't update VA space" lines. Untried: Steam launch option
  `DXVK_CONFIG="dxgi.maxDeviceMemory = 7168" %command%`, or lower texture quality.

Optional savings not applied: turn off the virtual keyboard (230 MB, System Settings → Keyboard →
Virtual Keyboard → None), stop Sunshine when not streaming (190 MB), close Unity Hub (160 MB).

## Measuring it again

Stall counter, no root, inaudible (plays silence on the internal speakers' device):

    python3 tools/stall-counter.py 15 "label"          # 15 s, prints stalls per second
    python3 tools/stall-temp-log.py 600 out.csv        # log stalls vs temperatures and fans

USB proof, needs root:

    sudo bash root-tests/usb-audio-check.sh            # counts packets the controller skipped

Click counting in recordings (mono 16-bit 48 kHz WAV, high-passed at 17.5 kHz first):

    ffmpeg -i in.m4a -ac 1 -ar 48000 -af "highpass=f=17500:poles=2,highpass=f=17500:poles=2,highpass=f=17500:poles=2,highpass=f=17500:poles=2,volume=8" hp.wav
    python3 tools/click-count-hf.py hp.wav "label"

## Open questions

- Which component overheats. The board sensor `SEN3` read 87–91 °C against trip points of
  65/75/80 °C, but it is only a rough proxy: stalls stopped at about 84 °C with the middle fan
  at maximum, yet were present at 64–73 °C with the old curve.
- The lowest middle-fan speed that still keeps stalls at 0 (7400 rpm is loud).
- Whether dust or aged thermal pads are why this area runs hot.
- The CPU still runs at 85–95 °C uncapped; that is a separate matter from the crackle.

## Other things found along the way

- The 12 GB GPU ran out of memory twice (11:33 and 11:51), freezing the game. Jellyfin Desktop
  held 4.3 GB at one point. Not related to the crackle.
- Jellyfin "pausing" was caused by an earlier test switching the Volt 2's profile.
- The user is no longer in the `pipewire` and `audio` groups after the migration.

## Folder layout

- `ui/` – the settings panel (see below); also installed as a menu entry, "ROG Settings Panel"
- `tools/` – measuring scripts, the fan presets (`fans-*.sh`), `jellyfin-software-ui.sh`,
  `build-systemdgenie.sh`
- `root-tests/` – scripts that need sudo (`setup-cool-performance.sh`, `chipset-probe.sh`,
  `usb-audio-check.sh`, and two older ones)
- `report/` – drafts for reporting this (kernel mailing list and ASUS), a redacted evidence
  bundle, and `HOW-TO-SEND.md`. Nothing has been sent.
- `data/logs/` – stall/temperature logs from the tests above
- `data/system-config/` – copies of the tuned profile and mapping, asusd fan curves and the old
  cap unit as they were on 2026-11-16
- `data/usbmon-volt-*.txt.gz` – the raw USB capture
- `data/phone-recordings/` – the three phone recordings of the crackle
- `data/systemdgenie-installed-files.txt` – what the systemdgenie build installed under `~/.local`

## Smart fan service

`rog-fand` samples the G814JZ every two seconds. In Performance, smoothed GPU watts select
minimum CPU/GPU/middle fan levels. Each point keeps the higher of the calibrated balanced PWM
and the requested floor. Two consecutive USB drop samples trigger a boost and raise the margin
for that power range. Status reports missing sensors. The conservative floor of at least 60/60/80 applies only when
GPU power, a temperature or a fan speed has been missing for three consecutive samples (6 s);
a single gap holds the last GPU reading. CPU power (RAPL) and usbmon are reported when missing
but never force the fallback, because they only feed the heat display and the feedback.
USB skips in the first second of a stream (after 2 s without traffic) are reported as
`warmup_skipped` and do not count as drops.

```sh
python3 -IB service/rog_fand.py --replay data/logs/10-fan-calibration-light.csv
python3 -IB service/rog_fand.py --dry-run --seconds 120
python3 -B -m unittest discover -s service
```

Dry run prints read-only live status to stdout. Replay uses the recorded GPU watts and timestamps.
The historical logs contain internal audio stalls. USB feedback uses live usbmon packet counts.

For live control, run `sudo service/install.sh install` from a trusted checkout. This installs the
root service and the **ROG Settings Panel** application launcher. Open the panel as your desktop
user and select **Auto**. The installer gives that user the mode control directory.
Scripts may still write one line to `/run/rog-fand/mode`:
`auto`, `off` (exact base), `preset:light` (35/40/70), `preset:heavy` (80/80/80), or
`preset:max` (100/100/100). Read `/run/rog-fand/status.json` or `journalctl -u rog-fand`.
The root-owned `/etc/rog-fand/config.json` holds the base, table and timing. An optional
`/etc/rog-fand/model.json` supplies a replacement `table` in the example config's format.
Set `log_csv` to true for per-minute sample files in `/var/log/rog-fand/`.

`service/install.sh status` shows unit status. `sudo service/install.sh undo` stops the service,
restores its base, and removes the installation, learned margins and sample logs. If a profile
switch interrupts restoration, the service saves the exact base in `/var/lib/rog-fand/restore.json`.
Return to Performance for recovery on the next service run, or repeat undo there.

Things to know once it is installed:
- In `auto` and the presets, the Performance curve is the configured base (in
  `/etc/rog-fand/config.json`) raised to the floors. Curve edits made elsewhere only stay while the
  mode is `off`.
- `/run` is cleared at boot, so the mode starts as `auto` after every reboot.
- Install with `sudo` or `pkexec`; the installer takes the desktop user from either. Without one,
  the mode can only be changed by root.

## Settings panel

A small local web page (`ui/`) that shows temperatures, fans, the live fan curve, CPU and GPU readings,
and switches the settings that already exist as scripts.

    python3 ui/rog-panel.py              # opens http://127.0.0.1:8765/ (also: ui/rog-panel.desktop)
    python3 ui/rog-panel.py --no-browser --port 8800
    python3 ui/rog-panel.py --dry-run    # actions only show the command they would run

- Fan preset: runs `tools/fans-original.sh`, `fans-ramped.sh`, `fans-mid-max.sh` or `fans-max.sh`; the panel says which one matches the live curve.
- Jellyfin software interface on/off: `tools/jellyfin-software-ui.sh`; applies at Jellyfin's next start (it is not restarted).
- CPU cap (40-100) and "Undo CPU profile": run `root-tests/setup-cool-performance.sh` through `pkexec`, so a system dialog asks for the password.
- Fan curve editor: pick a profile (defaults to the active one), drag the 8 points per fan on the chart or type temperature (C) and speed (%), then "Apply to <profile>" per fan or "Apply all three". Runs `asusctl fan-curve --mod-profile ... --fan ... --data ...` plus `--enable-fan-curves true`; the server rejects bad curves (8 points, temps 30-100 C strictly increasing, speed non-decreasing, last point at least 40 %). Shows non-blocking warnings (middle fan below 100 % from 70 C, GPU fan below 100 % from 80 C, profile not active). Built-in and custom presets can be loaded into the editor without applying; custom ones are saved in `ui/custom-presets.json` (up to 20). Stored curves are read from `/etc/asusd/fan_curves.ron`.
- Stall test: 10 s `tools/stall-counter.py`, shows stalls per second; refuses with "internal sound device busy" if the device is in use.

The server listens on 127.0.0.1 only, POSTs need a per-run token, and only the whitelisted commands above can run.

The native **Auto** tab shows the service mode, requested fan floors, measured RPM, heat input,
USB drops per device and learned margins. Select a measurement to chart its last five minutes.
**Apply mode** waits for acknowledgement from the service. **Preview live sensors** runs the
controller read-only in your desktop session and works before installation.

**Calibrate** offers Observe, Light, Heavy and Custom sessions under your current workload.
Observe measures stalls using the internal sound device. For a stepped run, select Performance
and set Auto to **Off · base curve** first. The calibration tool holds control through restoration
of the saved Performance curves and their enable flags. You can cancel a run, inspect per-step
mean and peak stalls, and export the complete or partial sample CSV. Quitting the panel during a
run waits for cleanup; closing the window only hides it to the tray. In panel dry-run mode, the start button previews the command.

**Tray icon** (`ui/fan_tray.py`). The panel puts the ROG logo that asusctl installs in the
system tray. The logo is loaded by icon name and not copied here. Its colour shows the service
state:

| Colour | Meaning |
|---|---|
| Green | Automatic control, no drops |
| Yellow | Fans boosted after USB drops, or ramping back down (about 4 minutes) |
| Orange | Sensor fallback: a sensor is missing and the fans are held at safe floors |
| Red | USB audio dropping packets now (start-up skips excluded), or the service stopped, stale or unreadable |
| Blue | Off, a manual preset, calibration in control, or a profile other than Performance |
| White | Service not installed or state unknown |

- **Tooltip:** floors, GPU power, board temperature and USB drops, plus the stall monitor's rate while it runs.
- **Menu:** open or hide the panel, fan mode, start or stop the stall monitor, and three settings:
  keep running in the tray when closed (on by default), notifications (on), and start in the tray
  at login (off; writes `~/.config/autostart/rog-panel-qt.desktop`).
- **Notifications:** each kind at most once per 5 minutes, for:
  - a USB drop boost, with the drop rate;
  - the service stopped, stale or unreadable for two polls in a row, running again, or restarted;
  - the sensor fallback starting or ending;
  - a mode change made outside this panel.
- **Background:** closing the window hides it to the tray. While hidden, only a 2 s read of
  `/run/rog-fand/status.json` keeps running. Use **Quit** in the tray menu to exit. If the stall
  monitor is running, hiding the window says so, because the monitor keeps the internal speakers
  in use. `--tray` starts hidden (used at login); `--no-tray` restores the old behaviour.

Native version (PySide6, launched as the desktop user):

    python3 ui/rog-panel-qt.py           # installed app menu entry: ROG Settings Panel
    python3 ui/rog-panel-qt.py --dry-run # shows a DRY RUN banner; actions only show the command they would run

It does the same as the web panel (readings every 2 s, presets, Jellyfin, CPU cap, stall test, fan-curve editor with
presets) using the same code (`ui/rogcore.py`), plus a "Monitor" tab: a live stall monitor (plays silence on the
internal sound device while on, so the internal speakers cannot play other sound; chart of stalls/s with the board
sensor temperature) and an on-demand "Check USB drops" (root, `pkexec`, runs `root-tests/usb-drop-count.sh`).
The web version still works unchanged. `ui/test_qt_offscreen.py` is an offscreen self-test (dry run only).

Checked on 2026-11-16: page rendering and polling in a browser; every button in `--dry-run`;
for real, re-applying the active fan preset, turning the Jellyfin setting on when already on, and
the "device busy" refusal of the stall test. Not yet exercised for real: the CPU cap and undo
(password dialog), a full stall-test run, switching to a different fan preset, and the launcher.

Fan-curve editor, checked on 2026-11-16 in a browser: charts render for all three fans, dragging a
point moves it and updates the number boxes, typed values update the chart, "Unsaved edits" and
the crackle warning appear, "Reload from saved" restores, presets save and delete, and Apply in
`--dry-run` sends the expected asusctl commands. For real: one apply of the CPU fan's unchanged
saved curve (no change to the live curves). Not yet exercised for real: applying an edited curve,
and editing the Balanced or Quiet profile.

Native window, checked on 2026-11-16: runs on the KDE desktop from the menu entry (the web panel's
menu entry was removed; `ui/rog-panel.desktop` is still in the folder). Offscreen self-test
`ui/test_qt_offscreen.py` passes. Real stall-monitor runs: clean readings (about 0.1 stalls/s),
aplay stopped on Stop and on window close, refusal when the internal sound device is busy, and a
clear message when aplay dies on its own. Fixed: the first one or two seconds after Start showed a
false peak (63.5/s) from the stream start-up burst; they are now dropped.
`root-tests/usb-drop-count.sh` reproduces the earlier capture's figures from the saved file
(9.47 % skipped, 84 events/s). Not yet run for real: the USB drop check (needs the password),
any fan or CPU action from the native window.

