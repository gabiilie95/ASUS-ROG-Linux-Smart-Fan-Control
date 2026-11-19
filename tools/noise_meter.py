#!/usr/bin/env python3
"""Optional microphone noise meter for fan calibration and auto-tune.

Records mono 48 kHz audio from a PipeWire source with parecord and keeps only one A-weighted
level per second (dBFS(A)); no audio is stored. The levels are relative, not calibrated sound
pressure: they compare fan mixes within one session, with the microphone at the listening position
and the room otherwise quiet. A step is summarized by the median of its levels, so a short sound
such as a cough or a key press does not decide it.

The laptop's own HD-audio inputs (PipeWire names starting alsa_input.pci-) are refused: they sit on
the same controller whose interrupts are the stall counter, and recording would add interrupts.

    python3 tools/noise_meter.py --list
    python3 tools/noise_meter.py --source <name> --seconds 10
"""
import argparse
import array
import cmath
from collections import deque
import math
import re
import subprocess
import sys
import threading
import time

RATE = 48000           # within 0.2 dB of the standard curve up to 6.3 kHz; about 2 % of a core
BUILTIN_PREFIX = "alsa_input.pci-"
NAME_RE = re.compile(r"[A-Za-z0-9_.:@+-]{1,200}")
SILENCE_DB = -150.0     # at or below: digital silence (nothing connected, or muted), not a measurement
# Analog A-weighting (IEC 61672): poles in Hz.
F1, F2, F3, F4 = 20.598997, 107.65265, 737.86223, 12194.217


def _bilinear(b, a, fs):
    """Analog biquad (b2 s^2 + b1 s + b0) / (a2 s^2 + a1 s + a0) to digital (b0, b1, b2, a1, a2)."""
    k = 2.0 * fs
    b2, b1, b0 = b
    a2, a1, a0 = a
    nb = (b2 * k * k + b1 * k + b0, 2 * b0 - 2 * b2 * k * k, b2 * k * k - b1 * k + b0)
    na = (a2 * k * k + a1 * k + a0, 2 * a0 - 2 * a2 * k * k, a2 * k * k - a1 * k + a0)
    return tuple(x / na[0] for x in nb) + (na[1] / na[0], na[2] / na[0])


def response(sections, freq, fs=RATE):
    """|H| of a biquad cascade at `freq`."""
    z = cmath.exp(-2j * math.pi * freq / fs)
    h = 1
    for b0, b1, b2, a1, a2 in sections:
        h *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
    return abs(h)


def a_weighting(fs=RATE):
    """A-weighting as three biquads, normalized to 0 dB at 1 kHz. Near the Nyquist frequency the
    bilinear transform attenuates more than the standard (at 48 kHz: -0.6 dB at 8 kHz)."""
    w1, w2, w3, w4 = (2 * math.pi * f for f in (F1, F2, F3, F4))
    sections = [_bilinear((1, 0, 0), (1, 2 * w1, w1 * w1), fs),                 # s^2 / (s + w1)^2
                _bilinear((1, 0, 0), (1, w2 + w3, w2 * w3), fs),                # s^2 / (s + w2)(s + w3)
                _bilinear((0, 0, w4 * w4), (1, 2 * w4, w4 * w4), fs)]           # w4^2 / (s + w4)^2
    gain = 1 / response(sections, 1000, fs)
    b0, b1, b2, a1, a2 = sections[0]
    sections[0] = (b0 * gain, b1 * gain, b2 * gain, a1, a2)
    return sections


class Weighting:
    """Streaming A-weighted level: feed int16 samples, get dBFS(A) of what was fed."""

    def __init__(self, fs=RATE):
        self.sections = a_weighting(fs)
        self.state = [[0.0, 0.0] for _ in self.sections]

    def level(self, samples):
        """dBFS(A) of `samples` (int16 values); updates the filter state."""
        total = 0.0
        coeffs = self.sections
        state = self.state
        for x in samples:
            y = x / 32768.0
            for (b0, b1, b2, a1, a2), s in zip(coeffs, state):
                # Transposed direct form II.
                out = b0 * y + s[0]
                s[0] = b1 * y - a1 * out + s[1]
                s[1] = b2 * y - a2 * out
                y = out
            total += y * y
        mean = total / max(1, len(samples))
        return 10 * math.log10(mean) if mean > 1e-20 else -200.0


