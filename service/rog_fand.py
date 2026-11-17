#!/usr/bin/python3
"""Fan controller for the ASUS G814JZ: raises fan floors in the protected platform profiles."""
import argparse
from collections import deque
import csv
import fcntl
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

# systemd runs Python in isolated mode. Import only from the installed sibling directory.
HERE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(HERE))
from rog_sensors import FANS, PROFILE, Sensors, UsbMonitor, read

VERSION = 1
RUN = Path("/run/rog-fand")
STATE = Path("/var/lib/rog-fand")
ETC = Path("/etc/rog-fand")
LOG = Path("/var/log/rog-fand")
ZERO = (0, 0, 0)
PRESETS = {"light": (35, 40, 70), "heavy": (80, 80, 80), "max": (100, 100, 100)}
# Platform profiles asusd keeps fan curves for. Performance always runs the configured base curve;
# in the others the service leaves the laptop's own curves alone until the floors are above zero.
CONTROL_PROFILES = ("performance", "balanced", "quiet")
STORED_RE = re.compile(r"fan:\s*(\w+),\s*pwm:\s*\(([^)]*)\),\s*temp:\s*\(([^)]*)\),\s*enabled:\s*(true|false)")
MODES = {"auto", "off", *("preset:" + name for name in PRESETS)}
# Readings the decision depends on. RAPL (reported heat only) and usbmon (feedback only) are
# deliberately absent: losing them must not force the loud fallback.
ESSENTIAL = ("gpu_w", "cpu_c", "board_c", "cpu_rpm", "gpu_rpm", "mid_rpm")
# A reading counts as failed only after this many consecutive missing samples (2 s each). A single
# gap at start-up or a momentary NVML error would otherwise jump to the fallback and, through the
# 60 s hold and 5-point steps, keep the fans loud for about six minutes.
MISSING_LIMIT = 3
# Timing defaults before 2026-11-17. An installed config.json was generated as a full copy of the
# example, so values equal to these are treated as unset and pick up the current defaults.
OLD_TIMING_DEFAULTS = {"fall_s": 60, "hold_s": 60, "down_step_s": 20, "clean_s": 1800}


def atomic_write(path, text, mode=0o600, owner=None):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            if owner:
                os.fchown(out.fileno(), owner.pw_uid, owner.pw_gid)
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_json(path):
    return json.loads(path.read_text())


def number(value, low, high):
    if type(value) not in (int, float) or not low <= value <= high or not math.isfinite(value):
        raise ValueError(f"expected a number between {low} and {high}")
    return value


def triple(values, low=0, high=100):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError("expected three values in cpu/gpu/mid order")
    return tuple(number(v, low, high) for v in values)


def validate_base(base):
    if set(base) != set(FANS):
        raise ValueError("base needs cpu, gpu and mid curves")
    for fan, points in base.items():
        if len(points) != 8:
            raise ValueError(f"{fan} needs eight points")
        prev_t, prev_pwm = 29, 0
        for temp, pwm in points:
            if type(temp) != int or type(pwm) != int:
                raise ValueError("curve points must be integers")
            if not prev_t < temp <= 100 or not prev_pwm <= pwm <= 255:
                raise ValueError(f"invalid {fan} temperature or PWM sequence")
            prev_t, prev_pwm = temp, pwm
        if prev_pwm != 255:
            raise ValueError(f"{fan} must reach full PWM")


def build_curves(base, floors):
    validate_base(base)
    return {fan: [(temp, max(pwm, math.ceil(pct * 255 / 100))) for temp, pwm in base[fan]]
            for fan, pct in zip(FANS, triple(floors))}


