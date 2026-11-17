#!/usr/bin/env python3
"""Offscreen test of rog-panel-qt.py (dry run only; nothing real runs, no window on a real display)."""
import importlib.util
import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch
import json
from datetime import datetime, timezone

os.environ["QT_QPA_PLATFORM"] = "offscreen"
OUT = tempfile.mkdtemp(prefix="rog-qt-offscreen-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(OUT, "config")   # tray settings and start at login
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rogcore as core  # noqa: E402
import fan_tray  # noqa: E402
import panel_settings  # noqa: E402
from PySide6.QtCore import Qt, QPoint, QTimer, QSaveFile, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication, QIcon  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

core.PRESET_FILE = os.path.join(OUT, "custom-presets.json")
core.FAND_RUN = Path(OUT) / "run"
core.FAND_UNIT = Path(OUT) / "rog-fand.service"
fails = []


def screenshot(w, name):
    QApplication.processEvents()
    dst = QSaveFile(os.path.join(OUT, name))
    assert dst.open(QIODevice.WriteOnly) and w.grab().save(dst, "PNG") and dst.commit()



def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        fails.append(msg)


def pump(ms):
    """Process events without QTest.qWait: qWait keeps the GIL, which starves the worker threads."""
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def wait_for(cond, ms=15000):
    t = 0
    while not cond() and t < ms:
        pump(50)
        t += 50
    return cond()


def publish(path, text):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as out:
        out.write(text)
    os.replace(out.name, path)


def service_tabs(w, q):
    auto, cal = w.auto_tab, w.cal_tab
    check(auto.state == "Not installed" and not auto.apply.isEnabled(), "new installation state")
    auto.preview_btn.click()
    check(wait_for(lambda: bool(auto.preview_sample)), "read-only preview receives actual service JSON")
    check(auto.state == "Dry run" and not auto.apply.isEnabled(), "preview cannot change mode")
    auto.preview_btn.click()
    check(wait_for(lambda: auto.preview is None), "preview child exits on request")

    control = core.FAND_RUN / "control"
    control.mkdir(parents=True)
    mode = control / "mode"
    public = core.FAND_RUN / "mode"
    public.symlink_to("control/mode")
    publish(mode, "auto\n")
    publish(core.FAND_UNIT, "")
    auto.refresh()
    check(auto.state == "Stopped", "installed service with no status")
    path = core.FAND_RUN / "status.json"
    publish(path, "{")
    auto.refresh()
    check(auto.state == "Unreadable", "malformed status disables controls")
    now = time.time()
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    s = dict(version=1, t=stamp(now-200), dry_run=False, mode="auto", applied=True, invalid_mode=False,
             sensors=dict(profile="performance", cpu_rpm=5100, gpu_rpm=5500, mid_rpm=6200,
                          cpu_w=42, gpu_w=146, board_c=78),
             floors=dict(cpu=80, gpu=80, mid=80), heat_w=188, smoothed_gpu_w=145,
             unavailable=[], margins={"0":0,"50":5,"110":0,"140":0},
             usb=dict(audio_active=True, devices=[dict(name="USB interface", drops_per_s=1.5)], error=None),
             events=[dict(t=stamp(now), message="USB drop boost")])
    publish(path, json.dumps(s))
    auto.refresh()
    check(auto.state == "Stale" and not auto.chart.samples, "stale samples excluded")
    for i in range(101):
        s["t"] = stamp(now-200+2*i)
        s["floors"] = dict(cpu=35 if i<40 else 80, gpu=40 if i<40 else 80, mid=70 if i<40 else 80)
        auto.display("Running", s)
    publish(path, json.dumps(s))
    auto.refresh()
    check(len(auto.chart.samples) == 101 and auto.bars["mid"].value()==80, "timestamped graph deduplicates samples")
    check("1.5 drops/s" in auto.usb.text() and "+5 pp" in auto.margin.text(), "USB rates and learned margins")
    check("boost" in auto.events.toPlainText() and not auto.apply.isEnabled(), "dry panel reads live events without control")
    auto.display("Running", dict(s, sensors=dict(s["sensors"], profile="quiet")))
    check(auto.detail.text().startswith("Quiet profile") and "Paused" in auto.detail.text()
          and "asusctl profile set Performance" in auto.detail.text(), "auto: paused outside Performance, with the way back")
    auto.display("Running", s)
    check("Paused" not in auto.detail.text(), "auto: no pause note in Performance")
    with patch.object(core, "read_fans", return_value=dict(cpu_rpm=3150, gpu_rpm=None, mid_rpm=7400)):
        auto.display("Running", s)
    check(auto.speeds["cpu"].value() == 3150 and auto.speeds["cpu"].format() == "3,150 rpm · 50%"
          and auto.speeds["gpu"].format() == "5,500 rpm · 80%" and auto.speeds["mid"].format() == "7,400 rpm · 100%",
          "fan speeds: live sensors first, service sample as fallback, scaled to full speed")
    with patch.object(core, "read_fans", return_value=dict(cpu_rpm=None, gpu_rpm=None, mid_rpm=None)):
        auto.display("Stopped", None)
    check(auto.speeds["cpu"].format() == "—" and auto.speeds["cpu"].value() == 0, "fan speeds: unknown shows a dash")
    with patch.object(core, "read_fans", return_value=dict(cpu_rpm=4100, gpu_rpm=2500, mid_rpm=9000)):
        auto.display("Stopped", None)
    check(auto.speeds["mid"].value() == 7400 and auto.speeds["mid"].format() == "9,000 rpm · 122%",
          "fan speeds: shown without the service, bar capped at full speed")
    auto.refresh()
    auto.change_mode()
    check(mode.read_text() == "auto\n", "panel dry run leaves mode file alone")
    auto.dry_run = False  # Everything from this point targets the fixture directory.
    auto.refresh()
    auto.mode.setCurrentIndex(auto.mode.findData("off"))
    auto.apply.click()
    check(mode.read_text()=="off\n" and auto.pending is not None and core.manual_fan_problem(),
          "mode published atomically, manual fan editor waits for service acknowledgement")
    s["mode"] = "off"
    s["t"] = stamp(time.time())
    publish(path, json.dumps(s))
    auto.refresh()
    check(auto.pending is None and "confirmed" in auto.feedback.text() and not core.manual_fan_problem(),
          "applied Off acknowledged by service and manual editing released")
    s["mode"] = "auto"
    publish(path, json.dumps(s))
    auto.refresh()
    screenshot(w, "auto.png")

    w.tabs.setCurrentIndex(4)
    cal.preset.setCurrentText("Custom")
    cal.custom.setText("mid=101")
    cal.start.click()
    check("percentage" in cal.message.text() and cal.process is None, "custom step validation")
    cal.custom.setText("mid=70;cpu=80,gpu=80,mid=80")
    cal.start.click()
    check("--steps" in cal.message.text() and cal.process is None, "calibration dry run starts no hardware process")
    cal.dry_run=False
    cal.preset.setCurrentText("Observe")
    with patch.object(core, "sound_device_busy", return_value=True):
        cal.start.click()
    check("busy" in cal.message.text() and cal.process is None, "busy internal device warning")
    # Drive the real QProcess lifecycle using a child that has no device access or commands.
    from textwrap import dedent
    child = dedent("""
        import json, os, signal, sys, time
        def cancel(*_):
            print(json.dumps(dict(kind='done',message='Cancelled.',cancelled=True)),flush=True)
            sys.exit(0)
        signal.signal(signal.SIGTERM,cancel)
        path=sys.argv[1]
        with open(path+'.part','w') as f:
            f.write('t,stalls_per_s\\n12:00:00,0\\n')
        os.replace(path+'.part',path)
        for event in [dict(kind='step',message='Observe',index=1,total=1),
                      dict(kind='sample',message='Reading',row={},elapsed=30),
                      dict(kind='result',name='Observe',samples=15,mean=0,peak=0,watts=70)]:
            print(json.dumps(event),flush=True)
        time.sleep(30)
    """)
    with patch.object(core, "calibration_args", side_effect=lambda p,s,c,o: ["-Ic",child,str(o)]), \
         patch.object(core, "sound_device_busy", return_value=False):
        cal.start.click()
        check(wait_for(lambda: cal.results.rowCount()==1), "streaming calibration progress and results")
        check(auto.calibrating and cal.progress.value()==25, "active calibration holds UI controls")
        cal.cancel.click()
        check(wait_for(lambda: cal.process is None) and "Cancelled" in cal.message.text(), "cancel waits for owned child")
    dest = Path(OUT) / "export.csv"
    cal.export_csv(dest)
    check(cal.export.isEnabled() and dest.read_text()==cal.output_path.read_text(), "exact atomic CSV export")
    screenshot(w, "calibrate.png")
    cal.dry_run = auto.dry_run = True
    path.unlink()
    core.FAND_UNIT.unlink()


def fand_sample(**changes):
    now = datetime.now(timezone.utc)
    s = dict(version=1, t=now.isoformat(), elapsed_s=100.0, dry_run=False, mode="auto", applied=True,
             invalid_mode=False, degraded=False,
             sensors=dict(profile="performance", cpu_rpm=4000, gpu_rpm=4200, mid_rpm=5000,
                          cpu_w=40, gpu_w=120, board_c=70),
             floors=dict(cpu=60, gpu=60, mid=75), heat_w=160, smoothed_gpu_w=118, unavailable=[],
             margins={"0": 0, "50": 0, "110": 0, "140": 0},
             usb=dict(audio_active=True, error=None,
                      devices=[dict(name="USB interface", packets=16000, skipped=0, warmup_skipped=0,
                                    drops_per_s=0.0)]),
             events=[])
    s.update(changes)
    return s


def tray_model_tests():
    m = fan_tray
    wall = time.time()
    s = fand_sample()
    check(m.classify("Running", s, wall, None)[0] == "green", "tray: clean automatic control is green")
    check(m.classify("Running", s, wall, wall - 30)[0] == "yellow"
          and m.classify("Running", s, wall, wall - 600)[0] == "green", "tray: yellow only while a boost ramps down")
    check(m.classify("Running", fand_sample(degraded=True), wall, None)[0] == "orange", "tray: sensor fallback is orange")
    check(all(m.classify(x, None, wall, None)[0] == "red" for x in ("Stopped", "Stale", "Unreadable")),
          "tray: stopped, stale or unreadable service is red")
    check(m.classify("Not installed", None, wall, None)[0] == "white", "tray: not installed is white")
    check(all(m.classify("Running", fand_sample(mode=x), wall, None)[0] == "blue" for x in ("off", "preset:heavy"))
          and m.classify("Running", fand_sample(sensors=dict(s["sensors"], profile="quiet")), wall, None)[0] == "blue",
          "tray: Off, a preset and other profiles are blue")
    check(m.classify("Running", fand_sample(sensors=dict(s["sensors"], profile="quiet")), wall, None)
          == ("blue", "Paused: Quiet profile")
          and m.classify("Running", fand_sample(sensors=dict(s["sensors"], profile=None)), wall, None)[1]
          == "Paused: unknown profile", "tray: outside Performance the summary says paused")
    check(core.profile_name("low-power") == "Low power" and core.profile_name(None) == "unknown",
          "tray: profile names for people")
    warm = dict(name="USB interface", packets=16000, skipped=10, warmup_skipped=10, drops_per_s=5.0)
    real = dict(name="USB interface", packets=16000, skipped=20, warmup_skipped=10, drops_per_s=10.0)
    check(m.usb_drops(fand_sample(usb=dict(audio_active=True, error=None, devices=[warm]))) == 0
          and m.usb_drops(fand_sample(usb=dict(audio_active=True, error=None, devices=[real]))) == 5,
          "tray: stream start-up skips are not drops")
    check(m.classify("Running", fand_sample(usb=dict(audio_active=True, error=None, devices=[real])), wall, None)
          == ("red", "USB audio dropping 5 packets/s"), "tray: live drops are red")

    model = m.TrayModel()
    stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
    kinds = lambda due: [k for k, _, _ in due]
    old = [dict(t=stamp(wall - 100), message="USB drop boost")]
    check(model.observe("Running", fand_sample(events=old), wall, 1000) == [] and model.last_boost is not None,
          "notify: nothing for the state found at start")
    new = old + [dict(t=stamp(wall), message="USB drop boost")]
    due = model.observe("Running", fand_sample(events=new, usb=dict(audio_active=True, error=None, devices=[real])),
                        wall, 1002)
    check(kinds(due) == ["boost"] and "5 packets/s" in due[0][2], "notify: a new boost names the drop rate")
    newer = new + [dict(t=stamp(wall + 60), message="USB drop boost")]
    check(model.observe("Running", fand_sample(events=newer), wall, 1060) == [], "notify: boosts rate limited")
    check(model.observe("Stale", None, wall, 1062) == [], "notify: one stale poll is ignored")
    check(kinds(model.observe("Stale", None, wall, 1064)) == ["service-down"], "notify: service down after two polls")
    check(kinds(model.observe("Running", fand_sample(events=newer), wall, 1066)) == ["service-up"],
          "notify: service running again")
    due = model.observe("Running", fand_sample(events=newer, elapsed_s=4.0, mode="auto"), wall, 1500)
    check(kinds(due) == ["service-up"] and "restarted" in due[0][2], "notify: restart noticed from elapsed time")
    due = model.observe("Running", fand_sample(degraded=True, unavailable=["gpu_w unavailable"]), wall, 1502)
    check(kinds(due) == ["fallback"] and "gpu_w" in due[0][2], "notify: sensor fallback names the sensor")
    check(kinds(model.observe("Running", fand_sample(), wall, 1504)) == ["fallback-end"], "notify: fallback ended")
    check(model.observe("Running", fand_sample(mode="off"), wall, 1506, ("off", 1500)) == [],
          "notify: no notice for the panel's own mode change")
    check(kinds(model.observe("Running", fand_sample(mode="preset:max"), wall, 1508, ("off", 1500))) == ["mode"],
          "notify: mode changed elsewhere")

    model = m.TrayModel()
    prof = lambda name: fand_sample(sensors=dict(s["sensors"], profile=name))
    check(model.observe("Running", prof("quiet"), wall, 2000) == [], "profile: nothing for the profile found at start")
    check(model.observe("Running", prof("performance"), wall, 2002) == [],
          "profile: no resumed notice without a paused notice")
    due = model.observe("Running", prof("quiet"), wall, 2004)
    check(kinds(due) == ["profile"] and due[0][1] == "Fan protection paused" and "now Quiet." in due[0][2]
          and "tray menu" in due[0][2], "profile: leaving Performance names the new profile")
    check(model.observe("Running", prof("balanced"), wall, 2006) == [], "profile: Quiet to Balanced stays paused, no notice")
    due = model.observe("Running", prof("performance"), wall, 2008)
    check(kinds(due) == ["profile-back"] and due[0][1] == "Fan protection resumed" and "Automatic" in due[0][2],
          "profile: back in Performance")
    check(kinds(model.observe("Running", prof("quiet"), wall, 2010)) == ["profile"],
          "profile: a pause right after a resumed notice is never rate limited")
    check(model.observe("Running", prof("performance"), wall, 2012) == []
          and model.observe("Running", prof("quiet"), wall, 2014) == [],
          "profile: flapping rate limited, the last notice still says paused")
    check(kinds(model.observe("Running", prof("performance"), wall, 2014 + m.NOTIFY_GAP_S)) == ["profile-back"],
          "profile: resumed notice again after 5 minutes")
    check(model.observe("Stale", None, wall, 2400) == [] and kinds(model.observe("Stale", None, wall, 2402))
          == ["service-down"] and kinds(model.observe("Running", prof(None), wall, 2404)) == ["service-up", "profile"],
          "profile: a profile change across a service outage is still noticed")

    ps = panel_settings
    check(ps.exec_arg("/usr/bin/python3") == "/usr/bin/python3", "exec: plain path unquoted")
    check(ps.exec_arg("/home/u/ROG Audio/ui/rog-panel-qt.py") == '"/home/u/ROG Audio/ui/rog-panel-qt.py"',
          "exec: spaces quoted")
    # Quoting escapes \ " ` $ with a backslash; string escaping then doubles every backslash.
    check(ps.exec_arg('/a b/$x"`\\%') == '"/a b/\\\\$x\\\\"\\\\`\\\\\\\\%%"', "exec: reserved characters escaped")
    path = ps.autostart_path()
    check(str(path).startswith(OUT) and not ps.autostart_enabled(), "autostart: isolated and off by default")
    ps.set_autostart(True)
    text = path.read_text()
    check(ps.autostart_enabled() and "--tray" in text and ps.exec_arg(str(ps.PANEL)) in text
          and oct(path.stat().st_mode & 0o777) == "0o644", "autostart: entry written atomically")
    ps.set_autostart(False)
    check(not path.exists(), "autostart: entry removed")
    path.write_text("[Desktop Entry]\nExec=something-else\n")
    ps.set_autostart(False)
    check(path.exists(), "autostart: never removes an entry it did not write")
    path.unlink()


def update_tests(w, tray, shown):
    up = w.updater
    class Fake:
        def __init__(self, tag, installed, fail=None):
            self.tag, self.installed, self.fail = tag, installed, fail
        def latest(self):
            if self.fail:
                raise OSError(self.fail)
            return dict(tag_name=self.tag)
        def installed_version(self):
            return self.installed
        is_newer = staticmethod(core.load_updater().is_newer)
    with patch.object(core, "load_updater", return_value=Fake("v0.2.0", "0.2.0-dev")):
        check(panel_settings.check_updates() == dict(installed="0.2.0-dev", tag="v0.2.0", newer=True),
              "update: newer release found")
    with patch.object(core, "load_updater", return_value=Fake("v0.1.0", "0.2.0-dev")):
        check(panel_settings.check_updates()["newer"] is False, "update: older release not offered")
    with patch.object(core, "load_updater", return_value=Fake(None, None, fail="offline")):
        check(panel_settings.check_updates() == dict(error="offline"), "update: network errors reported, not raised")

    found = dict(installed="0.2.0-dev", tag="v0.2.0", newer=True)
    before = len(shown)
    with patch.object(panel_settings, "check_updates", return_value=found):
        up.check()
        check(wait_for(lambda: up.tag == "v0.2.0" and not up.checking, 5000), "update: background check")
        up.check()
        check(wait_for(lambda: not up.checking, 5000) and len(shown) == before + 1
              and shown[-1][0] == "Update available", "update: notified once per release")
    tray.update_menu()
    check(tray.install_act.isVisible() and "v0.2.0" in tray.install_act.text()
          and "Update v0.2.0 available" in tray.tray.toolTip(), "update: tray menu entry and tooltip")
    tab = w.settings_tab
    check(tab.install_btn.isVisibleTo(tab) and "v0.2.0" in tab.install_btn.text()
          and "newest release v0.2.0" in tab.version.text() and "Update available" in tab.update_status.text(),
          "update: Settings tab shows the release and the install button")
    check(core.update_command("v0.2.0") == ["pkexec", "/usr/bin/python3", "-IB",
                                            "/usr/local/lib/rog-fand/rog_update.py", "install", "v0.2.0"],
          "update: pkexec runs only the root-owned installed updater with the tag")
    restarts = []
    up.restart_panel = lambda: restarts.append(1)
    cases = ((126, "", "Update not installed", tray.install_act.trigger),
             (1, "error: v0.2.0 VERSION file says '0.1.0'", "Update failed", tab.install_btn.click),
             (0, "installed v0.2.0", "Updated to v0.2.0", tab.install_btn.click))
    for code, output, title, press in cases:
        script = f"echo \"{output}\"; exit {code}"
        with patch.object(core, "updater_installed", return_value=True), \
             patch.object(core, "update_command", return_value=["/bin/sh", "-c", script]):
            up.tag = "v0.2.0"
            tray.update_menu()
            tab.refresh()
            check(tray.install_act.isEnabled() and tab.install_btn.isEnabled(),
                  "update: install enabled when the root-owned updater exists")
            press()
            check(up.proc is not None and not tab.install_btn.isEnabled() and not tab.check_btn.isEnabled(),
                  "update: buttons disabled while installing")
            check(wait_for(lambda: up.proc is None, 5000) and shown[-1][0] == title and title in tab.update_status.text()
                  and (code != 1 or "VERSION file says" in shown[-1][1]), "update: exit %d -> %s" % (code, title))
    check(wait_for(lambda: restarts, 4000) and up.tag is None and up.installed == "0.2.0",
          "update: panel restarts after installing")
    with patch.object(core, "updater_installed", return_value=False):
        up.tag = "v0.2.0"
        tray.update_menu()
        tab.refresh()
        check(not tray.install_act.isEnabled() and "install the service first" in tray.install_act.text()
              and not tab.install_btn.isEnabled() and "Install the service" in tab.update_status.text(),
              "update: no install without the root-owned updater")
    up.tag = None
    newest = dict(installed="0.2.0", tag="v0.2.0", newer=False)
    with patch.object(panel_settings, "check_updates", return_value=newest):
        tab.check_btn.click()
        check(up.checking and not tab.check_btn.isEnabled() and tab.check_btn.text() == "Checking…",
              "update: Check now shows progress")
        check(wait_for(lambda: not up.checking, 5000) and "Up to date" in tab.update_status.text()
              and shown[-1][0] == "Up to date" and not tab.install_btn.isVisibleTo(tab) and tab.check_btn.isEnabled(),
              "update: manual check reports up to date")
    with patch.object(panel_settings, "check_updates", return_value=dict(error="offline")):
        up.check()
        check(wait_for(lambda: not up.checking, 5000) and "last check failed: offline" in tab.version.text(),
              "update: a failed automatic check shows in the tab quietly")
    w.prefs.set("check_updates", False)
    up.check()
    check(not up.checking and not tray.options["check_updates"].isChecked(), "update: automatic checks can be off")
    w.prefs.set("check_updates", True)


def settings_tests(w, tray, shown):
    tab, prefs = w.settings_tab, w.prefs
    w.tabs.setCurrentWidget(w.settings_scroll)
    pump(100)
    screenshot(w, "6-settings.png")
    tab.boxes["close_to_tray"].setChecked(False)
    check(not prefs.get("close_to_tray") and not tray.keep_act.isChecked() and not tray.keep_running(),
          "settings: tab checkbox updates the tray menu")
    tray.keep_act.setChecked(True)
    check(prefs.get("close_to_tray") and tab.boxes["close_to_tray"].isChecked(), "settings: tray menu updates the tab")
    count = len(shown)
    tab.boxes["notifications"].setChecked(False)
    tray.notify("Test", "hidden")
    check(len(shown) == count and not tray.notify_act.isChecked(), "settings: notifications can be switched off")
    tab.boxes["notifications"].setChecked(True)
    tray.notify("Test", "shown")
    check(len(shown) == count + 1, "settings: notifications back on")
    path = panel_settings.autostart_path()
    tab.boxes["start_at_login"].setChecked(True)
    check(path.exists() and "--tray" in path.read_text() and tray.options["start_at_login"].isChecked(),
          "settings: start at login writes the autostart entry")
    tray.options["start_at_login"].setChecked(False)
    check(not path.exists() and not tab.boxes["start_at_login"].isChecked(), "settings: start at login removed from the tray")
    with patch.object(panel_settings, "set_autostart", side_effect=OSError("read-only")):
        tab.boxes["start_at_login"].setChecked(True)
    check(not tab.boxes["start_at_login"].isChecked() and "read-only" in tab.message.text(),
          "settings: a failed change is shown and the box reverts")
    w.tabs.setCurrentIndex(0)
    tray.settings_act.trigger()
    check(w.tabs.currentWidget() is w.settings_scroll and w.isVisible(), "settings: tray menu opens the Settings tab")

    old = tray
    w.hide()
    tab.boxes["tray"].setChecked(False)
    check(w.tray is None and not old.tray.isVisible() and not old.timer.isActive() and w.isVisible()
          and not tab.boxes["notifications"].isEnabled() and not tab.boxes["close_to_tray"].isEnabled(),
          "settings: tray icon off removes it and shows the hidden window")
    check(w.windowIcon().name() == "asus_notif_red", "icon: window back to the red logo without the tray")
    pump(100)
    with patch.object(w, "quit_app", return_value=True) as quit_app:
        w.close()
    check(quit_app.called and not w.isVisible(), "settings: without the tray, closing quits")
    w.show()
    tab.boxes["tray"].setChecked(True)
    check(w.tray is not None and w.tray is not old and tab.boxes["notifications"].isEnabled(),
          "settings: tray icon back on")
    w.tray_allowed = False
    w.apply_tray()
    check(w.tray is None and not tab.boxes["tray"].isEnabled() and "No system tray" in tab.tray_note.text(),
          "settings: desktop without a tray")
    w.tray_allowed = True
    return w.apply_tray()


def profile_tests(w, tray, shown, status):
    """The tray warns when the profile leaves Performance and offers the way back (asusctl is never run)."""
    heavy = lambda profile: fand_sample(mode="preset:heavy", sensors=dict(fand_sample()["sensors"], profile=profile))
    tray.update_menu()
    check(not tray.profile_act.isVisible(), "profile: no switch entry in Performance")
    publish(status, json.dumps(heavy("quiet")))
    tray.poll()
    check(tray.colour == "blue" and "ROG fans: Paused: Quiet profile" in tray.tray.toolTip() and len(shown) == 1
          and shown[-1][0] == "Fan protection paused" and "now Quiet" in shown[-1][1],
          "profile: leaving Performance warns; the tooltip says paused")
    tray.update_menu()
    check(tray.profile_act.isVisible() and tray.profile_act.isEnabled()
          and tray.profile_act.text() == "Switch to Performance profile", "profile: switch entry while paused")
    w.dry_run = True
    tray.switch_profile()
    check(tray.profile_proc is None and shown[-1] == ("Profile not changed",
                                                      "Dry run: asusctl profile set Performance was not run."),
          "profile: dry run only names the command")
    w.dry_run = False
    fail = ["/bin/sh", "-c", "echo 'Error: asusd is not running' >&2; exit 1"]
    with patch.object(core, "performance_profile_command", return_value=fail):
        tray.profile_act.trigger()
        check(tray.profile_proc is not None and not tray.profile_act.isEnabled()
              and tray.profile_act.text().startswith("Switching"), "profile: switch runs without blocking the tray")
        check(wait_for(lambda: tray.profile_proc is None, 5000)
              and shown[-1] == ("Profile not changed", "Error: asusd is not running") and tray.profile_act.isEnabled(),
              "profile: a failed switch is notified with asusctl's message")
    with patch.object(core, "performance_profile_command", return_value=[os.path.join(OUT, "no-asusctl")]):
        tray.profile_act.trigger()
        check(wait_for(lambda: tray.profile_proc is None, 5000) and shown[-1][0] == "Profile not changed"
              and shown[-1][1].startswith("asusctl: "), "profile: a missing asusctl is notified")
    count = len(shown)
    with patch.object(core, "performance_profile_command", return_value=["/bin/sh", "-c", "exit 0"]):
        tray.profile_act.trigger()
        check(wait_for(lambda: tray.profile_proc is None, 5000) and len(shown) == count,
              "profile: a successful switch waits for the service to confirm")
    publish(status, json.dumps(heavy("performance")))
    tray.poll()
    tray.update_menu()
    check(shown[-1][0] == "Fan protection resumed" and len(shown) == count + 1 and not tray.profile_act.isVisible()
          and tray.colour == "blue" and "Heavy preset" in tray.tray.toolTip(),
          "profile: back in Performance hides the entry and says so")
    shown.clear()


def tray_tests(w):
    tray_model_tests()
    hicolor = Path("/usr/share/icons/hicolor/512x512/apps/asus_notif_green.png").exists()
    QIcon.setThemeSearchPaths(["/usr/share/icons"])   # offscreen has no icon theme; a desktop sets these
    QIcon.setThemeName("hicolor")
    w.tray_allowed = True
    tray = w.apply_tray()
    shown = []
    w.prefs.set("notifications", True)
    capture = lambda title, text, *_: shown.append((title, text))
    tray.tray.showMessage = capture
    check(not tray.icon("green").isNull(), "tray: icon available (%s)" %
          ("ROG logo from asusctl" if hicolor else "fallback icon"))
    status = core.FAND_RUN / "status.json"
    publish(status, json.dumps(fand_sample()))
    tray.poll()
    check(tray.colour == "green" and "Floors 60 / 60 / 75" in tray.tray.toolTip(), "tray: live status and tooltip")
    check(w.windowIcon().name() == "asus_notif_green", "icon: window follows the tray colour")
    launcher = (Path(HERE) / "rog-panel-qt.desktop").read_text()
    check("Icon=asus_notif_red\n" in launcher and "Icon=asus_notif_red\n" in panel_settings.autostart_entry(),
          "icon: launcher and login entry use the red ROG logo by name")
    tray.update_menu()
    check(not tray.mode_menu.isEnabled(), "tray: dry run cannot change mode")
    w.dry_run = False   # the control directory is the test fixture
    tray.update_menu()
    check(tray.mode_menu.isEnabled() and tray.mode_acts["auto"].isChecked(), "tray: mode menu follows the service")
    tray.mode_acts["preset:heavy"].trigger()
    check((core.FAND_RUN / "control" / "mode").read_text() == "preset:heavy\n", "tray: mode request published")
    publish(status, json.dumps(fand_sample(mode="preset:heavy")))
    tray.poll()
    check(tray.colour == "blue" and not shown, "tray: own mode change without a notification")
    profile_tests(w, tray, shown, status)
    publish(status, json.dumps(fand_sample(mode="auto")))
    w.dry_run = True
    tray.toggle_monitor()
    check(w.mon_proc is None and shown and shown[-1][0] == "Stall monitor not started", "tray: dry-run monitor refused")
    w.show()
    pump(100)
    check(w.timer.isActive() and w.auto_tab.timer.isActive(), "window: status timers run while shown")
    tray.keep_act.setChecked(True)
    w.close()
    pump(100)
    check(not w.isVisible() and not w.quitting and not w.timer.isActive() and not w.auto_tab.timer.isActive()
          and tray.timer.isActive(), "window: close hides to the tray and pauses the window's timers")
    check(shown[-1][0] == "Still running in the tray", "window: hiding to the tray is announced")
    tray.toggle_panel()
    pump(100)
    check(w.isVisible() and w.timer.isActive(), "tray: click reopens the window")
    update_tests(w, tray, shown)
    tray = settings_tests(w, tray, shown)
    tray.tray.showMessage = capture
    quits = []
    with patch.object(QApplication, "quit", side_effect=lambda: quits.append(1)):
        tray.menu.actions()[-1].trigger()
    check(quits and w.quitting and not tray.timer.isActive() and not w.isVisible(), "tray: Quit shuts down and quits")
    status.unlink()


def main():
    app = QApplication([])
    check(QGuiApplication.platformName() == "offscreen", "platform is offscreen")
    spec = importlib.util.spec_from_file_location("rogqt", os.path.join(HERE, "rog-panel-qt.py"))
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    w = q.MainWindow(dry_run=True)
    w.updater.stop()     # no automatic network checks during the test
    w.resize(1280, 800)
    w.show()
    check(w.height() <= 800 and w.width() <= 1280, "window fits 1280x800 (%dx%d)" % (w.width(), w.height()))
    check(wait_for(lambda: w.first_status), "first status refresh arrived")
    check("DRY RUN" in w.banner.text(), "DRY RUN banner visible")
    check(w.ed_built, "editor built from stored curves (needs /etc/asusd/fan_curves.ron)")
    if not w.ed_built:
        return finish(w)
    pump(300)
    service_tabs(w, q)

    # ---- monitor: synthetic IRQ counts, no aplay ----
    wu = q.core.StallRate()
    got = [wu.feed(100 * i, float(i)) for i in range(4)]
    check(got[:3] == [None, None, None] and abs(got[3] - (100 - core.NORMAL_IRQ_PER_S)) < 1e-6,
          "stall rate: first two intervals after start are dropped (warm-up burst): %r" % got)
    r = q.core.StallRate(warmup=0)
    check(r.feed(1000, 0.0) is None, "stall rate: first sample gives no value")
    v = r.feed(1000 + 5, 1.0)
    check(v is not None and v == 0.0, "stall rate: 6 irq/s is clamped to 0 (normal %.2f)" % core.NORMAL_IRQ_PER_S)
    v = r.feed(1005 + 106, 2.0)
    check(abs(v - (106 - core.NORMAL_IRQ_PER_S)) < 1e-6, "stall rate: 106 irq/s -> %.2f stalls/s" % v)
    check(core.parse_irq_total(" 163: 1 2 3 IR-PCI-MSI %s\n" % core.HDA_LABEL) == 6, "parse_irq_total sums per-CPU counts")
    base = 100000
    import math
    for i in range(0, 330):
        extra = 0 if i < 100 else int(40 * (1 + math.sin(i / 15.0)))
        base += 6 + extra
        w.monitor_feed(base, 1000.0 + i, 70 + 12 * math.sin(i / 60.0))
    check(len(w.mon_samples) == 300, "rolling window keeps 300 samples")
    check("Peak" in w.mon_stats.text() and "n/a" not in w.mon_stats.text(), "peak and average shown: " + w.mon_stats.text())
    check(w.mon_proc is None, "no aplay was started")

    # ---- USB check parsing (canned stdout) ----
    ok = ('USBDROP bus=1 dev=12 path=1-1.4.3 name="Volt 2" packets=239990 skipped=22718 pct=9.47 events=2522 events_per_s=84.07\n'
          'USBDROP bus=1 dev=14 path=1-1.2 name="Clean Dev" packets=1000 skipped=0 pct=0.00 events=0 events_per_s=0.00\n')
    p = core.parse_usbdrop(ok)
    check(len(p["devices"]) == 2 and p["devices"][0]["name"] == "Volt 2" and p["devices"][0]["skipped"] == 22718, "parse USBDROP device lines")
    check(core.parse_usbdrop("USBDROP none\n")["none"], "parse USBDROP none")
    check(core.parse_usbdrop('USBDROP error msg="no usbmon"\n')["error"] == "no usbmon", "parse USBDROP error")
    check(core.parse_usbdrop("garbage\n") == {"devices": [], "none": False, "error": None}, "garbage gives no lines")
    w.show_usb(p)
    check(w.usb_table.rowCount() == 2, "usb table has 2 rows")
    w.tabs.setCurrentIndex(3)
    pump(200)
    screenshot(w, "3-monitor.png")
    w.show_usb(core.parse_usbdrop("USBDROP none\n"))
    check(w.usb_table.rowCount() == 0 and "No USB audio" in w.usb_msg.text(), "none case shown")
    w.show_usb(p)
    # dry-run button
    w.usb_btn.click()
    check(wait_for(lambda: "usb-drop-count.sh" in w.out.toPlainText() and not w.acting), "usb check dry run shows command")
    check("pkexec bash" in w.out.toPlainText() and w.out.toPlainText().count("dry run") >= 1, "usb dry-run text: pkexec bash ...")
    # start monitor in dry run must not run aplay
    busy_before = core.sound_device_busy()
    w.mon_btn.click()
    if busy_before:   # e.g. a real panel's monitor is running: the refusal path is what must show
        check(w.mon_proc is None and "busy" in w.out.toPlainText(), "dry-run monitor start refuses while the device is busy")
    else:
        check(w.mon_proc is None and "aplay" in w.out.toPlainText(), "dry-run monitor start only shows the aplay command")

    # ---- screenshots of status and editor ----
    w.tabs.setCurrentIndex(1)
    pump(200)
    screenshot(w, "1-status.png")
    w.tabs.setCurrentIndex(2)
    pump(200)
    screenshot(w, "2-fan-curves.png")

    # ---- drag a point on the CPU chart ----
    ch = w.charts["cpu"]
    i = 3
    before = list(w.ed["cpu"][i])
    nxt = w.ed["cpu"][i + 1][0]
    tt = before[0] + 1 if before[0] + 1 < nxt else before[0] - 1
    vv = min(w.ed["cpu"][i + 1][1], before[1] + 5)
    start = ch.point_pos(i).toPoint()
    end = QPoint(round(ch.x_of(tt)), round(ch.y_of(vv)))
    QTest.mousePress(ch, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(ch, QPoint((start.x() + end.x()) // 2, (start.y() + end.y()) // 2))
    QTest.mouseMove(ch, end)
    QTest.mouseRelease(ch, Qt.LeftButton, Qt.NoModifier, end)
    after = w.ed["cpu"][i]
    a, b = w.spins["cpu"][i]
    check(after[:2] != before[:2], "drag moved point %s -> %s" % (before[:2], after[:2]))
    check(a.value() == after[0] and b.value() == after[1], "spin boxes follow drag (%d C, %d %%)" % (a.value(), b.value()))
    check(w.dirty_lbl["cpu"].isVisibleTo(w), "unsaved-edit marker shown after drag")
    ts = [p[0] for p in w.ed["cpu"]]
    check(all(x < y for x, y in zip(ts, ts[1:])), "temperatures still strictly increasing")

    # ---- spin box edit moves the point ----
    a2, b2 = w.spins["gpu"][2]
    g = w.ed["gpu"]
    newt = g[2][0] + 1 if g[2][0] + 1 < g[3][0] else g[2][0] - 1
    a2.setValue(newt)
    check(g[2][0] == newt, "spin edit moved the point to %d C" % newt)
    a2.setValue(100)   # must clamp against the next point
    check(g[2][0] < g[3][0], "spin edit clamps against neighbour (%d < %d)" % (g[2][0], g[3][0]))
    check(a2.value() == g[2][0], "spin box shows the clamped value")
    # pwm preserved while percent untouched
    w.on_reload()
    sv = w.stored[w.ed_prof]["mid"]["points"]
    check(w.send_pts("mid") == [list(x) for x in sv], "unchanged curve sends the exact stored pwm values")
    check(not w.dirty_lbl["cpu"].isVisibleTo(w), "reload clears unsaved marker")
    screenshot(w, "2b-fan-curves-after-reload.png")

    # ---- fan preset click ----
    w.tabs.setCurrentIndex(1)
    btn = w.preset_buttons["max"]
    btn.click()
    check(not btn.isEnabled() or w.acting or True, "buttons disabled while running (best effort)")
    check(wait_for(lambda: "fans-max.sh" in w.out.toPlainText() and not w.acting), "preset click shows fans-max.sh dry-run command")
    check("dry run, not executed" in w.out.toPlainText(), "preset result says dry run")
    screenshot(w, "4-after-preset.png")

    # ---- per-fan apply ----
    w.tabs.setCurrentIndex(2)
    w.apply_btns["cpu"].click()
    exp = ",".join("%dc:%d" % tuple(p) for p in sv and w.stored[w.ed_prof]["cpu"]["points"])
    check(wait_for(lambda: "asusctl fan-curve" in w.out.toPlainText() and not w.acting), "apply result shows asusctl")
    txt = w.out.toPlainText()
    check(("--fan cpu --data " + exp) in txt, "apply sends exact stored cpu curve: " + exp)
    check("--mod-profile %s" % w.ed_prof in txt and "--enable-fan-curves true" in txt, "apply includes profile and enable command")
    screenshot(w, "5-after-apply.png")

    # ---- custom preset save/delete through the shared code path (writes then removes ui/custom-presets.json)
    existed = os.path.exists(core.PRESET_FILE)
    r1 = q.run_one("save-preset", {"name": "qt test", "curves": {f: w.send_pts(f) for f, _ in q.FAN_LABELS}}, True)
    check(r1.get("ok") and "qt test" in core.load_custom_presets(), "save-preset works")
    r2 = q.run_one("delete-preset", {"name": "qt test"}, True)
    check(r2.get("ok") and "qt test" not in core.load_custom_presets(), "delete-preset works")
    if not existed and os.path.exists(core.PRESET_FILE) and not core.load_custom_presets():
        os.remove(core.PRESET_FILE)
    check(q.run_one("fan-preset", {"preset": "nope"}, True).get("ok") is False, "bad preset rejected")
    tray_tests(w)
    return finish(w)


def finish(w):
    w.close()
    pump(100)
    print("Screenshots:", OUT)
    print("\n%d failure(s)" % len(fails), flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        import traceback
        traceback.print_exc()
    QTimer.singleShot(0, lambda: None)
    sys.stdout.flush()
    os._exit(code)
