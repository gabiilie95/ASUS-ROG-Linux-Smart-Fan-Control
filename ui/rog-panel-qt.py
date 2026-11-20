#!/usr/bin/env python3
"""Native Qt (PySide6) version of the ROG G18 settings panel. Same actions as rog-panel.py, no web server."""
import argparse
import atexit
import collections
import getpass
import os
import signal
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rogcore as core  # noqa: E402
from fan_auto import AutoTab, heading
from fan_calibration import CalibrateTab
from fan_tray import FanTray
from panel_settings import PanelSettings, SettingsTab, Updater

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal, QPointF, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPolygonF  # noqa: E402
from PySide6.QtNetwork import QLocalServer, QLocalSocket  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QHeaderView, QSpinBox,
    QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget, QSizePolicy)

FAN_LABELS = (("cpu", "CPU"), ("gpu", "GPU"), ("mid", "Middle"))
TMIN, TMAX = 30, 100
WARN_CSS = "background-color:#b42318;color:white;font-weight:bold;border-radius:3px;padding:0 4px;"
CURVE = QColor("#1f78d1")
TEMP_LINE = QColor("#e08a00")
GOOD = QColor("#2e9e4f")
WINDOW_S = 300


def na(v):
    return "n/a" if v is None else str(v)


def fmt(v, unit, digits=0):
    return "n/a" if v is None else ("%.*f" % (digits, v)) + unit


def pct_of(pwm):
    return round(pwm * 100 / 255)


def pwm_of_pct(p):
    return round(p * 100 / 100 * 255 / 100)


# ---- worker threads --------------------------------------------------------------------------
class Signals(QObject):
    done = Signal(object)


class Job(QRunnable):
    """Work done on a QThreadPool thread; the result comes back to the GUI thread through a Qt signal."""
    def __init__(self):
        super().__init__()
        self.setAutoDelete(False)   # the window keeps the job (and its signal carrier) alive until done

    def start(self):
        QThreadPool.globalInstance().start(self)


class StatusJob(Job):
    def __init__(self, sig):
        super().__init__()
        self.sig = sig

    def run(self):
        try:
            res = core.status()
        except Exception as e:  # never let a reader crash the thread silently
            res = {"_error": str(e)}
        self.sig.done.emit(res)


def format_result(j):
    m = ""
    if j.get("error"):
        m += "Error: %s\n" % j["error"]
    if j.get("stalls_per_s") is not None:
        m += "Stalls per second: %s\n" % j["stalls_per_s"]
    if j.get("output"):
        m += j["output"] + "\n"
    if j.get("note"):
        m += "\n" + j["note"]
    if not m:
        m = "Done." if j.get("ok") else "Failed%s." % (" (exit code %s)" % j["returncode"] if j.get("returncode") is not None else "")
    return m


def run_one(name, body, dry_run):
    """Same flow as the web handler: validate, whitelist, then run or only describe."""
    if not core.busy.acquire(blocking=False):
        return {"ok": False, "error": "another action is running"}
    try:
        if name in ("save-preset", "delete-preset"):
            try:
                return core.preset_action(name, body)
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            except OSError as e:
                return {"ok": False, "error": "could not write presets: %s" % e}
        try:
            argv, timeout, note = core.build_action(name, body)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        multi = bool(argv) and isinstance(argv[0], list)
        if dry_run:
            lines = [" ".join(a) for a in (argv if multi else [argv])]
            res = {"ok": True, "dry_run": True, "command": argv,
                   "output": "dry run, not executed:\n" + "\n".join(lines)}
        else:
            res = core.execute_all(argv, timeout) if multi else core.execute(name, argv, timeout)
            core._cache.clear()
        if note:
            res["note"] = note
        return res
    finally:
        core.busy.release()


class ActionJob(Job):
    def __init__(self, jobs, dry_run, sig):
        super().__init__()
        self.jobs, self.dry_run = jobs, dry_run
        self.sig = sig

    def run(self):
        msg, ok = "", True
        try:
            for name, body, label in self.jobs:
                j = run_one(name, body, self.dry_run)
                msg += (label + ":\n" if label else "") + format_result(j) + "\n"
                if j.get("ok") is False or j.get("error"):
                    ok = False
                    break
        except Exception as e:
            ok, msg = False, msg + "Action failed: %s" % e
        self.sig.done.emit({"ok": ok, "text": msg.rstrip("\n")})


# ---- curve chart -----------------------------------------------------------------------------
class CurveWidget(QWidget):
    dragged = Signal(int, float, float)   # point index, temperature C, speed %
    PL, PR, PT, PB = 40, 10, 10, 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = []           # list of [temp, pct, pwm]
        self._drag = None
        self.setMinimumSize(300, 170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setCursor(Qt.ArrowCursor)

    def sizeHint(self):
        from PySide6.QtCore import QSize
        return QSize(400, 210)

    def set_points(self, pts):
        self.points = pts
        self.update()

    def _plot(self):
        return QRectF(self.PL, self.PT, max(1, self.width() - self.PL - self.PR), max(1, self.height() - self.PT - self.PB))

    def x_of(self, t):
        r = self._plot()
        return r.left() + (t - TMIN) / (TMAX - TMIN) * r.width()

    def y_of(self, v):
        r = self._plot()
        return r.top() + (100 - v) / 100 * r.height()

    def point_pos(self, i):
        """Pixel position (float QPointF) of point i, in widget coordinates."""
        return QPointF(self.x_of(self.points[i][0]), self.y_of(self.points[i][1]))

    def value_at(self, x, y):
        r = self._plot()
        return TMIN + (x - r.left()) / r.width() * (TMAX - TMIN), 100 - (y - r.top()) / r.height() * 100

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = self._plot()
        p.fillRect(r, pal.base())
        grid = QColor(pal.mid().color())
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.85))
        p.setFont(small)
        for v in range(0, 101, 25):
            y = self.y_of(v)
            pen = QPen(grid, 1, Qt.DashLine if v == 100 else Qt.SolidLine)
            p.setPen(pen)
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(pal.windowText().color())
            p.drawText(QRectF(0, y - 8, self.PL - 5, 16), Qt.AlignRight | Qt.AlignVCenter, "%d%%" % v)
        for t in range(TMIN, TMAX + 1, 10):
            x = self.x_of(t)
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(pal.windowText().color())
            p.drawText(QRectF(x - 20, r.bottom() + 2, 40, 14), Qt.AlignCenter, str(t))
        p.drawText(QRectF(r.left(), r.bottom() + 18, r.width(), 14), Qt.AlignRight | Qt.AlignVCenter, "temperature (C)")
        p.setPen(QPen(grid, 1))
        p.drawRect(r)
        if self.points:
            poly = QPolygonF([QPointF(self.x_of(q[0]), self.y_of(q[1])) for q in self.points])
            p.setPen(QPen(CURVE, 2))
            p.drawPolyline(poly)
            for i, q in enumerate(self.points):
                p.setPen(QPen(pal.base().color(), 2))
                p.setBrush(CURVE.lighter(130) if i == self._drag else CURVE)
                p.drawEllipse(QPointF(self.x_of(q[0]), self.y_of(q[1])), 6, 6)
        p.end()

    def _hit(self, pos):
        best, bd = None, 14 * 14
        for i in range(len(self.points)):
            c = self.point_pos(i)
            d = (c.x() - pos.x()) ** 2 + (c.y() - pos.y()) ** 2
            if d <= bd:
                best, bd = i, d
        return best

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag = self._hit(e.position())
            if self._drag is not None:
                self.setCursor(Qt.ClosedHandCursor)
                self.update()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            t, v = self.value_at(e.position().x(), e.position().y())
            self.dragged.emit(self._drag, t, v)
        else:
            self.setCursor(Qt.OpenHandCursor if self._hit(e.position()) is not None else Qt.ArrowCursor)

    def mouseReleaseEvent(self, _e):
        self._drag = None
        self.setCursor(Qt.ArrowCursor)
        self.update()