def config(installed=False):
    cfg = read_json(HERE / "config.example.json")
    if installed:
        overrides = read_json(ETC / "config.json")
        if not isinstance(overrides, dict) or overrides.keys() - cfg.keys():
            raise ValueError("invalid config keys")
        cfg.update(migrate_timing(overrides))
        try:
            cfg["table"] = read_json(ETC / "model.json")["table"]
        except FileNotFoundError:
            pass
    validate_base(cfg["base_curve"])
    triple(cfg["fallback"])
    triple(cfg["noise_weights"], 0, 100)
    for k in ("rise_s", "fall_s", "hold_s", "down_step_s", "hot_down_step_s", "clean_s"):
        number(cfg[k], 0.001, 86400)
    number(cfg["max_down_step"], 5, 100)
    number(cfg["max_up_step"], 5, 100)
    number(cfg["hot_board_c"], 30, 110)
    number(cfg["quick_boost_drops"], 1, 1e6)
    if type(cfg["log_csv"]) != bool:
        raise ValueError("log_csv must be a boolean")
    profiles = cfg["profiles"]
    if (not isinstance(profiles, list) or len(set(profiles)) != len(profiles)
            or not set(profiles) <= set(CONTROL_PROFILES)):
        raise ValueError("profiles must list distinct names from " + ", ".join(CONTROL_PROFILES))
    user = cfg.get("control_user")
    if user is not None:
        if not isinstance(user, str):
            raise ValueError("control_user must be a user name or null")
        try:
            pwd.getpwnam(user)
        except KeyError:
            raise ValueError(f"control_user {user!r} does not exist") from None
    last = -1
    for row in cfg["table"]:
        threshold = number(row["min_gpu_w"], 0, 1000)
        if threshold <= last or (last == -1 and threshold != 0):
            raise ValueError("GPU thresholds must start at zero and increase")
        triple(row["floors"])
        last = threshold
    if last == -1:
        raise ValueError("empty GPU table")
    return cfg


def migrate_timing(overrides):
    """Drop timing values that are just the old defaults, so updates bring the new ones."""
    return {k: v for k, v in overrides.items() if OLD_TIMING_DEFAULTS.get(k, object()) != v}


def parse_mode(raw):
    mode = raw.decode("ascii").removesuffix("\n")
    if mode not in MODES:
        raise ValueError("unknown or malformed mode")
    return mode


class Gaps:
    """Track consecutive missing essential readings and briefly hold the last GPU power."""
    def __init__(self):
        self.missing = dict.fromkeys(ESSENTIAL, 0)
        self.last_gpu = None

    def update(self, values):
        for key in ESSENTIAL:
            # Keys a caller does not provide are not counted as missing.
            if key in values:
                self.missing[key] = self.missing[key] + 1 if values[key] is None else 0
        gpu = values.get("gpu_w")
        if gpu is not None:
            self.last_gpu = gpu
        elif self.missing["gpu_w"] < MISSING_LIMIT:
            gpu = self.last_gpu
        degraded = any(count >= MISSING_LIMIT for count in self.missing.values())
        return gpu, degraded


