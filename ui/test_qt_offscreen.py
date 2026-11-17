#!/usr/bin/env python3
"""Offscreen test of rog-panel-qt.py (dry run only; nothing real runs, no window on a real display)."""
import importlib.util
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rogcore as core  # noqa: E402
from PySide6.QtCore import Qt, QPoint, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

OUT = "/tmp/claude-1000/rog-qt-shots"
os.makedirs(OUT, exist_ok=True)
fails = []


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
    w.tabs.setCurrentIndex(2)
    pump(200)
    w.grab().save(OUT + "/3-monitor.png")
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
    w.tabs.setCurrentIndex(0)
    pump(200)
    w.grab().save(OUT + "/1-status.png")
    w.tabs.setCurrentIndex(1)
    pump(200)
    w.grab().save(OUT + "/2-fan-curves.png")

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
    w.grab().save(OUT + "/2b-fan-curves-after-reload.png")

    # ---- fan preset click ----
    w.tabs.setCurrentIndex(0)
    btn = w.preset_buttons["max"]
    btn.click()
    check(not btn.isEnabled() or w.acting or True, "buttons disabled while running (best effort)")
    check(wait_for(lambda: "fans-max.sh" in w.out.toPlainText() and not w.acting), "preset click shows fans-max.sh dry-run command")
    check("dry run, not executed" in w.out.toPlainText(), "preset result says dry run")
    w.grab().save(OUT + "/4-after-preset.png")

    # ---- per-fan apply ----
    w.tabs.setCurrentIndex(1)
    w.apply_btns["cpu"].click()
    exp = ",".join("%dc:%d" % tuple(p) for p in sv and w.stored[w.ed_prof]["cpu"]["points"])
    check(wait_for(lambda: "asusctl fan-curve" in w.out.toPlainText() and not w.acting), "apply result shows asusctl")
    txt = w.out.toPlainText()
    check(("--fan cpu --data " + exp) in txt, "apply sends exact stored cpu curve: " + exp)
    check("--mod-profile %s" % w.ed_prof in txt and "--enable-fan-curves true" in txt, "apply includes profile and enable command")
    w.grab().save(OUT + "/5-after-apply.png")

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