class UsbJob(Job):
    """Runs the root USB drop check (pkexec) off the GUI thread and parses its USBDROP lines."""
    def __init__(self, dry_run, sig):
        super().__init__()
        self.dry_run = dry_run
        self.sig = sig

    def run(self):
        try:
            j = run_one("usb-drop-check", {}, self.dry_run)
            res = {"ok": bool(j.get("ok")), "text": format_result(j), "parsed": None}
            if not self.dry_run:
                p = core.parse_usbdrop(j.get("output", ""))
                good = j.get("returncode") == 0 and (p["devices"] or p["none"]) and not p["error"]
                res["parsed"] = p if good else None
                res["ok"] = bool(good)
                if not good:
                    res["text"] = "USB drop check failed%s.\nRaw output:\n%s" % (
                        " (%s)" % p["error"] if p["error"] else "", format_result(j))
        except Exception as e:
            res = {"ok": False, "text": "USB drop check failed: %s" % e, "parsed": None}
        self.sig.done.emit(res)


class MonitorChart(QWidget):
    """Rolling chart: stalls/s (left axis) and board sensor temperature (right axis)."""
    PL, PR, PT, PB = 44, 48, 10, 40

    def __init__(self, parent=None):
        super().__init__(parent)
        self.samples = []      # list of (age-ordered) (stalls, board_c or None)
        self.setMinimumSize(500, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_samples(self, samples):
        self.samples = list(samples)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = QRectF(self.PL, self.PT, max(1, self.width() - self.PL - self.PR), max(1, self.height() - self.PT - self.PB))
        p.fillRect(r, pal.base())
        grid = QColor(pal.mid().color())
        small = QFont(self.font())
        small.setPointSizeF(max(7.0, small.pointSizeF() * 0.85))
        p.setFont(small)
        smax = max([s for s, _ in self.samples] + [1.0])
        temps = [t for _, t in self.samples if t is not None]
        tlo, thi = (min(temps) - 2, max(temps) + 2) if temps else (60.0, 90.0)
        if thi - tlo < 10:
            thi = tlo + 10
        for k in range(5):
            y = r.top() + r.height() * k / 4
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(pal.windowText().color())
            p.drawText(QRectF(0, y - 8, self.PL - 5, 16), Qt.AlignRight | Qt.AlignVCenter, "%.1f" % (smax * (1 - k / 4)))
            p.setPen(TEMP_LINE)
            p.drawText(QRectF(r.right() + 5, y - 8, self.PR - 5, 16), Qt.AlignLeft | Qt.AlignVCenter, "%.0f" % (thi - (thi - tlo) * k / 4))
        p.setPen(pal.windowText().color())
        for m in range(0, 6):
            x = r.left() + r.width() * m / 5
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(pal.windowText().color())
            lab = "now" if m == 5 else "-%d min" % (5 - m)
            p.drawText(QRectF(x - 25, r.bottom() + 2, 50, 14), Qt.AlignCenter, lab)
        p.drawText(QRectF(0, r.bottom() + 18, self.PL + 80, 14), Qt.AlignLeft, "stalls/s (left axis)")
        p.setPen(TEMP_LINE)
        p.drawText(QRectF(r.right() - 200, r.bottom() + 18, 200 + self.PR, 14), Qt.AlignRight, "board sensor C (right axis)")
        p.setPen(QPen(grid, 1))
        p.drawRect(r)
        n = len(self.samples)
        if n:
            def x_of(i):
                return r.right() - (n - 1 - i) * r.width() / WINDOW_S
            line = QPolygonF([QPointF(x_of(i), r.bottom() - s / smax * r.height()) for i, (s, _) in enumerate(self.samples)])
            tl = QPolygonF([QPointF(x_of(i), r.bottom() - (t - tlo) / (thi - tlo) * r.height())
                            for i, (_, t) in enumerate(self.samples) if t is not None])
            p.setPen(QPen(TEMP_LINE, 2))
            p.drawPolyline(tl)
            p.setPen(QPen(CURVE, 2))
            p.drawPolyline(line)
        p.end()


# ---- main window -----------------------------------------------------------------------------
def card(title):
    g = QGroupBox(title)
    form = QVBoxLayout(g)
    return g, form


def lock_note():
    """A visible reason why some controls are locked (hidden while nothing is)."""
    l = QLabel("")
    l.setWordWrap(True)
    l.setStyleSheet("border-left: 3px solid #388ab9; padding: 4px 8px;")
    l.hide()
    return l


def note_label(text):
    l = QLabel(text)
    l.setWordWrap(True)
    f = l.font()
    f.setItalic(True)
    l.setFont(f)
    return l


def app_icon():
    """The red ROG logo asusctl installs (loaded by name, never copied: ASUS trademark)."""
    return QIcon.fromTheme("asus_notif_red", QIcon.fromTheme("preferences-system"))


class MainWindow(QMainWindow):
    def __init__(self, dry_run=False):
        super().__init__()
        self.dry_run = dry_run
        self.setWindowTitle("ROG Settings Panel")
        self.setWindowIcon(app_icon())
        self.jobs = set()
        self.tray = None
        self.tray_allowed = False    # main() sets this when the desktop has a system tray
        self.quitting = False
        self.refreshing = False
        self.acting = False
        self.action_buttons = []
        self.first_status = False
        self.last_status = None
        self.cap_set = False
        self.preset_buttons = {}
        self.preset_built = False
        # editor state
        self.stored = None
        self.ed = None
        self.ed_prof = None
        self.active_prof = None
        self.ed_built = False
        self.preset_sig = None
        self.preset_data = {"builtin": {}, "custom": {}}
        self.mon_proc = None
        self.mon_rate = core.StallRate()
        self.mon_samples = collections.deque(maxlen=WINDOW_S)
        self.mon_timer = QTimer(self)
        self.mon_timer.setInterval(1000)
        self.mon_timer.timeout.connect(self.monitor_tick)
        self.usb_busy = False
        self.charts, self.spins, self.dirty_lbl, self.en_lbl, self.apply_btns = {}, {}, {}, {}, {}

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(8)
        masthead = QFrame()
        masthead.setObjectName("masthead")
        header = QHBoxLayout(masthead)
        header.setContentsMargins(16, 10, 16, 10)
        header.setSpacing(16)
        header.addWidget(heading("ROG Settings Panel"))
        header.addWidget(QLabel("Strix G18  ·  G814JZ"))
        header.addStretch()
        self.banner = QLabel("Loading…")
        header.addWidget(self.banner)
        root.addWidget(masthead)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.auto_tab = AutoTab(dry_run)
        self.auto_tab.timer.stop()   # status timers run only while the window is shown
        self.cal_tab = CalibrateTab(dry_run, lambda: self.acting)
        self.close_after_calibration = False
        self.cal_tab.runningChanged.connect(self.calibration_changed)
        self.prefs = PanelSettings(self)
        self.updater = Updater(self, self.prefs)
        self.settings_tab = SettingsTab(self)
        self.prefs.changed.connect(self.pref_changed)
        setup = self.auto_tab.calibration
        setup.set_suggest(self.prefs.get("calibration_setup"))
        setup.startRequested.connect(self.start_calibration)
        setup.dismissed.connect(lambda: self.prefs.set("calibration_setup", False))
        setup.openCalibrate.connect(lambda: self.tabs.setCurrentWidget(self.cal_scroll))
        self.cal_tab.proposalChanged.connect(self.auto_tab.set_proposal)
        self.tabs.addTab(self._scroll(self.auto_tab), "Overview")
        self.tabs.addTab(self._scroll(self._build_status_tab()), "Status")
        self.tabs.addTab(self._build_editor_tab(), "Fan curves")
        self.tabs.addTab(self._scroll(self._build_monitor_tab()), "Monitor")
        self.cal_scroll = self._scroll(self.cal_tab)
        self.tabs.addTab(self.cal_scroll, "Calibrate")
        self.settings_scroll = self._scroll(self.settings_tab)
        self.tabs.addTab(self.settings_scroll, "Settings")
        gb, lay = card("Last result")
        self.out = QPlainTextEdit("Nothing run yet.")
        self.out.setReadOnly(True)
        self.out.setMaximumHeight(100)
        lay.addWidget(self.out)
        root.addWidget(gb)
        self.tabs.currentChanged.connect(
            lambda i: gb.setVisible(self.tabs.tabText(i) not in ("Overview", "Calibrate", "Settings")))
        gb.hide()
        self.setStyleSheet("""
            QFrame#masthead {background: #192e40; border-radius: 7px;}
            QFrame#masthead QLabel {color: #edf6fc;}
            QTabWidget::pane {border: 0;}
            QTabBar::tab {padding: 7px 16px; margin-right: 3px; color: palette(window-text);
                          border-bottom: 3px solid transparent;}
            QTabBar::tab:selected {border-color: #388ab9; font-weight: bold;}
            QGroupBox {background: palette(base); border: 1px solid palette(midlight);
                       border-radius: 6px; margin-top: 11px; padding: 8px 4px 3px;}
            QGroupBox::title {subcontrol-origin: margin; left: 10px; padding: 0 4px;}
            QPushButton, QComboBox, QSpinBox, QLineEdit {padding: 3px 6px;}
            QProgressBar {border: 1px solid palette(midlight); border-radius: 4px;
                          text-align: center; background: palette(alternate-base);}
            QProgressBar::chunk {background: #3684ad; border-radius: 3px;}
            QPushButton#primaryAction {min-width: 88px; padding: 5px 13px; border-radius: 4px;}
            QPushButton#primaryAction:enabled {color: white; background: #236b92; border: 0;}
            QPushButton#primaryAction:pressed {background: #195576;}
            QGroupBox#calibrationCard[prominent="true"] {border: 2px solid #388ab9;}
        """)
        self._set_banner()
        self.resize(1280, 800)

        self.timer = QTimer(self)
        self.timer.setInterval(2000)
        self.timer.timeout.connect(self.refresh)
        QTimer.singleShot(0, self.refresh)

    def showEvent(self, e):
        super().showEvent(e)
        if not self.timer.isActive():
            self.timer.start()
            self.auto_tab.timer.start()
            self.auto_tab.refresh()
            QTimer.singleShot(0, self.refresh)

    def hideEvent(self, e):
        super().hideEvent(e)
        # Hidden in the tray, only the tray's light service poll keeps running.
        self.timer.stop()
        self.auto_tab.timer.stop()

    def apply_tray(self):
        """Show or remove the tray icon to match the setting (a desktop may have no tray at all)."""
        want = self.tray_allowed and self.prefs.get("tray")
        if want and self.tray is None:
            self.tray = FanTray(self)
        elif not want and self.tray is not None:
            tray, self.tray = self.tray, None
            tray.remove()
            tray.deleteLater()
            self.setWindowIcon(app_icon())   # no state colour without the tray
            if not self.isVisible() and not self.quitting:
                self.show()          # nothing else would lead back to a hidden window
        self.settings_tab.refresh()
        return self.tray

    def pref_changed(self, key, value):
        if key == "tray":
            self.apply_tray()
        elif key == "calibration_setup":
            self.auto_tab.calibration.set_suggest(value)

    def start_calibration(self, kind):
        """The Overview's setup buttons: run auto-tune from the Calibrate tab, where it can be followed."""
        self.tabs.setCurrentWidget(self.cal_scroll)
        self.cal_tab.start_autotune(kind)

    def show_settings(self):
        self.tabs.setCurrentWidget(self.settings_scroll)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    @staticmethod
    def _scroll(w):
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setWidget(w)
        return sa

    def _set_banner(self):
        if self.dry_run:
            self.banner.setText("DRY RUN  ·  Hardware controls preview only")
            self.banner.setStyleSheet("background:#365060;color:#f1f8fc;border-radius:4px;padding:4px 8px;")
        else:
            self.banner.setText("Live controls")
            self.banner.setStyleSheet("")

    def launch(self, make_job, on_done):
        """Run a QRunnable on the thread pool; the signal carrier lives in the GUI thread and is kept alive here."""
        sig = Signals(self)
        job = make_job(sig)
        entry = (job, sig)
        self.jobs.add(entry)

        def done(res):
            self.jobs.discard(entry)
            sig.deleteLater()
            on_done(res)
        sig.done.connect(done)
        job.start()

    def add_action_button(self, text, slot=None):
        b = QPushButton(text)
        if slot:
            b.clicked.connect(slot)
        self.action_buttons.append(b)
        return b

    # -- status tab --
    def _build_status_tab(self):
        w = QWidget()
        g = QGridLayout(w)
        self.read_forms = {}
        for idx, (key, title) in enumerate((("temps", "Temperatures"), ("fans", "Fans"), ("cpu", "CPU"), ("gpu", "GPU"))):
            box, lay = card(title)
            form = QFormLayout()
            lay.addLayout(form)
            lay.addStretch(1)
            self.read_forms[key] = form
            g.addWidget(box, 0, idx)

        # fan preset
        box, lay = card("Fan preset")
        self.curmatch = QLabel("")
        lay.addWidget(self.curmatch)
        self.preset_lock = lock_note()
        lay.addWidget(self.preset_lock)
        self.preset_lay = QGridLayout()
        lay.addLayout(self.preset_lay)
        lay.addStretch(1)
        g.addWidget(box, 1, 0, 1, 2)
        # jellyfin
        box, lay = card("Jellyfin software interface")
        self.jf = QLabel("")
        lay.addWidget(self.jf)
        row = QHBoxLayout()
        row.addWidget(self.add_action_button("Turn on", lambda: self.act("jellyfin", {"value": "on"})))
        row.addWidget(self.add_action_button("Turn off", lambda: self.act("jellyfin", {"value": "off"})))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(note_label("Applies at Jellyfin's next start. Jellyfin is not restarted."))
        lay.addStretch(1)
        g.addWidget(box, 1, 2)
        # cpu cap
        box, lay = card("CPU cap")
        row = QHBoxLayout()
        self.cap = QSpinBox()
        self.cap.setRange(40, 100)
        self.cap.setValue(55)
        self.cap.setSuffix(" %")
        row.addWidget(self.cap)
        row.addWidget(self.add_action_button("Apply cap", lambda: self.act("cpu-cap", {"cap": self.cap.value()})))
        row.addWidget(self.add_action_button("Undo CPU profile", lambda: self.act("cpu-undo")))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(note_label("Saved for every power mode and reapplied after reboot. "
                                 "Asks for your password in a system dialog (pkexec)."))
        lay.addStretch(1)
        g.addWidget(box, 1, 3)
        # stall test
        box, lay = card("Stall test")
        row = QHBoxLayout()
        row.addWidget(self.add_action_button("Run 10 s stall test", lambda: self.act("stall-test")))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(note_label("About 12 s. Plays inaudible silence on the internal sound device."))
        lay.addStretch(1)
        g.addWidget(box, 2, 0, 1, 4)
        g.setRowStretch(3, 1)
        return w

    # -- editor tab --
    def _build_editor_tab(self):
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        outer.setFrameShape(QFrame.NoFrame)
        w = QWidget()
        outer.setWidget(w)
        lay = QVBoxLayout(w)
        self.edmsg = QLabel("")
        self.edmsg.setWordWrap(True)
        lay.addWidget(self.edmsg)
        self.edmain = QWidget()
        lay.addWidget(self.edmain)
        lay.addStretch(1)
        m = QVBoxLayout(self.edmain)
        m.setContentsMargins(0, 0, 0, 0)

        row = QHBoxLayout()
        row.addWidget(QLabel("Profile:"))
        self.prof_combo = QComboBox()
        for n in core.PROFILES:
            self.prof_combo.addItem(n, n)
        self.prof_combo.activated.connect(self.on_profile_chosen)
        row.addWidget(self.prof_combo)
        self.edact = QLabel("")
        row.addWidget(self.edact)
        row.addStretch(1)
        m.addLayout(row)
        self.curve_lock = lock_note()
        m.addWidget(self.curve_lock)
        self.warn_box = QVBoxLayout()
        self.warn_labels = []
        for _ in range(3):
            l = QLabel("")
            l.setWordWrap(True)
            l.setStyleSheet(WARN_CSS)
            l.hide()
            self.warn_box.addWidget(l)
            self.warn_labels.append(l)
        m.addLayout(self.warn_box)

        fans = QHBoxLayout()
        m.addLayout(fans)
        for f, label in FAN_LABELS:
            col = QVBoxLayout()
            head = QHBoxLayout()
            h = QLabel("<b>%s fan</b>" % label)
            head.addWidget(h)
            dv = QLabel("Unsaved edits")
            dv.setStyleSheet(WARN_CSS)
            dv.hide()
            head.addWidget(dv)
            en = QLabel("")
            head.addWidget(en)
            head.addStretch(1)
            ap = self.add_action_button("Apply")
            ap.clicked.connect(lambda _=False, fan=f: self.apply_fans([fan]))
            head.addWidget(ap)
            col.addLayout(head)
            chart = CurveWidget()
            chart.dragged.connect(lambda i, t, v, fan=f: self.on_drag(fan, i, t, v))
            col.addWidget(chart, 1)
            grid = QGridLayout()
            grid.setSpacing(3)
            sp_list = []
            for i in range(8):
                a, b = QSpinBox(), QSpinBox()
                a.setRange(TMIN, TMAX)
                b.setRange(0, 100)
                a.setSuffix(" C")
                b.setSuffix(" %")
                for x in (a, b):
                    x.setKeyboardTracking(False)
                a.setToolTip("Point %d temperature (C)" % (i + 1))
                b.setToolTip("Point %d speed (%%)" % (i + 1))
                a.valueChanged.connect(lambda v, fan=f, i=i: self.on_spin(fan, i, "t", v))
                b.valueChanged.connect(lambda v, fan=f, i=i: self.on_spin(fan, i, "p", v))
                grid.addWidget(a, i % 4, (i // 4) * 2)
                grid.addWidget(b, i % 4, (i // 4) * 2 + 1)
                sp_list.append((a, b))
            col.addLayout(grid)
            fans.addLayout(col, 1)
            self.charts[f], self.spins[f], self.dirty_lbl[f], self.en_lbl[f], self.apply_btns[f] = chart, sp_list, dv, en, ap

        row = QHBoxLayout()
        self.apply_all = self.add_action_button("Apply all three", lambda: self.apply_fans([f for f, _ in FAN_LABELS]))
        row.addWidget(self.apply_all)
        b = QPushButton("Reload from saved")
        b.clicked.connect(self.on_reload)
        self.action_buttons.append(b)
        row.addWidget(b)
        self.eddirty = QLabel("Unsaved edits")
        self.eddirty.setStyleSheet(WARN_CSS)
        self.eddirty.hide()
        row.addWidget(self.eddirty)
        row.addStretch(1)
        m.addLayout(row)
        m.addWidget(note_label("Drag points, or type values. Temperature in C, speed in %. Applying runs asusctl for the chosen profile."))

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        m.addWidget(line)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Presets</b>"))
        self.psel = QComboBox()
        self.psel.setMinimumWidth(200)
        self.psel.currentIndexChanged.connect(self._psel_changed)
        row.addWidget(self.psel)
        bl = QPushButton("Load preset into editor")
        bl.clicked.connect(self.on_preset_load)
        self.pdel = QPushButton("Delete custom preset")
        self.pdel.clicked.connect(self.on_preset_delete)
        self.action_buttons += [bl, self.pdel]
        row.addWidget(bl)
        row.addWidget(self.pdel)
        row.addWidget(note_label("Loading only fills the editor; nothing is applied."), 1)
        m.addLayout(row)
        row = QHBoxLayout()
        self.pname = QLineEdit()
        self.pname.setMaxLength(32)
        self.pname.setPlaceholderText("Preset name")
        self.pname.setMaximumWidth(240)
        row.addWidget(self.pname)
        bs = QPushButton("Save as preset")
        bs.clicked.connect(self.on_preset_save)
        self.action_buttons.append(bs)
        row.addWidget(bs)
        row.addWidget(note_label("Saves all three fan curves as edited. Does not apply them."), 1)
        m.addLayout(row)
        self.edmain.hide()
        return outer

    # -- monitor tab --
    def _build_monitor_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box, bl = card("Live stall monitor")
        bl.addWidget(QLabel("<b>Chipset stalls per second (same events that drop USB audio packets; 0 = clean)</b>"))
        row = QHBoxLayout()
        self.mon_btn = QPushButton("Start monitor")
        self.mon_btn.setCheckable(True)
        self.mon_btn.clicked.connect(self.on_monitor_toggle)
        row.addWidget(self.mon_btn)
        self.mon_now = QLabel("Now: n/a")
        f = self.mon_now.font()
        f.setBold(True)
        self.mon_now.setFont(f)
        row.addWidget(self.mon_now)
        self.mon_stats = QLabel("Peak (5 min): n/a    Average (5 min): n/a")
        row.addWidget(self.mon_stats)
        row.addStretch(1)
        bl.addLayout(row)
        self.mon_msg = QLabel("Stopped.")
        bl.addWidget(self.mon_msg)
        bl.addWidget(note_label("While the monitor runs it plays inaudible silence on the internal sound device, so the internal speakers cannot play other sound. It is not started automatically."))
        self.mon_chart = MonitorChart()
        bl.addWidget(self.mon_chart, 1)
        lay.addWidget(box, 2)

        box, ul = card("USB drop check")
        row = QHBoxLayout()
        self.usb_btn = self.add_action_button("Check USB drops (10 s, asks for password)", self.on_usb_check)
        row.addWidget(self.usb_btn)
        row.addStretch(1)
        ul.addLayout(row)
        self.usb_msg = QLabel("Not run yet. Plays nothing itself: start USB audio first, then run the check.")
        self.usb_msg.setWordWrap(True)
        ul.addWidget(self.usb_msg)
        self.usb_table = QTableWidget(0, 5)
        self.usb_table.setHorizontalHeaderLabels(["Device", "Packets", "Skipped", "Percent skipped", "Drop events/s"])
        self.usb_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.usb_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.usb_table.setMaximumHeight(110)
        ul.addWidget(self.usb_table)
        ul.addWidget(note_label("Percent and counts are estimates: usbmon's text interface shows only the first 5 packets of each URB."))
        lay.addWidget(box, 1)
        return w

    def monitor_running(self):
        return self.mon_proc is not None

    def on_monitor_toggle(self, checked):
        if checked:
            self.monitor_start()
        else:
            self.monitor_stop("Stopped.")

    def monitor_start(self):
        if self.mon_proc is not None:
            return
        if self.cal_tab.process:
            self.mon_btn.setChecked(False)
            self.mon_msg.setText("Calibration is using the stall counter.")
            return
        if core.sound_device_busy():
            self.mon_btn.setChecked(False)
            self.mon_msg.setText("internal sound device busy")
            self.out.setPlainText("Error: internal sound device busy")
            return
        if self.dry_run:
            self.out.setPlainText("dry run, not executed:\n" + " ".join(core.MONITOR_APLAY))
            self.mon_btn.setChecked(False)
            self.mon_msg.setText("Dry run: aplay not started.")
            return
        try:
            self.mon_proc = subprocess.Popen(core.MONITOR_APLAY, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, cwd=core.ROOT)
        except OSError as e:
            self.mon_btn.setChecked(False)
            self.mon_msg.setText("Could not start aplay: %s" % e)
            return
        self.mon_rate = core.StallRate()
        self.mon_samples.clear()
        self.mon_btn.setChecked(True)   # also when started from the tray
        self.mon_btn.setText("Stop monitor")
        self.mon_msg.setText("Running (aplay pid %d)." % self.mon_proc.pid)
        self.mon_timer.start()
        self.monitor_tick()

    def monitor_stop(self, msg):
        self.mon_timer.stop()
        p, self.mon_proc = self.mon_proc, None
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(2)
                    except subprocess.TimeoutExpired:
                        p.kill()
                        p.wait(2)
            except OSError:
                pass
        self.mon_btn.setChecked(False)
        self.mon_btn.setText("Start monitor")
        self.mon_msg.setText(msg)

    def monitor_tick(self):
        if self.mon_proc is not None and self.mon_proc.poll() is not None:
            code = self.mon_proc.returncode
            self.monitor_stop("aplay exited on its own (code %s); monitor stopped." % code)
            self.out.setPlainText("aplay exited on its own (code %s); monitor stopped." % code)
            return
        board = core.read_temps().get("board_c")
        self.monitor_feed(core.read_irq_total(), time.monotonic(), board)

    def monitor_feed(self, total, now, board=None):
        """Feed one IRQ total (callable from tests with synthetic counts). Returns stalls/s or None."""
        rate = self.mon_rate.feed(total, now)
        if rate is None:
            return None
        self.mon_samples.append((rate, board))
        vals = [s for s, _ in self.mon_samples]
        self.mon_now.setText("Now: %.2f stalls/s" % rate)
        self.mon_stats.setText("Peak (5 min): %.2f    Average (5 min): %.2f" % (max(vals), sum(vals) / len(vals)))
        self.mon_chart.set_samples(self.mon_samples)
        return rate

    def on_usb_check(self):
        if self.acting or self.usb_busy:
            return
        self.usb_busy = True
        self.set_busy(True)
        self.out.setPlainText("Running USB drop check (about 10 s). A password dialog will appear...")
        self.launch(lambda sig: UsbJob(self.dry_run, sig), self._usb_done)

    def _usb_done(self, res):
        self.usb_busy = False
        self.out.setPlainText(res["text"])
        self.set_busy(False)
        if res.get("parsed") is not None:
            self.show_usb(res["parsed"])
        elif not self.dry_run:
            self.usb_msg.setText("Check failed; see Last result.")

    def show_usb(self, p):
        self.usb_table.setRowCount(0)
        if p["none"] and not p["devices"]:
            self.usb_msg.setText("No USB audio was playing during the check.")
            return
        self.usb_msg.setText("Estimates from a 10 s check.")
        for d in p["devices"]:
            r = self.usb_table.rowCount()
            self.usb_table.insertRow(r)
            vals = [d["name"], "%d" % d["packets"], "%d" % d["skipped"], "%.2f %%" % d["pct"], "%.2f" % d["events_per_s"]]
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if c == 3:
                    if d["pct"] > 0:
                        it.setBackground(QColor("#b42318"))
                        it.setForeground(QColor("white"))
                    else:
                        it.setForeground(GOOD)
                self.usb_table.setItem(r, c, it)

    def calibration_changed(self, running):
        self.auto_tab.calibrating = running
        self.auto_tab.refresh()
        self.apply_locks()
        if not running and self.close_after_calibration:
            QTimer.singleShot(0, self.quit_app)

    def closeEvent(self, e):
        if self.tray is not None and self.tray.keep_running() and not self.quitting:
            e.ignore()
            self.hide()
            self.tray.hidden_notice()
            return
        # Accept when quitting, so a session logout is never refused.
        e.setAccepted(self.quit_app())

    def quit_app(self):
        """Quit now, or after a running calibration has restored the exact curve (returns False then)."""
        self.quitting = True
        if self.cal_tab.process:
            self.close_after_calibration = True
            self.cal_tab.stop()
            return False
        self.shutdown()
        self.hide()
        QApplication.quit()
        return True

    def shutdown(self):
        self.auto_tab.shutdown()
        self.updater.stop()
        self.monitor_stop("Stopped.")
        if self.tray is not None:
            self.tray.remove()

    # ---- refresh ----
    def refresh(self):
        if self.refreshing:
            return
        self.refreshing = True
        self.launch(StatusJob, self._status_done)

    def _status_done(self, res):
        self.refreshing = False
        if res.get("_error"):
            self.banner.setText("Cannot read status: " + res["_error"])
            return
        self._set_banner()
        self.last_status = res
        self.auto_tab.set_system(res)
        self.render(res)
        self.first_status = True

    def _fill(self, key, rows):
        form = self.read_forms[key]
        while form.rowCount():
            form.removeRow(0)
        for k, v, bad in rows:
            val = QLabel(str(na(v)) if not isinstance(v, str) else v)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if bad:
                val.setStyleSheet(WARN_CSS)
            form.addRow(k, val)

    def render(self, s):
        t = s["temps"]
        self._fill("temps", [
            ("CPU package", fmt(t["cpu_c"], " C"), False),
            ("Board sensor (SEN3)", fmt(t["board_c"], " °C"), t["board_hot"])])
        f, c = s["fans"], s["curves"]["fans"]
        rows = [(n + " fan", fmt(f[k + "_rpm"], " rpm") + " (curve top " + fmt(c[k]["top_pct"], " %") + ")", False)
                for k, n in FAN_LABELS]
        rows.append(("Live curve matches", s["curves"]["matches_preset"] or "no preset", False))
        self._fill("fans", rows)
        u = s["cpu"]
        self._fill("cpu", [("Max / min perf", "%s %% / %s %%" % (na(u["max_perf_pct"]), na(u["min_perf_pct"])), False),
                           ("Governor", na(u["governor"]), False), ("Energy preference", na(u["epp"]), False),
                           ("tuned profile", na(u["tuned"]), False), ("Platform profile", na(u["platform_profile"]), False)])
        g = s["gpu"]
        rows = [("Temperature", fmt(g["temp_c"], " C"), False), ("Power", fmt(g["power_w"], " W", 1), False),
                ("Utilisation", fmt(g["util_pct"], " %"), False),
                ("Memory", fmt(g["mem_used_mb"], " MB") + " / " + fmt(g["mem_total_mb"], " MB") + ("  above 95 %" if g["mem_high"] else ""), g["mem_high"])]
        if not g["apps"]:
            rows.append(("Top apps", "n/a", False))
        for a in g["apps"]:
            name = a["name"].replace("\\", "/").split("/")[-1].split(" ")[0]
            rows.append(("%s (%s)" % (name, a["pid"]), "%s MB" % a["mem_mb"], False))
        self._fill("gpu", rows)
        self.jf.setText("Software interface is currently: " + na(s["jellyfin_software_ui"]))
        match = s["curves"]["matches_preset"]
        self.curmatch.setText("Live curve matches: " + match if match else "Live curve matches no preset.")
        if not self.preset_built:
            self.preset_built = True
            for k, d in s["presets"].items():
                b = self.add_action_button(k, lambda _=False, name=k: self.act("fan-preset", {"preset": name}))
                b.setToolTip(d)
                b.setProperty("presetName", k)
                self.preset_buttons[k] = b
                n = len(self.preset_buttons) - 1
                cell = QVBoxLayout()
                cell.addWidget(b)
                cell.addWidget(note_label(d))
                self.preset_lay.addLayout(cell, n // 2, n % 2)
        for k, b in self.preset_buttons.items():
            b.setText(k + "  (live)" if k == match else k)
            fnt = b.font()
            fnt.setBold(k == match)
            b.setFont(fnt)
        if not self.cap_set and isinstance(u["max_perf_pct"], int):
            self.cap.setValue(min(100, max(40, u["max_perf_pct"])))
            self.cap_set = True
        try:
            self.render_editor(s)
        except Exception as e:
            self.edmsg.setText("Editor error: %s" % e)
        self.apply_locks()

    # ---- actions ----
    def set_busy(self, busy):
        self.acting = busy
        for b in self.action_buttons:
            b.setEnabled(not busy)
        if not busy:
            self.pdel.setEnabled(self.psel.currentData() is not None and str(self.psel.currentData()).startswith("c:"))
            self.apply_locks()

    def lock_reason(self, profile):
        state, s = core.fan_service()
        return core.curve_lock(profile, state, s, calibrating=bool(self.cal_tab.process))

    def apply_locks(self):
        """Disable applying curves the fan service or a calibration run owns, and say why."""
        perf = self.lock_reason("performance")
        for b in self.preset_buttons.values():
            b.setEnabled(not self.acting and perf is None)
        self.preset_lock.setText(perf or "")
        self.preset_lock.setVisible(perf is not None)
        reason = self.lock_reason(self.ed_prof) if self.ed_prof else None
        for b in [*self.apply_btns.values(), self.apply_all]:
            b.setEnabled(not self.acting and reason is None)
            b.setToolTip(reason or "")
        self.curve_lock.setText(reason or "")
        self.curve_lock.setVisible(reason is not None)

    def act(self, name, body=None):
        self.act_many([(name, body or {}, None)])

    def act_many(self, jobs):
        if self.acting:
            return
        if self.cal_tab.process and any(j[0] in ("fan-curve", "fan-preset", "stall-test") for j in jobs):
            self.show_error("Finish the calibration session first.")
            return
        for name, body, _ in jobs:
            profile = "performance" if name == "fan-preset" else body.get("profile") if name == "fan-curve" else None
            reason = self.lock_reason(profile) if profile else None
            if reason:
                self.show_error(reason)
                self.apply_locks()
                return
        self.set_busy(True)
        self.out.setPlainText("Running %s..." % ", ".join(j[2] or j[0] for j in jobs))
        self.launch(lambda sig: ActionJob(jobs, self.dry_run, sig), self._action_done)

    def _action_done(self, res):
        self.out.setPlainText(res["text"])
        self.set_busy(False)
        self.refresh()

    def show_error(self, msg):
        self.out.setPlainText("Error: " + msg)

    # ---- fan curve editor ----
    def from_pwm(self, pts):
        return [[t, pct_of(w), w] for t, w in pts]

    @staticmethod
    def pwm_of(p):
        return p[2] if p[2] is not None else round(p[1] * 255 / 100)

    def send_pts(self, f):
        return [[p[0], self.pwm_of(p)] for p in self.ed[f]]

    def is_dirty(self, f):
        sv = self.stored and self.stored.get(self.ed_prof, {}).get(f, {}).get("points")
        if not sv:
            return False
        return any(p[0] != sv[i][0] or self.pwm_of(p) != sv[i][1] for i, p in enumerate(self.ed[f]))

    def any_dirty(self):
        return bool(self.ed) and any(self.is_dirty(f) for f, _ in FAN_LABELS)

    def load_from_stored(self):
        self.ed = {f: self.from_pwm(self.stored[self.ed_prof][f]["points"]) for f, _ in FAN_LABELS}

    def clamp_point(self, f, i, t, v):
        a = self.ed[f]
        t, v = round(t), round(v)
        t = max(a[i - 1][0] + 1 if i > 0 else TMIN, min(a[i + 1][0] - 1 if i < 7 else TMAX, t))
        t = max(TMIN + i, min(TMAX - (7 - i), t))
        v = max(a[i - 1][1] if i > 0 else 0, min(a[i + 1][1] if i < 7 else 100, v))
        return t, v

    def set_point(self, f, i, t, v):
        p = self.ed[f][i]
        nt, nv = self.clamp_point(f, i, t, v)
        if nv != p[1]:
            p[2] = None     # percent edited: pwm is recomputed from it; otherwise the stored pwm is kept
        p[0], p[1] = nt, nv

    def redraw(self, f):
        self.charts[f].set_points(self.ed[f])
        self.charts[f].update()
        for i, p in enumerate(self.ed[f]):
            a, b = self.spins[f][i]
            for sp, val in ((a, p[0]), (b, p[1])):
                if sp.value() != val:
                    sp.blockSignals(True)
                    sp.setValue(val)
                    sp.blockSignals(False)

    def full_redraw(self):
        for f, _ in FAN_LABELS:
            self.redraw(f)
        self.refresh_editor()

    def warn_list(self):
        w = []
        if not self.ed:
            return w
        if self.ed_prof == "performance":
            if any(62 <= p[0] <= 70 and p[1] < 70 for p in self.ed["mid"]):
                w.append("The calibrated light-load curve uses 70% middle fan from 62 °C.")
            if any(p[0] >= 72 and p[1] < 80 for fan in core.FANS for p in self.ed[fan]):
                w.append("The calibrated heavy-load curve reaches 80% on all three fans at 72 °C.")
        if self.ed_prof != self.active_prof:
            w.append("Editing %s, which is not the active profile (%s). The change takes effect when %s is selected."
                     % (self.ed_prof, na(self.active_prof), self.ed_prof))
        return w

    def refresh_editor(self):
        if not self.ed_built or not self.stored:
            return
        anyd = False
        for f, _ in FAN_LABELS:
            d = self.is_dirty(f)
            anyd = anyd or d
            self.dirty_lbl[f].setVisible(d)
            self.apply_btns[f].setText("Apply to " + self.ed_prof)
            self.en_lbl[f].setText("saved curve " + ("enabled" if self.stored[self.ed_prof][f]["enabled"] else "disabled"))
        self.eddirty.setVisible(anyd)
        msgs = self.warn_list()
        for i, l in enumerate(self.warn_labels):
            l.setText(msgs[i] if i < len(msgs) else "")
            l.setVisible(i < len(msgs))
        self.edact.setText("Active profile: " + na(self.active_prof))
        self.apply_locks()

    def render_editor(self, s):
        self.stored = s["stored_curves"]
        p = s["cpu"].get("platform_profile")
        self.active_prof = p if p in core.PROFILES else None
        self.fill_presets(s)
        if not self.stored:
            self.edmsg.setText("Could not read or parse /etc/asusd/fan_curves.ron, so the editor is unavailable.")
            self.edmain.hide()
            return
        self.edmsg.setText("")
        self.edmain.show()
        if not self.ed_built:
            self.ed_built = True
            self.ed_prof = self.active_prof or "performance"
            self.prof_combo.setCurrentIndex(self.prof_combo.findData(self.ed_prof))
            self.load_from_stored()
            self.full_redraw()
        for i in range(self.prof_combo.count()):
            n = self.prof_combo.itemData(i)
            self.prof_combo.setItemText(i, n + (" (active)" if n == self.active_prof else ""))
        self.refresh_editor()

    def fill_presets(self, s):
        sig = (tuple(s.get("preset_curves", {})), tuple(s.get("custom_presets", {})))
        self.preset_data = {"builtin": s.get("preset_curves", {}), "custom": s.get("custom_presets", {})}
        if sig != self.preset_sig:
            self.preset_sig = sig
            old = self.psel.currentData()
            self.psel.blockSignals(True)
            self.psel.clear()
            for k in self.preset_data["builtin"]:
                self.psel.addItem("Built-in: " + k, "b:" + k)
            for k in self.preset_data["custom"]:
                self.psel.addItem("Custom: " + k, "c:" + k)
            idx = self.psel.findData(old)
            self.psel.setCurrentIndex(idx if idx >= 0 else 0)
            self.psel.blockSignals(False)
        if not self.acting:
            self.pdel.setEnabled(str(self.psel.currentData() or "").startswith("c:"))

    def _psel_changed(self, _i):
        if not self.acting:
            self.pdel.setEnabled(str(self.psel.currentData() or "").startswith("c:"))

    # ---- editor events ----
    def confirm(self, title, text):
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def on_drag(self, f, i, t, v):
        if not self.ed:
            return
        self.set_point(f, i, t, v)
        self.redraw(f)
        self.refresh_editor()

    def on_spin(self, f, i, kind, value):
        if not self.ed:
            return
        p = self.ed[f][i]
        if kind == "t":
            self.set_point(f, i, value, p[1])
        else:
            self.set_point(f, i, p[0], value)
        self.redraw(f)
        a, b = self.spins[f][i]   # show the clamped value
        for sp, val in ((a, p[0]), (b, p[1])):
            if sp.value() != val:
                sp.blockSignals(True)
                sp.setValue(val)
                sp.blockSignals(False)
        self.refresh_editor()

    def on_profile_chosen(self, idx):
        new = self.prof_combo.itemData(idx)
        if new == self.ed_prof:
            return
        if self.any_dirty() and not self.confirm("Fan curves", "Discard unsaved edits and switch profile?"):
            self.prof_combo.setCurrentIndex(self.prof_combo.findData(self.ed_prof))
            return
        self.ed_prof = new
        self.load_from_stored()
        self.full_redraw()

    def on_reload(self):
        if not self.stored:
            return
        self.load_from_stored()
        self.full_redraw()
        self.out.setPlainText("Edits discarded; showing saved curves for %s." % self.ed_prof)

    def check_send(self, f):
        try:
            core.validate_points(self.send_pts(f), f)
            return True
        except ValueError as e:
            self.show_error(str(e))
            return False

    def apply_fans(self, fans):
        if not self.ed or not all(self.check_send(f) for f in fans):
            return
        self.act_many([("fan-curve", {"profile": self.ed_prof, "fan": f, "points": self.send_pts(f)},
                        "%s -> %s" % (f, self.ed_prof)) for f in fans])

    def on_preset_load(self):
        v = self.psel.currentData()
        if not v or not self.ed:
            return
        k = v[2:]
        src = (self.preset_data["custom"] if v[0] == "c" else self.preset_data["builtin"]).get(k)
        if not src:
            return
        for f, _ in FAN_LABELS:
            self.ed[f] = self.from_pwm(src[f])
        self.full_redraw()
        self.out.setPlainText("Loaded preset '%s' into the editor. Nothing was applied." % k)

    def on_preset_delete(self):
        v = self.psel.currentData()
        if not v or not str(v).startswith("c:"):
            return
        if self.confirm("Delete preset", "Delete custom preset '%s'?" % v[2:]):
            self.act("delete-preset", {"name": v[2:]})

    def on_preset_save(self):
        if not self.ed:
            return
        n = self.pname.text().strip()
        if not core.NAME_RE.fullmatch(n):
            self.show_error("preset name must be 1-32 characters: letters, digits, space, dash, underscore.")
            return
        if not all(self.check_send(f) for f, _ in FAN_LABELS):
            return
        self.act("save-preset", {"name": n, "curves": {f: self.send_pts(f) for f, _ in FAN_LABELS}})


# ---- single instance, startup ----------------------------------------------------------------
def server_name():
    return "rog-panel-qt-" + getpass.getuser()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="show the commands instead of running them")
    ap.add_argument("--no-single-instance", action="store_true")
    ap.add_argument("--tray", action="store_true", help="start hidden in the system tray (used at login)")
    ap.add_argument("--no-tray", action="store_true", help="no tray icon; closing the window quits")
    a = ap.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ROG Settings Panel")
    app.setDesktopFileName("rog-panel-qt")
    app.setQuitOnLastWindowClosed(False)   # quit_app() quits; the window may live in the tray
    server = None
    if not a.no_single_instance:
        sock = QLocalSocket()
        sock.connectToServer(server_name())
        if sock.waitForConnected(300):
            if a.tray:               # started at login while already running: stay out of the way
                return 0
            sock.write(b"raise\n")
            sock.waitForBytesWritten(300)
            print("ROG Settings Panel is already running; raised it.", flush=True)
            return 0
        QLocalServer.removeServer(server_name())
        server = QLocalServer()
        server.listen(server_name())
    win = MainWindow(dry_run=a.dry_run)
    atexit.register(lambda: win.mon_proc is not None and win.mon_proc.poll() is None and win.mon_proc.kill())
    app.aboutToQuit.connect(win.shutdown)
    # At logout, close requests must quit instead of hiding to the tray.
    app.commitDataRequest.connect(lambda _manager: setattr(win, "quitting", True))
    for sg in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sg, lambda *_: win.quit_app())
    sigtimer = QTimer()          # lets Python run the signal handlers while Qt's loop is busy
    sigtimer.timeout.connect(lambda: None)
    sigtimer.start(300)
    if server:
        def on_conn():
            c = server.nextPendingConnection()
            if c:
                c.readyRead.connect(c.deleteLater)
            win.showNormal()
            win.raise_()
            win.activateWindow()
        server.newConnection.connect(on_conn)
    tries = [0]

    def place():
        available = not a.no_tray and QSystemTrayIcon.isSystemTrayAvailable()
        if not available and a.tray and not a.no_tray and win.prefs.get("tray") and tries[0] < 30:
            tries[0] += 1            # at login the panel can start before the tray host
            QTimer.singleShot(1000, place)
            return
        win.tray_allowed = available
        win.apply_tray()
        if win.tray is None or not a.tray:   # without a tray icon the window is the only way in
            win.show()
    place()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