def read_mode(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("expected a regular mode file")
        return parse_mode(os.read(fd, 64))
    finally:
        os.close(fd)


def smooth(previous, value, dt, cfg):
    if value is None or previous is None:
        return value
    tau = cfg["rise_s"] if value > previous else cfg["fall_s"]
    return previous + (value - previous) * -math.expm1(-dt / tau)


class Ramp:
    """Up quickly (at most max_up_step points per sample, so a game starting is a quick ramp, not a
    jump); down after a hold, in steps. Steps cover a third of the gap (5 to
    max_down_step points) every down_step_s, so large gaps close quickly and the last points
    gently. While the board is heat-soaked (hot_board_c and above) it keeps the cautious pace of
    5 points every hot_down_step_s. A mode the user picks skips the hold."""
    def __init__(self):
        self.floors = ZERO
        self.down_at = [None] * 3

    def step(self, target, now, cfg, board=None, skip_hold=False):
        hot = board is not None and board >= cfg["hot_board_c"]
        levels = list(self.floors)
        for i, (old, wanted) in enumerate(zip(self.floors, target)):
            # Start going down only past the 5-point dead band; once going, finish at the target.
            if wanted <= old - 5 or (wanted < old and self.down_at[i] is not None):
                if self.down_at[i] is None or skip_hold:
                    self.down_at[i] = min(self.down_at[i] or math.inf, now if skip_hold else now + cfg["hold_s"])
                if now >= self.down_at[i]:
                    step = 5 if hot else min(cfg["max_down_step"], max(5, math.ceil((old - wanted) / 3)))
                    levels[i] = max(wanted, old - step)
                    self.down_at[i] = now + (cfg["hot_down_step_s"] if hot else cfg["down_step_s"])
            else:
                self.down_at[i] = None
                if wanted >= old + 5:
                    levels[i] = min(wanted, old + cfg["max_up_step"])
        self.floors = tuple(levels)
        return self.floors


class Policy:
    def __init__(self, cfg, saved=None):
        self.cfg = cfg
        self.gpu = self.heat = self.last = None
        self.margins = {str(row["min_gpu_w"]): 0 for row in cfg["table"]}
        if saved is not None:
            for key in self.margins.keys() & saved.keys():
                self.margins[key] = number(saved[key], 0, 20)
        self.reset_control()

    def reset_control(self):
        self.mode = None
        self.ramp = Ramp()
        self.bad_samples = 0
        self.clean_since = self.clean_bin = None

    def step(self, now, gpu, cpu, *, active=False, traffic=False, drops=0, degraded=False,
             mode="auto", controlled=True, board=None):
        dt = 0 if self.last is None else max(0, now - self.last)
        self.last = now
        self.gpu = smooth(self.gpu, gpu, dt, self.cfg)
        total = None if gpu is None or cpu is None else gpu + cpu
        self.heat = smooth(self.heat, total, dt, self.cfg)
        if not controlled or mode == "off":
            self.reset_control()
            return ZERO, None

        row = next(row for row in reversed(self.cfg["table"])
                   if row["min_gpu_w"] <= (self.gpu or 0))
        key = str(row["min_gpu_w"])
        margin = self.margins[key] if gpu is not None else 0
        auto = mode == "auto"
        # The user picking another mode is a decision, not a load dip: no hold before stepping down.
        mode_changed = self.mode is not None and mode != self.mode
        self.mode = mode
        self.bad_samples = self.bad_samples + 1 if active and drops and auto else 0
        # Two drop samples in a row, or one large burst (real stalls skip hundreds of packets).
        boosted = self.bad_samples >= 2 or (active and auto and drops >= self.cfg["quick_boost_drops"])
        event = None
        if boosted:
            if gpu is not None:
                self.margins[key] = min(20, self.margins[key] + 5)
            self.bad_samples = 0
            event = "USB drop boost"
        if auto and active and traffic and not drops and not degraded and gpu is not None:
            if self.clean_bin != key:
                self.clean_bin, self.clean_since = key, now
            elif now - self.clean_since >= self.cfg["clean_s"]:
                self.margins[key] = max(0, self.margins[key] - 2.5)
                self.clean_since = now
        else:
            self.clean_since = self.clean_bin = None

        if auto:
            # A boost adds 10/10/20 to the previous baseline; learn the new baseline for later.
            if not boosted and gpu is not None:
                margin = self.margins[key]
            target = [v + margin for v in row["floors"]]
        else:
            target = list(PRESETS[mode.removeprefix("preset:")])
        if degraded or gpu is None:
            target = [max(v, floor) for v, floor in zip(target, self.cfg["fallback"])]
        if boosted:
            target = [v + extra for v, extra in zip(target, (10, 10, 20))]
        return self.ramp.step([min(100, v) for v in target], now, self.cfg, board, mode_changed), event


def replay(path, cfg):
    policy = Policy(cfg)
    previous_t = None
    now = 0
    with path.open(newline="") as src:
        rows = csv.DictReader(src)
        if not {"t", "gpu_w"} <= set(rows.fieldnames or []):
            raise ValueError("CSV needs t and gpu_w columns")
        for row in rows:
            stamp = datetime.strptime(row["t"], "%H:%M:%S")
            seconds = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
            if previous_t is not None:
                now += (seconds - previous_t) % 86400
            previous_t = seconds
            try:
                gpu = number(float(row["gpu_w"]), 0, 1000)
            except (TypeError, ValueError):
                gpu = None
            # These logs predate RAPL and usbmon. Exercise GPU feed-forward on their timeline.
            floors, _ = policy.step(now, gpu, None)
            yield dict(t=row["t"], gpu_w=gpu, smoothed_gpu_w=rounded(policy.gpu),
                       floors=dict(zip(FANS, floors)))


def rounded(value):
    return None if value is None else round(value, 2)


def write_curves(curves, restoring=False, previous=None, profile="performance", enabled=True):
    """Write `curves` (None: flags only) and the enable flags (bool, or a bool per fan) to `profile`.
    Recheck before *each* command: a profile switch can interrupt a three-fan update, and asusd only
    writes a profile's curves to the hardware while that profile is active."""
    args = [] if curves is None else [
        ["--fan", fan, "--data", ",".join(f"{t}c:{p}" for t, p in curves[fan])]
        for fan in FANS if previous is None or curves[fan] != previous[fan]]
    flags = enabled if isinstance(enabled, dict) else dict.fromkeys(FANS, bool(enabled))
    if len(set(flags.values())) == 1:
        args.append(["--enable-fan-curves", "true" if flags["cpu"] else "false"])
    else:
        args += [["--fan", fan, "--enable-fan-curve", "true" if flags[fan] else "false"] for fan in FANS]
    failures = []
    for suffix in args:
        if read(PROFILE) != profile:
            logging.warning("%s profile inactive; curve write deferred", profile.capitalize())
            return False
        try:
            subprocess.run(["/usr/bin/asusctl", "fan-curve", "--mod-profile", profile,
                            *suffix], check=True, timeout=10)
        except (OSError, subprocess.SubprocessError) as exc:
            if not restoring:
                raise
            failures.append(str(exc))
    if failures:
        raise RuntimeError("base restore incomplete: " + ", ".join(failures))
    return True


def check_stored(stored):
    """A profile's own curves and enable flags, as asusd reports them (points may repeat)."""
    curves, enabled = stored["curves"], stored["enabled"]
    if set(curves) != set(FANS) or set(enabled) != set(FANS):
        raise ValueError("stored curves need cpu, gpu and mid")
    for fan, points in curves.items():
        if len(points) != 8 or type(enabled[fan]) != bool:
            raise ValueError(f"invalid stored {fan} curve")
        prev_t, prev_pwm = 0, 0
        for temp, pwm in points:
            if type(temp) != int or type(pwm) != int or not prev_t <= temp <= 120 or not 0 <= pwm <= 255:
                raise ValueError(f"invalid stored {fan} curve")
            prev_t = temp
    return curves, enabled


def read_stored(profile):
    """Read a profile's curves and enable flags from asusd before the service changes them."""
    out = subprocess.run(["/usr/bin/asusctl", "fan-curve", "--mod-profile", profile], check=True,
                         timeout=10, capture_output=True, text=True).stdout
    curves, enabled = {}, {}
    for name, pwm, temp, flag in STORED_RE.findall(out):
        fan = name.lower()
        temps = [int(x) for x in temp.replace(",", " ").split()]
        pwms = [int(x) for x in pwm.replace(",", " ").split()]
        if fan not in FANS or len(temps) != len(pwms):
            raise ValueError("unexpected asusctl fan-curve output")
        curves[fan] = [[t, p] for t, p in zip(temps, pwms)]
        enabled[fan] = flag == "true"
    stored = {"curves": curves, "enabled": enabled}
    check_stored(stored)
    return stored


class Actuator:
    """Root-only receipts survive partial writes, process death and profile changes.

    Performance's receipt is the configured base curve (enabled), as before. Another profile's
    receipt holds the curves and enable flags asusd had for it before the service raised its fans."""
    def __init__(self):
        self.receipt = STATE / "restore.json"
        self.last = None
        self.last_profile = None

    def receipt_for(self, profile):
        return self.receipt if profile == "performance" else STATE / f"restore-{profile}.json"

    def pending(self, profile=None):
        """Profiles whose receipt still waits (or whether `profile`'s does)."""
        waiting = [p for p in CONTROL_PROFILES if self.receipt_for(p).exists()]
        return waiting if profile is None else profile in waiting

    def restore_profile(self, profile):
        """Restore `profile` if a receipt waits for it. Only possible while it is active."""
        path = self.receipt_for(profile)
        try:
            data = read_json(path)
        except FileNotFoundError:
            return True
        if profile == "performance":
            validate_base(data)
            if not write_curves(data, restoring=True):
                return False
            last = build_curves(data, ZERO)
        else:
            curves, enabled = check_stored(data)
            # A profile whose curves were disabled runs the laptop's own curve; switching custom
            # curves off again restores that. Its stored points are rewritten only when in use.
            if not write_curves(curves if any(enabled.values()) else None, restoring=True,
                                profile=profile, enabled=enabled):
                return False
            last = None
        path.unlink()
        self.last, self.last_profile = last, profile if last else None
        logging.info("restored exact %s curve", profile)
        return True

    def restore(self):
        """Restore what the active profile allows; True when no receipt is left anywhere."""
        active = read(PROFILE)
        done = True
        for profile in self.pending():
            done = (profile == active and self.restore_profile(profile)) and done
        return done

    def apply(self, base, floors, profile="performance", stored=None):
        curves = build_curves(base, floors)
        if curves == self.last and profile == self.last_profile:
            return True
        path = self.receipt_for(profile)
        if not path.exists():
            if profile != "performance":
                if stored is None:
                    raise ValueError(f"the {profile} profile's own curves are needed before changing them")
                check_stored(stored)
            atomic_write(path, json.dumps(base if profile == "performance" else stored))
        previous = self.last if profile == self.last_profile else None
        self.last = None
        if not write_curves(curves, previous=previous, profile=profile):
            return False
        self.last, self.last_profile = curves, profile
        if profile == "performance" and not any(floors):
            path.unlink()
        return True


class SampleCsv:
    """Publish each minute's bounded CSV atomically, including the current partial minute."""
    def __init__(self):
        self.minute = self.path = self.buffer = self.writer = None

    def add(self, timestamp, sensors, floors, usb):
        minute = timestamp[:16]
        if self.minute != minute:
            self.minute = minute
            self.path = LOG / (timestamp.replace(":", "-") + ".csv")
            self.buffer = io.StringIO()
            self.writer = csv.writer(self.buffer)
            self.writer.writerow(["t", *sensors, "cpu_floor", "gpu_floor", "mid_floor",
                                  "audio_active", "skipped"])
        self.writer.writerow([timestamp[11:19], *sensors.values(), *floors, usb["audio_active"],
                              sum(d["skipped"] for d in usb["devices"])])
        atomic_write(self.path, self.buffer.getvalue())


def init_mode(cfg):
    user = cfg["control_user"]
    owner = pwd.getpwnam(user) if user else None
    # Only the control directory is replaceable by the desktop user. Status stays root-owned.
    public = RUN / "mode"
    directory = RUN / "control"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "mode"
    old = public if not public.is_symlink() and public.exists() else path
    mode = "auto"
    try:
        if old.lstat().st_uid == (owner.pw_uid if owner else 0):
            mode = read_mode(old)
    except (OSError, ValueError):
        pass
    # Stage privileged writes under the root-owned parent, never inside the user directory.
    with tempfile.TemporaryDirectory(dir=RUN) as staging:
        tmp = Path(staging) / "mode"
        atomic_write(tmp, mode + "\n", 0o644, owner)
        os.replace(tmp, path)
        tmp.symlink_to("control/mode")
        os.replace(tmp, public)
    os.chmod(directory, 0o700)
    if owner:
        os.chown(directory, owner.pw_uid, owner.pw_gid)
    elif os.geteuid() == 0:
        os.chown(directory, 0, 0)
    return path


def run_service(dry, duration):
    cfg = config(installed=not dry)
    actuator = None if dry else Actuator()
    saved = {}
    if not dry:
        try:
            saved = read_json(STATE / "state.json")["margins"]
        except FileNotFoundError:
            pass
        mode_path = init_mode(cfg)
    policy = Policy(cfg, saved)
    sensors = Sensors()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    usbmon = UsbMonitor(stop)
    samples = SampleCsv() if cfg["log_csv"] and not dry else None
    gaps = Gaps()
    events = deque(maxlen=12)
    mode, last_mode_error = "auto", None
    last_profile = last_problems = last_decision = None
    actuated = object()       # the profile the actuator last saw; forces a recovery check at start
    needs_recovery = False

    def event(message):
        events.append({"t": timestamp, "message": message})
        logging.info("%s", message)

    start = time.monotonic()
    until = start + duration if duration else math.inf
    try:
        usbmon.start()
        while not stop.is_set() and time.monotonic() < until:
            now = time.monotonic()
            timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            values = sensors.sample(now)
            usb = usbmon.snapshot(time.monotonic())
            profile = values["profile"]
            if profile != last_profile:
                event(f"platform profile: {profile}")
                last_profile = profile
            if not dry:
                try:
                    selected = read_mode(mode_path)
                    last_mode_error = None
                    if mode != selected:
                        if mode == "off":
                            # A manual editor may have changed any point while control was released.
                            actuator.last = None
                        event(f"mode: {selected}")
                    mode = selected
                except (OSError, ValueError) as exc:
                    # Never put user-supplied bytes into the journal.
                    if last_mode_error != type(exc):
                        event("invalid mode file; keeping " + mode)
                    last_mode_error = type(exc)
            problems = [("RAPL CPU W unreadable or awaiting delta" if k == "cpu_w"
                         else f"{k} unavailable") for k, v in values.items() if v is None]
            if usb["error"]:
                problems.append(usb["error"])
            if problems != last_problems:
                event(", ".join(problems) if problems else "all sensors available")
                last_problems = problems
            before = policy.margins.copy()
            controlled = profile in cfg["profiles"]
            gpu, degraded = gaps.update(values)
            floors, boost = policy.step(
                now, gpu, values["cpu_w"], active=usb["audio_active"],
                traffic=bool(usb["devices"]),
                # Skips in a stream's first second are start-up artefacts, not chipset stalls.
                drops=sum(d["skipped"] - d.get("warmup_skipped", 0) for d in usb["devices"]),
                degraded=degraded, mode=mode, controlled=controlled, board=values.get("board_c"))
            if boost:
                event(boost)
            if before != policy.margins:
                event(f"GPU-bin margins: {policy.margins}")
                if not dry:
                    atomic_write(STATE / "state.json", json.dumps({"margins": policy.margins}))
            applied = manual_control = False
            if actuator:
                if profile != actuated:
                    # asusd rewrites a profile's own curves when it becomes active.
                    actuator.last = actuator.last_profile = None
                    needs_recovery = profile in CONTROL_PROFILES and actuator.pending(profile)
                    actuated = profile
                if profile in CONTROL_PROFILES:
                    # A desktop calibration takes this lease only after Off is acknowledged.
                    # It holds the lease through exact-curve restoration on finish or cancel.
                    lock = os.open(mode_path.parent, os.O_DIRECTORY)
                    try:
                        try:
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            manual_control = True
                        else:
                            if needs_recovery:
                                needs_recovery = not actuator.restore_profile(profile)
                            if needs_recovery:
                                pass    # the receipt stays; retried next sample
                            elif controlled and (profile == "performance" or any(floors)):
                                stored = None
                                if profile != "performance" and not actuator.pending(profile):
                                    stored = read_stored(profile)
                                applied = actuator.apply(cfg["base_curve"], floors, profile, stored)
                                needs_recovery = not applied
                            elif actuator.pending(profile):
                                # Floors are back at zero (or Off): hand the profile its own curves back.
                                needs_recovery = not actuator.restore_profile(profile)
                    finally:
                        os.close(lock)
            decision = floors if controlled else None
            if decision != last_decision:
                event(f"{'planned' if dry else 'requested'} floors (% cpu/gpu/mid): {decision}")
                last_decision = decision
            status = dict(version=VERSION, t=timestamp, elapsed_s=round(now-start, 2),
                          dry_run=dry, mode=mode, invalid_mode=last_mode_error is not None,
                          controlled=controlled, profiles=cfg["profiles"],
                          sensors=values, unavailable=problems, degraded=degraded, heat_w=rounded(policy.heat),
                          smoothed_gpu_w=rounded(policy.gpu), floors=dict(zip(FANS, floors)),
                          applied=applied, manual_control=manual_control, margins=policy.margins, usb=usb, events=list(events))
            if dry:
                print(json.dumps(status), flush=True)
            else:
                atomic_write(RUN / "status.json", json.dumps(status) + "\n", 0o644)
            if samples:
                samples.add(timestamp, values, floors, usb)
            stop.wait(max(0, min(now + 2, until) - time.monotonic()))
    finally:
        stop.set()
        try:
            if actuator and not actuator.restore():
                logging.warning("restore pending for %s; select it and run --restore",
                                ", ".join(actuator.pending()))
        finally:
            if usbmon.thread.is_alive():
                usbmon.thread.join(timeout=2)
            sensors.gpu.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="read sensors and print JSON only")
    modes.add_argument("--replay", type=Path, metavar="CSV", help="replay recorded GPU decisions")
    modes.add_argument("--restore", action="store_true", help="finish a pending base restore")
    modes.add_argument("--restore-on-stop", action="store_true",
                       help="systemd cleanup: keep inactive-profile receipts for later recovery")
    parser.add_argument("--seconds", type=int, help="end the dry run after this many seconds")
    args = parser.parse_args(argv)
    if args.seconds is not None and (not args.dry_run or args.seconds <= 0):
        parser.error("--seconds requires --dry-run and a positive duration")
    if args.replay:
        for row in replay(args.replay, config()):
            print(json.dumps(row))
    elif args.dry_run:
        run_service(True, args.seconds)
    elif os.geteuid() != 0:
        parser.error("live control requires root; use --dry-run or --replay")
    else:
        # Serialize manual restore, service startup, and systemd stop-post recovery.
        lock = os.open(STATE, os.O_DIRECTORY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if args.restore or args.restore_on_stop:
                actuator = Actuator()
                if actuator.restore():
                    return 0
                print("restore pending for " + ", ".join(actuator.pending()) + "; select that profile "
                      "and run --restore again", file=sys.stderr)
                # Deferred profiles remain recoverable on their next activation. Actual command
                # or receipt errors still raise, so systemd reports failed cleanup.
                return 0 if args.restore_on_stop else 1
            run_service(False, None)
        finally:
            os.close(lock)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    sys.exit(main())
