#!/usr/bin/env python3
"""Tests for sensor_scan.py against a fake sysfs tree (no real sensors read)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sensor_scan as ss  # noqa: E402


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text) + "\n")


class FakeSys:
    def __init__(self, root):
        self.root = Path(root)
        self.n = 0

    def chip(self, name, device, **files):
        d = self.root / "class" / "hwmon" / f"hwmon{self.n}"
        self.n += 1
        write(d / "name", name)
        target = self.root / "devices" / "fake" / device
        target.mkdir(parents=True, exist_ok=True)
        os.symlink(target, d / "device")
        for key, value in files.items():
            write(d / key, value)
        return d

    def zone(self, n, kind, temp, trips=()):
        z = self.root / "class" / "thermal" / f"thermal_zone{n}"
        write(z / "type", kind)
        write(z / "temp", temp)
        for i, (trip, value) in enumerate(trips):
            write(z / f"trip_point_{i}_type", trip)
            write(z / f"trip_point_{i}_temp", value)


def laptop(root):
    f = FakeSys(root)
    f.chip("coretemp", "coretemp.0", temp1_input=63000, temp1_label="Package id 0", temp1_max=100000,
           temp1_crit=100000, temp2_input=52000, temp2_label="Core 0", temp6_input=71000, temp6_label="Core 4")
    f.chip("spd5118", "12-0052", temp1_input=55750, temp1_max=55000, temp1_crit=85000)
    f.chip("nvme", "nvme1", temp1_input=42850, temp1_label="Composite", temp1_max=86850, temp1_crit=89850,
           temp2_input=42850, temp2_label="Sensor 1", temp2_max=65261850)
    f.chip("asus", "asus-nb-wmi", fan1_input=2900, fan1_label="cpu_fan", fan2_input=2500, fan2_label="gpu_fan",
           fan3_input=4100, fan3_label="mid_fan")
    f.chip("acpitz", "thermal_zone0", temp1_input=27800)
    f.chip("spd5118", "12-0050", temp1_input=54000, temp1_max=55000, temp1_crit=85000)
    f.chip("nvme", "nvme0", temp1_input=43850, temp1_label="Composite", temp1_max=89850, temp1_crit=93850)
    f.chip("BAT0", "BAT0", in0_input=16122, power1_input=12500000)
    f.chip("r8169_0_6c00:00", "r8169-0-6c00:00", temp1_input=49500, temp1_max=120000)
    f.chip("r8169_0_7c00:00", "r8169-0-7c00:00", temp1_input="garbage")
    f.chip("mystery", "mystery.0", temp1_input=40000, in0_input=1200, pwm1=128)
    f.chip("ADP0", "ADP0")
    f.zone(0, "acpitz", 27800)
    f.zone(1, "INT3400 Thermal", 20000)
    f.zone(2, "SEN3", 56050, [("passive", -274000), ("critical", 80050), ("hot", 75050), ("passive", 65050)])
    f.zone(6, "x86_pkg_temp", 64000)
    write(f.root / "class" / "power_supply" / "ADP0" / "type", "Mains")
    write(f.root / "class" / "power_supply" / "ADP0" / "online", 1)
    write(f.root / "class" / "power_supply" / "BAT0" / "type", "Battery")
    write(f.root / "class" / "power_supply" / "BAT0" / "capacity", 80)
    write(f.root / "class" / "power_supply" / "BAT0" / "status", "Not charging")
    for cpu, khz in ((0, 800000), (1, 4000000), (2, 1200000)):
        write(f.root / "devices" / "system" / "cpu" / f"cpu{cpu}" / "cpufreq" / "scaling_cur_freq", khz)
    return f.root


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.result = ss.scan(laptop(self.tmp.name))
        self.by_id = {r["id"]: r for r in self.result["readings"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_known_parts_get_stable_names(self):
        names = {rid: (r["group"], r["name"]) for rid, r in self.by_id.items()}
        self.assertEqual(names["cpu_pkg"], ("CPU", "Package"))
        self.assertEqual(names["cpu_core4"], ("CPU", "Core 4"))
        self.assertEqual(names["dimm_a"], ("Board", "Memory A"), "memory modules ordered by bus address")
        self.assertEqual(self.by_id["dimm_a"]["value"], 54.0)
        self.assertEqual(self.by_id["dimm_b"]["value"], 55.75)
        self.assertEqual(names["ssd1"], ("Board", "SSD 1"), "SSDs numbered by controller")
        self.assertEqual(self.by_id["ssd1"]["value"], 43.85)
        self.assertEqual(names["ssd2_temp2"], ("Board", "SSD 2 · Sensor 1"))
        self.assertEqual(names["lan"], ("Board", "Ethernet"))
        self.assertEqual(names["mid_fan"], ("Fans", "Middle fan"))
        self.assertEqual(self.by_id["mid_fan"]["value"], 4100)
        self.assertEqual(names["sen3"], ("Board", "Board · SEN3"))
        self.assertEqual(names["bat_v"], ("Power", "Battery voltage"))
        self.assertEqual(self.by_id["bat_v"]["value"], 16.122)
        self.assertEqual(self.by_id["bat_w"]["value"], 12.5)
        self.assertEqual(self.by_id["mystery@mystery.0/in0"]["group"], "Other")
        self.assertNotIn("mystery@mystery.0/pwm1", self.by_id, "only *_input files are readings")

    def test_duplicates_twins_and_placeholders(self):
        self.assertIn("lan#2", self.by_id, "a second Ethernet chip keeps its own id")
        self.assertIsNone(self.by_id["lan#2"]["value"], "an unreadable input is None, not an error")
        sources = [r["source"] for r in self.result["readings"]]
        self.assertNotIn("thermal zone · acpitz", sources, "a zone with an hwmon twin is listed once")
        self.assertNotIn("thermal zone · INT3400 Thermal", sources)
        self.assertIn("thermal zone · x86_pkg_temp", sources)

    def test_limits(self):
        self.assertEqual((self.by_id["dimm_a"]["warm"], self.by_id["dimm_a"]["hot"]), (75, 85),
                         "spd5118's 55 C alarm default is not used")
        self.assertEqual((self.by_id["cpu_pkg"]["warm"], self.by_id["cpu_pkg"]["hot"]), (90, 95))
        self.assertEqual((self.by_id["ssd1"]["warm"], self.by_id["ssd1"]["hot"]), (89.85, 93.85))
        self.assertIsNone(self.by_id["ssd2_temp2"]["warm"], "an unset limit (65261 C) is ignored")
        self.assertEqual((self.by_id["sen3"]["warm"], self.by_id["sen3"]["hot"]), (75.05, 80.05),
                         "board limits come from the ACPI hot and critical trip points")
        self.assertEqual(ss.level(dict(self.by_id["sen3"], value=76)), "warm")
        self.assertEqual(ss.level(dict(self.by_id["sen3"], value=80.1)), "hot")
        self.assertIsNone(ss.level(self.by_id["sen3"]))
        self.assertIsNone(ss.level(dict(self.by_id["sen3"], value=None)))

    def test_derived_power_and_clocks(self):
        self.assertTrue(self.result["ac"])
        self.assertEqual(self.result["battery_status"], "Not charging")
        self.assertEqual(self.by_id["bat_pct"]["value"], 80)
        self.assertEqual(self.by_id["cpu_clock_avg"]["value"], 2.0)
        self.assertEqual(self.by_id["cpu_clock_max"]["value"], 4.0)
        hottest = self.by_id["cpu_core_max"]
        self.assertEqual((hottest["value"], hottest["source"]), (71.0, "coretemp · Core 4"))

    def test_missing_tree(self):
        result = ss.scan(Path(self.tmp.name) / "nothing")
        self.assertEqual((result["readings"], result["ac"]), ([], None))


class FormatAndHistoryTest(unittest.TestCase):
    def test_fmt(self):
        self.assertEqual(ss.fmt(None, "°C"), "—")
        self.assertEqual(ss.fmt(4100, "rpm"), "4,100 rpm")
        self.assertEqual(ss.fmt(56.04, "°C"), "56.0 °C")
        self.assertEqual(ss.fmt(63.0, "°C"), "63 °C")
        self.assertEqual(ss.fmt(146.4, "W"), "146 W")
        self.assertEqual(ss.fmt(4.0, "GHz"), "4.00 GHz")
        self.assertEqual(ss.fmt(16.122, "V"), "16.1 V")

    def test_history_window_and_range(self):
        h = ss.SensorHistory(window=300)
        r = ss.reading("sen3", "Board", "Board · SEN3", 60.0, "°C", "zone")
        h.add(0, [r])
        h.add(2, [dict(r, value=None)])
        h.add(200, [dict(r, value=70.0)])
        self.assertEqual(h.series("sen3"), [(0, 60.0), (2, None), (200, 70.0)])
        h.add(400, [dict(r, value=65.0)])
        self.assertEqual(h.series("sen3"), [(200, 70.0), (400, 65.0)], "older than five minutes is dropped")
        self.assertEqual((h.low["sen3"], h.high["sen3"]), (60.0, 70.0), "range since the panel opened")
        self.assertEqual(h.latest("sen3"), 65.0)
        self.assertEqual(h.series("nope"), [])


if __name__ == "__main__":
    unittest.main()
