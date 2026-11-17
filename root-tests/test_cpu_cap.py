"""CPU cap policy tests; all writes/services are isolated in a temporary filesystem."""
import configparser
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cpu_cap_profiles", ROOT / "cpu_cap_profiles.py")
capper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capper)
PPD = """[main]
default=balanced
battery_detection=true
[profiles]
performance=fast
balanced=custom
power-saver=eco
[battery]
balanced=battery
"""
POLICIES = {"fast": {"cpu": {"min_perf_pct": "100"}},
            "custom": {"cpu": {"min_perf_pct": "8", "max_perf_pct": "80"}},
            "eco": {"cpu": {"min_perf_pct": "2", "max_perf_pct": "45"}},
            "battery": {"cpu": {"max_perf_pct": "50"}}}


def config(text):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    return cfg


class PolicyTests(unittest.TestCase):
    def test_all_modes_and_battery_preserve_policies_and_lower_limits(self):
        mapping, files = capper.build(PPD, 60, POLICIES.__getitem__)
        cfg = config(mapping)
        for section, mode, original, minimum, maximum in (
                ("profiles", "performance", "fast", 60, 60),
                ("profiles", "balanced", "custom", 8, 60),
                ("profiles", "power-saver", "eco", 2, 45),
                ("battery", "balanced", "battery", 0, 50)):
            overlay = config(files[cfg[section][mode]])
            self.assertEqual(overlay["main"]["include"], original)
            self.assertEqual(dict(overlay["cpu"]), {"min_perf_pct": str(minimum), "max_perf_pct": str(maximum)})
        self.assertEqual(cfg["main"]["default"], "balanced")

    def test_custom_cpu_instance_and_legacy_cap_change(self):
        mapping, files = capper.build(PPD.replace("fast", "rog-performance"), 90,
            lambda name: {"cpu.custom": {"min_perf_pct": "10", "max_perf_pct": "55"}})
        overlay = config(files[config(mapping)["profiles"]["performance"]])
        self.assertEqual(overlay["cpu.custom"]["max_perf_pct"], "90")
        self.assertEqual(overlay["cpu.custom"]["type"], "cpu")

    def test_reject_unsafe_or_unresolved_values_before_changes(self):
        for value in (True, 0, 39, 101, "70"):
            with self.assertRaises(ValueError):
                capper.build(PPD, value, POLICIES.__getitem__)
        with self.assertRaises(ValueError):
            capper.build(PPD, 60, lambda name: {"cpu": {"max_perf_pct": "${unresolved}"}})
        with self.assertRaises(ValueError):
            capper.build(PPD.replace("fast", "../bad"), 60, POLICIES.__getitem__)


