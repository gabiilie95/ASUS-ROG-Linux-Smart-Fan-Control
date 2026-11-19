#!/usr/bin/env python3
"""Tests for noise_meter.py: no microphone or PipeWire needed."""
import array
import io
import math
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noise_meter as nm

PACTL = """Source #52
	State: SUSPENDED
	Name: alsa_output.usb-Universal_Audio_Volt_2-00.Direct__Direct__sink.monitor
	Description: Monitor of Volt 2 Direct Volt 2
Source #53
	State: SUSPENDED
	Name: alsa_input.usb-Universal_Audio_Volt_2_000000000000-00.Direct__Direct__source
	Description: Volt 2 Direct Volt 2
	Properties:
		device.description = "should not be read"
Source #57
	Name: alsa_input.pci-0000_00_1f.3.analog-stereo
	Description: Built-in Audio Analog Stereo
Source #60
	Name: bad name with spaces
	Description: Broken
"""


def sine(freq, seconds, amplitude=16384, rate=nm.RATE):
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * freq * n / rate))
                             for n in range(int(seconds * rate))))


class WeightingTest(unittest.TestCase):
    def test_matches_the_standard_curve(self):
        sections = nm.a_weighting()
        standard = {31.5: -39.4, 63: -26.2, 125: -16.1, 250: -8.6, 500: -3.2, 1000: 0.0,
                    2000: 1.2, 4000: 1.0, 6300: -0.1, 8000: -1.1}
        for freq, ref in standard.items():
            got = 20 * math.log10(nm.response(sections, freq))
            self.assertAlmostEqual(got, ref, delta=0.7, msg=f"{freq} Hz")

    def test_levels(self):
        w = nm.Weighting()
        w.level(sine(1000, 0.5))
        # A full-scale sine is -3 dBFS; half scale another -6 dB.
        self.assertAlmostEqual(w.level(sine(1000, 1)), -9.0, delta=0.3)
        low = nm.Weighting()
        low.level(sine(100, 0.5))
        self.assertLess(low.level(sine(100, 1)), -26, "100 Hz is weighted down by about 19 dB")
        self.assertEqual(nm.Weighting().level(array.array("h", [0] * 1000)), -200.0)

    def test_median_summary(self):
        self.assertEqual(nm.summarize([-40.0, -41.0, -10.0]), -40.0, "one loud second does not decide")
        self.assertEqual(nm.summarize([-40.0, -41.0]), -40.5)
        self.assertIsNone(nm.summarize([None, None]))
        self.assertIsNone(nm.summarize([]))


class SourceTest(unittest.TestCase):
    def test_parse_sources(self):
        sources = nm.parse_sources(PACTL)
        self.assertEqual([s["name"] for s in sources],
                         ["alsa_input.usb-Universal_Audio_Volt_2_000000000000-00.Direct__Direct__source",
                          "alsa_input.pci-0000_00_1f.3.analog-stereo"])
        self.assertEqual(sources[0]["description"], "Volt 2 Direct Volt 2")
        self.assertEqual([s["usable"] for s in sources], [True, False])

    def test_refuses_built_in_monitors_and_bad_names(self):
        with self.assertRaisesRegex(ValueError, "built-in"):
            nm.check_source("alsa_input.pci-0000_00_1f.3.analog-stereo")
        for bad in ("x.monitor", "a b", "", None, "x" * 300):
            with self.assertRaises(ValueError):
                nm.check_source(bad)
        name = "alsa_input.usb-Universal_Audio_Volt_2_000000000000-00.Direct__Direct__source"
        self.assertEqual(nm.check_source(name), name)


class FakeProc:
    def __init__(self, data):
        self.stdout = io.BytesIO(data)
        self.argv = None
        self.ended = False

    def poll(self):
        return 0 if self.ended else None

    def terminate(self):
        self.ended = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.ended = True


class MeterTest(unittest.TestCase):
    def test_one_level_per_second_and_no_audio_kept(self):
        data = sine(1000, 2.5).tobytes()
        procs = []
        def popen(argv, **kw):
            proc = FakeProc(data)
            proc.argv = argv
            procs.append(proc)
            return proc
        meter = nm.NoiseMeter("alsa_input.usb-Mic-00.mono", popen=popen)
        meter.thread.join(timeout=5)
        self.assertEqual(len(meter.levels), 2, "the last half second is not a full level")
        self.assertAlmostEqual(meter.level(), -9.0, delta=0.5)
        self.assertIsNone(meter.level(now=time.monotonic() + 10), "stale levels are not reported")
        argv = procs[0].argv
        self.assertEqual(argv[0], "parecord")
        self.assertIn("--raw", argv)
        self.assertIn("--device=alsa_input.usb-Mic-00.mono", argv)
        self.assertFalse(any(a.endswith((".wav", ".flac")) for a in argv), "nothing is written to a file")
        meter.close()
        self.assertTrue(procs[0].ended)

    def test_digital_silence_is_not_a_reading(self):
        meter = nm.NoiseMeter("alsa_input.usb-Dock-00.mono",
                              popen=lambda *a, **k: FakeProc(bytes(nm.RATE * 2 * 2)))
        meter.thread.join(timeout=5)
        self.assertEqual(len(meter.levels), 2)
        self.assertIsNone(meter.level(), "an unplugged or muted input gives no level")
        import threading
        with self.assertRaisesRegex(ValueError, "No signal"):
            meter.wait_signal(threading.Event(), timeout=0.5)
        meter.close()

    def test_built_in_is_refused_before_recording(self):
        started = []
        with self.assertRaisesRegex(ValueError, "built-in"):
            nm.NoiseMeter("alsa_input.pci-0000_00_1f.3.analog-stereo", popen=lambda *a, **k: started.append(1))
        self.assertEqual(started, [])


if __name__ == "__main__":
    unittest.main()
