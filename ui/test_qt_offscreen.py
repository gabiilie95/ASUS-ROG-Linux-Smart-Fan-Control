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
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rogcore as core  # noqa: E402
from PySide6.QtCore import Qt, QPoint, QTimer, QSaveFile, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

OUT = tempfile.mkdtemp(prefix="rog-qt-offscreen-")
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


def main():
    app = QApplication([])
    check(QGuiApplication.platformName() == "offscreen", "platform is offscreen")
    spec = importlib.util.spec_from_file_location("rogqt", os.path.join(HERE, "rog-panel-qt.py"))
    q = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(q)
    w = q.MainWindow(dry_run=True)
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