MOCK = r'''#!/usr/bin/python3
import configparser, os, pathlib, sys
r = pathlib.Path(os.environ["CPU_TEST_ROOT"])
def cfg(path):
    c = configparser.ConfigParser(interpolation=None); c.read(path); return c

def apply():
    selected = (r / "etc/tuned/ppd_base_profile").read_text().strip()
    c = cfg(r / "etc/tuned/ppd.conf")
    section = "battery" if os.environ.get("ON_BATTERY") and selected in c["battery"] else "profiles"
    name = c[section][selected]
    p = cfg(r / "etc/tuned/profiles" / name / "tuned.conf")
    maximum = p.get("cpu", "max_perf_pct", fallback="100")
    if (r / "active").exists() and (r / "active").read_text() == name and not os.environ.get("FORCE_RELOAD"):
        return
    (r / "active").write_text(name)
    (r / "sys/devices/system/cpu/intel_pstate/max_perf_pct").write_text(maximum)
name = pathlib.Path(sys.argv[0]).name
if name == "id": print("0")
elif name == "sleep": pass
elif name == "powerprofilesctl":
    if sys.argv[1] == "get": print((r / "etc/tuned/ppd_base_profile").read_text().strip())
    else:
        (r / "etc/tuned/ppd_base_profile").write_text(sys.argv[2] + "\n"); apply()
elif name == "tuned-adm":
    if sys.argv[1] == "active": print("Current active profile: " + (r / "active").read_text())
    else:
        os.environ["FORCE_RELOAD"] = "1"; apply()
elif name == "systemctl":
    with (r / "calls").open("a") as out: out.write(" ".join(sys.argv[1:]) + "\n")
    if sys.argv[1] == "is-enabled": print("disabled"); sys.exit(1)
    if os.environ.get("FAIL_RESTART") and sys.argv[1] == "restart": sys.exit(1)
    if sys.argv[1] == "restart": apply()
'''


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for directory in ("etc/tuned/profiles", "usr/lib/tuned/profiles", "sys/devices/system/cpu/intel_pstate", "bin", "app"):
            (self.root / directory).mkdir(parents=True)
        (self.root / "etc/tuned/ppd.conf").write_text(PPD)
        (self.root / "etc/tuned/ppd_base_profile").write_text("balanced\n")
        (self.root / "sys/devices/system/cpu/intel_pstate/max_perf_pct").write_text("100")
        for name, units in POLICIES.items():
            path = self.root / "etc/tuned/profiles" / name
            path.mkdir()
            c = configparser.ConfigParser()
            c["main"] = {"summary": name}
            for unit, values in units.items():
                c[unit] = values
            with (path / "tuned.conf").open("w") as out:
                c.write(out)
        # Transform paths in a private copy; production scripts have no test-root override.
        for name in ("setup-cool-performance.sh", "cpu_cap_profiles.py"):
            text = (ROOT / name).read_text()
            for prefix in ("/etc/tuned", "/usr/lib/tuned", "/sys/devices"):
                text = text.replace(prefix, str(self.root) + prefix)
            (self.root / "app" / name).write_text(text)
        shim = self.root / "bin/mock"
        shim.write_text(MOCK)
        shim.chmod(0o755)
        for name in ("id", "sleep", "powerprofilesctl", "systemctl", "tuned-adm"):
            (self.root / "bin" / name).symlink_to(shim)
        self.env = dict(os.environ, CPU_TEST_ROOT=str(self.root), PATH=str(self.root / "bin") + ":" + os.environ["PATH"], CAP="60")

    def run_script(self, mode="install", **env):
        return subprocess.run(["bash", str(self.root / "app/setup-cool-performance.sh"), mode],
                              env=dict(self.env, **env), text=True, capture_output=True)

    def power_mode(self, mode, **env):
        subprocess.run([str(self.root / "bin/powerprofilesctl"), "set", mode], env=dict(self.env, **env), check=True)
        return int((self.root / "sys/devices/system/cpu/intel_pstate/max_perf_pct").read_text())

    def test_install_reboot_modes_reapply_and_undo(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/tuned/ppd_base_profile").read_text().strip(), "balanced")
        for mode, expected in (("performance", 60), ("balanced", 60), ("power-saver", 45)):
            self.assertEqual(self.power_mode(mode), expected)
            # A boot/resume-style PPD restart reapplies the saved mode's overlay.
            (self.root / "sys/devices/system/cpu/intel_pstate/max_perf_pct").write_text("100")
            (self.root / "active").unlink()
            subprocess.run([str(self.root / "bin/systemctl"), "restart", "tuned-ppd"], env=self.env, check=True)
            self.assertEqual(self.power_mode(mode), expected)
        self.assertEqual(self.power_mode("balanced", ON_BATTERY="1"), 50)
        self.assertEqual(self.power_mode("performance"), 60)
        result = self.run_script(CAP="75")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.power_mode("performance"), 75)
        self.assertEqual((self.root / "etc/tuned/ppd.conf.before-rog-cpu-cap").read_text(), PPD)
        result = self.run_script("undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/tuned/ppd.conf").read_text(), PPD)
        self.assertFalse(list((self.root / "etc/tuned/profiles").glob("rog-cpu-cap-*")))
        self.assertEqual(self.run_script("undo").returncode, 0)

    def test_upgrade_undo_legacy_install_before_first_new_apply(self):
        original = self.root / "etc/tuned/ppd.conf.before-rog-performance"
        original.write_text(PPD)
        current = PPD.replace("performance=fast", "performance=rog-performance")
        (self.root / "etc/tuned/ppd.conf").write_text(current)
        legacy = self.root / "etc/tuned/profiles/rog-performance"
        legacy.mkdir()
        (legacy / "tuned.conf").write_text("[main]\ninclude=fast\n[cpu]\nmax_perf_pct=70\n")
        (self.root / "etc/tuned/rog-performance.disabled-old-cap-unit").touch()
        self.power_mode("performance")
        result = self.run_script("undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/tuned/ppd.conf").read_text(), PPD)
        self.assertFalse(legacy.exists())
        self.assertFalse(original.exists())
        self.assertIn("enable intel-pstate-70.service", (self.root / "calls").read_text())

    def test_all_mode_undo_retains_legacy_policy_and_original_backup(self):
        previous = PPD.replace("performance=fast", "performance=rog-performance")
        (self.root / "etc/tuned/ppd.conf").write_text(previous)
        original = self.root / "etc/tuned/ppd.conf.before-rog-performance"
        original.write_text(PPD)
        legacy = self.root / "etc/tuned/profiles/rog-performance"
        legacy.mkdir()
        (legacy / "tuned.conf").write_text("[main]\ninclude=fast\n[cpu]\nmax_perf_pct=55\n")
        self.assertEqual(self.run_script(CAP="75").returncode, 0)
        self.assertEqual(self.power_mode("performance"), 75)
        result = self.run_script("undo")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "etc/tuned/ppd.conf").read_text(), previous)
        self.assertEqual(original.read_text(), PPD)
        self.assertTrue(legacy.exists())
        self.assertEqual(self.power_mode("performance"), 55)

    def test_failures_report_nonzero_and_invalid_cap_does_not_write(self):
        result = self.run_script(CAP="50;echo unsafe")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "calls").exists())
        self.assertEqual((self.root / "etc/tuned/ppd.conf").read_text(), PPD)
        result = self.run_script(FAIL_RESTART="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("Saved CPU ceiling", result.stdout)
        self.assertTrue((self.root / "etc/tuned/ppd.conf.before-rog-cpu-cap").exists())


if __name__ == "__main__":
    unittest.main()
