#!/usr/bin/python3
"""Performance-profile fan controller for the ASUS G814JZ."""
import argparse
from collections import deque
import csv
from datetime import datetime, timezone
import io
import json
import logging
import math
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

# systemd runs Python in isolated mode. Import only from the installed sibling directory.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from rog_sensors import FANS, PROFILE, Sensors, UsbMonitor, read

VERSION = 1
RUN = Path("/run/rog-fand")
STATE = Path("/var/lib/rog-fand")
ETC = Path("/etc/rog-fand")
LOG = Path("/var/log/rog-fand")
ZERO = (0, 0, 0)
PRESETS = {"light": (35, 40, 70), "heavy": (80, 80, 80), "max": (100, 100, 100)}
MODES = {"auto", "off", *("preset:" + name for name in PRESETS)}


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
        cfg.update(overrides)
        try:
            cfg["table"] = read_json(ETC / "model.json")["table"]
        except FileNotFoundError:
            pass
    validate_base(cfg["base_curve"])
    triple(cfg["fallback"])
    triple(cfg["noise_weights"], 0, 100)
    for k in ("rise_s", "fall_s", "hold_s", "down_step_s", "clean_s"):
        number(cfg[k], 0.001, 86400)
    if type(cfg["log_csv"]) != bool:
        raise ValueError("log_csv must be a boolean")
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


def parse_mode(raw):
    mode = raw.decode("ascii").removesuffix("\n")
    if mode not in MODES:
        raise ValueError("unknown or malformed mode")
    return mode


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
    def __init__(self):
        self.floors = ZERO
        self.down_at = [None] * 3

    def step(self, target, now, cfg):
        levels = list(self.floors)
        for i, (old, wanted) in enumerate(zip(self.floors, target)):
            if wanted <= old - 5:
                if self.down_at[i] is None:
                    self.down_at[i] = now + cfg["hold_s"]
                if now >= self.down_at[i]:
                    levels[i] = old - 5
                    self.down_at[i] = now + cfg["down_step_s"]
            else:
                self.down_at[i] = None
                if wanted >= old + 5:
                    levels[i] = wanted
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
        self.ramp = Ramp()
        self.bad_samples = 0
        self.clean_since = self.clean_bin = None

    def step(self, now, gpu, cpu, *, active=False, traffic=False, drops=0, degraded=False,
             mode="auto", performance=True):
        dt = 0 if self.last is None else max(0, now - self.last)
        self.last = now
        self.gpu = smooth(self.gpu, gpu, dt, self.cfg)
        total = None if gpu is None or cpu is None else gpu + cpu
        self.heat = smooth(self.heat, total, dt, self.cfg)
        if not performance or mode == "off":
            self.reset_control()
            return ZERO, None

        row = next(row for row in reversed(self.cfg["table"])
                   if row["min_gpu_w"] <= (self.gpu or 0))
        key = str(row["min_gpu_w"])
        auto = mode == "auto"
        self.bad_samples = self.bad_samples + 1 if active and drops and auto else 0
        boosted = self.bad_samples >= 2
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
            margin = self.margins[key] if gpu is not None else 0
            target = [v + margin for v in row["floors"]]
        else:
            target = list(PRESETS[mode.removeprefix("preset:")])
        if degraded or gpu is None:
            target = [max(v, floor) for v, floor in zip(target, self.cfg["fallback"])]
        if boosted:
            target = [v + extra for v, extra in zip(target, (10, 10, 20))]
        return self.ramp.step([min(100, v) for v in target], now, self.cfg), event


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