def median(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summarize(levels):
    """A step's noise: the median 1 s level, rounded to 0.1 dB, or None without levels."""
    value = median(levels)
    return None if value is None else round(value, 1)


def check_source(name):
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name.endswith(".monitor"):
        raise ValueError("Choose a microphone input by its PipeWire source name")
    if name.startswith(BUILTIN_PREFIX):
        raise ValueError("The laptop's built-in microphone shares the sound chip used by the stall "
                         "counter; use a USB microphone or audio interface instead")
    return name


def parse_sources(text):
    """Inputs from `pactl list sources`: [{name, description, usable}], monitors left out."""
    sources, current = [], None
    for line in text.splitlines():
        stripped = line.strip()
        if line.startswith("Source #"):
            current = {}
            sources.append(current)
        elif current is not None and stripped.startswith("Name:"):
            current["name"] = stripped.split(":", 1)[1].strip()
        elif current is not None and stripped.startswith("Description:") and "description" not in current:
            current["description"] = stripped.split(":", 1)[1].strip()
    out = []
    for s in sources:
        name = s.get("name", "")
        if not name or name.endswith(".monitor") or not NAME_RE.fullmatch(name):
            continue
        out.append(dict(name=name, description=s.get("description") or name,
                        usable=not name.startswith(BUILTIN_PREFIX)))
    return out


def list_sources(run=subprocess.run):
    try:
        out = run(["pactl", "list", "sources"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_sources(out or "")


class NoiseMeter:
    """Records from `source` in the background and keeps one A-weighted level per second."""

    def __init__(self, source, rate=RATE, popen=subprocess.Popen, keep=900):
        self.source = check_source(source)
        self.rate = rate
        self.levels = deque(maxlen=keep)
        self.lock = threading.Lock()
        self.error = None
        self.proc = popen(["parecord", "--raw", "--format=s16le", f"--rate={rate}", "--channels=1",
                           f"--device={self.source}", "--client-name=rog-fand noise meter",
                           "--stream-name=fan noise level (levels only)"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._read, name="noise-meter", daemon=True)
        self.thread.start()

    def _read(self):
        weighting = Weighting(self.rate)
        chunk = self.rate * 2           # one second of s16 mono
        pending = b""
        try:
            while True:
                data = self.proc.stdout.read(chunk - len(pending))
                if not data:
                    break
                pending += data
                if len(pending) < chunk:
                    continue
                samples = array.array("h")
                samples.frombytes(pending)
                if sys.byteorder != "little":
                    samples.byteswap()
                pending = b""
                level = weighting.level(samples)
                with self.lock:
                    self.levels.append((time.monotonic(), level))
        except (OSError, ValueError) as exc:
            self.error = str(exc)

    def alive(self):
        return self.proc.poll() is None

    def level(self, max_age=3.0, now=None):
        """The latest 1 s level, or None when none is recent (the recording stalled or ended) or the
        input is digitally silent."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.levels:
                return None
            t, value = self.levels[-1]
        return round(value, 1) if now - t <= max_age and value > SILENCE_DB else None

    def wait_signal(self, stop, timeout=5.0):
        """Wait until a real level arrives; raise ValueError if none does (silent or failed input).
        `stop` is a threading.Event; returns False if it was set while waiting."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if stop.wait(0.25):
                return False
            if self.level() is not None:
                return True
            if not self.alive():
                break
        raise ValueError("No signal from the microphone %s: check that it is connected and not muted"
                         % self.source)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.thread.join(timeout=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="list microphone inputs")
    ap.add_argument("--source", help="PipeWire source name")
    ap.add_argument("--seconds", type=int, default=10)
    a = ap.parse_args(argv)
    if a.list or not a.source:
        for s in list_sources():
            print(("  " if s["usable"] else "x ") + s["name"] + "  (" + s["description"] + ")"
                  + ("" if s["usable"] else "  [built-in: not usable with the stall counter]"))
        return 0
    try:
        meter = NoiseMeter(a.source)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        for _ in range(a.seconds):
            time.sleep(1)
            print(meter.level() if meter.level() is not None else "--", flush=True)
            if not meter.alive():
                print("recording ended", file=sys.stderr)
                return 1
    finally:
        meter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
