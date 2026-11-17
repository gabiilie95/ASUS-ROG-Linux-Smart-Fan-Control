"""System tray icon and desktop notifications for the ROG settings panel.

The icon shows the rog-fand state with the ROG logos that asusctl installs into the hicolor icon
theme (asus_notif_<colour>). They are loaded by theme name only and never copied into this
repository: the logo is an ASUS trademark.
"""
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from PySide6.QtCore import QObject, QSettings, QTimer
from PySide6.QtGui import QActionGroup, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import rogcore as core

FALLBACK_ICON = "preferences-system"
POLL_MS = 2000
BOOST_SHOW_S = 240     # a boost holds 60 s, then steps back down 5 points every 20 s
NOTIFY_GAP_S = 300     # each kind of notification at most once per 5 minutes
OWN_REQUEST_S = 30     # a mode change this soon after the panel asked for it is the panel's own
BAD_POLLS = 2          # a down service must stay down this many polls before notifying (resume, restart)
BAD_STATES = {
    "Stopped": ("Fan service stopped", "The base fan curve applies until rog-fand starts again."),
    "Stale": ("Fan service not updating", "The last fan floors stay applied. Check the rog-fand journal."),
    "Unreadable": ("Fan service status unreadable", "Check the rog-fand journal."),
}
PANEL = Path(core.UI) / "rog-panel-qt.py"
AUTOSTART_MARK = "X-ROG-Panel-Autostart=true"


# ---- state, no Qt ------------------------------------------------------------------------------
def usb_drops(s):
    """Skipped USB audio packets per second, leaving out stream start-up skips; None if unknown."""
    usb = (s or {}).get("usb") or {}
    if usb.get("error") or not s:
        return None
    total = 0.0
    for d in usb.get("devices", []):
        skipped = d.get("skipped") or 0
        real = skipped - (d.get("warmup_skipped") or 0)
        if skipped > 0 and real > 0:
            total += d["drops_per_s"] * real / skipped
    return round(total, 2)


def event_time(event):
    try:
        return datetime.fromisoformat(event["t"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def classify(state, s, wall, last_boost):
    """Icon colour and a one-line summary, most urgent first."""
    if state in BAD_STATES:
        return "red", BAD_STATES[state][0]
    if state != "Running" or not s:
        return "white", {"Not installed": "Fan service not installed",
                         "Dry run": "Fan service in dry run"}.get(state, "Fan service state unknown")
    drops = usb_drops(s)
    if drops:
        return "red", f"USB audio dropping {drops:g} packets/s"
    if s.get("manual_control"):
        return "blue", "Calibration controls the fans"
    profile = s["sensors"].get("profile")
    if profile != "performance":
        return "blue", f"Idle in the {profile or 'unknown'} profile"
    if s["mode"] == "off":
        return "blue", "Off, base curve only"
    if s.get("degraded"):
        return "orange", "Sensor fallback, fans held at safe floors"
    if s["mode"] != "auto":
        return "blue", core.FAND_MODES[s["mode"]] + " preset"
    if last_boost is not None and wall - last_boost < BOOST_SHOW_S:
        return "yellow", "Boosted after USB drops"
    return "green", "Automatic"


class TrayModel:
    """Turns service polls into the notifications due now."""

    def __init__(self):
        self.started = False
        self.last_running = None
        self.bad_polls = 0
        self.down_sent = False
        self.last_boost = None
        self.sent = {}

    def observe(self, state, s, wall, mono, requested=None):
        """Feed one poll; returns [(kind, title, text)] after rate limiting."""
        due = []
        boosts = [event_time(e) for e in (s or {}).get("events", []) if e.get("message") == "USB drop boost"]
        newest = max((t for t in boosts if t is not None), default=None)
        if newest is not None and (self.last_boost is None or newest > self.last_boost):
            if self.started:
                drops = usb_drops(s)
                rate = f" ({drops:g} packets/s)" if drops else ""
                due.append(("boost", "Fans boosted",
                            f"USB audio skipped packets{rate}. The fans were raised and the floor for this "
                            "load was increased."))
            self.last_boost = newest

        if state in BAD_STATES:
            self.bad_polls += 1
            if self.bad_polls == BAD_POLLS:
                due.append(("service-down", *BAD_STATES[state]))
                self.down_sent = True
        elif state == "Running" and s:
            self.bad_polls = 0
            last = self.last_running
            restarted = (last is not None and isinstance(s.get("elapsed_s"), (int, float))
                         and isinstance(last.get("elapsed_s"), (int, float)) and s["elapsed_s"] < last["elapsed_s"])
            mode = core.FAND_MODES[s["mode"]]
            if self.down_sent:
                due.append(("service-up", "Fan service running", f"rog-fand controls the fans again ({mode})."))
            elif restarted:
                due.append(("service-up", "Fan service restarted", f"rog-fand restarted in {mode} mode."))
            elif last is not None:
                if bool(s.get("degraded")) != bool(last.get("degraded")):
                    if s.get("degraded"):
                        missing = ", ".join(s["unavailable"]) or "a sensor"
                        due.append(("fallback", "Sensor fallback",
                                    f"Missing: {missing}. Fans are held at safe floors until it returns."))
                    else:
                        due.append(("fallback-end", "Sensors back", "Smart fan control resumed."))
                own = requested is not None and requested[0] == s["mode"] and mono - requested[1] < OWN_REQUEST_S
                if s["mode"] != last["mode"] and not own:
                    due.append(("mode", "Fan mode changed", f"Now {mode}."))
            self.down_sent = False
            self.last_running = s
        else:
            self.bad_polls = 0
        self.started = True

        shown = []
        for kind, title, text in due:
            if mono - self.sent.get(kind, -NOTIFY_GAP_S) >= NOTIFY_GAP_S:
                self.sent[kind] = mono
                shown.append((kind, title, text))
        return shown


# ---- start at login ----------------------------------------------------------------------------
def autostart_path():
    base = os.environ.get("XDG_CONFIG_HOME", "")
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "autostart" / "rog-panel-qt.desktop"


def exec_arg(arg):
    """Quote one argument for a desktop entry's Exec key (quoting, then string escaping)."""
    if any(ord(c) < 32 or c == "\x7f" for c in arg):
        raise ValueError("Path contains a control character")
    arg = arg.replace("%", "%%")
    if re.fullmatch(r"[\w@+=:,./-]+", arg):
        return arg
    return ('"' + re.sub(r'([\\"`$])', r"\\\1", arg) + '"').replace("\\", "\\\\")


def autostart_entry(python=sys.executable, panel=PANEL):
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ROG Settings Panel\n"
            "Comment=Fan service status in the system tray\n"
            f"Exec={exec_arg(python)} -IB {exec_arg(str(panel))} --tray\n"
            "Icon=rog-control-center\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
            f"{AUTOSTART_MARK}\n")


def autostart_enabled():
    try:
        return AUTOSTART_MARK in autostart_path().read_text()
    except (OSError, UnicodeDecodeError):
        return False


def set_autostart(enabled):
    path = autostart_path()
    if not enabled:
        if autostart_enabled():          # never remove an entry this panel did not write
            path.unlink()
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=".rog-panel-", delete=False) as out:
        tmp = Path(out.name)
        try:
            out.write(autostart_entry())
            out.flush()
            os.fsync(out.fileno())
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


# ---- Qt ----------------------------------------------------------------------------------------
class FanTray(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.model = TrayModel()
        self.settings = QSettings("rog-fand", "rog-panel-qt")
        self.icons = {}
        self.colour = None
        self.state, self.sample = "", None
        self.hidden_told = False
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.icon("white"))
        self.tray.setToolTip("ROG Settings Panel")
        self.tray.activated.connect(self.on_activated)
        self.tray.messageClicked.connect(self.open_panel)

        menu = self.menu = QMenu(win)
        self.open_act = menu.addAction("Open ROG Settings Panel")
        self.open_act.triggered.connect(self.toggle_panel)
        menu.addSeparator()
        self.mode_menu = menu.addMenu("Fan mode")
        group = QActionGroup(self)
        self.mode_acts = {}
        for key, name in core.FAND_MODES.items():
            act = self.mode_menu.addAction(name)
            act.setCheckable(True)
            group.addAction(act)
            act.triggered.connect(lambda _checked=False, k=key: self.set_mode(k))
            self.mode_acts[key] = act
        self.monitor_act = menu.addAction("Start stall monitor")
        self.monitor_act.triggered.connect(self.toggle_monitor)
        menu.addSeparator()
        self.keep_act = self.option("Keep running in the tray when closed", "close_to_tray")
        self.notify_act = self.option("Show notifications", "notifications")
        self.login_act = menu.addAction("Start in the tray at login")
        self.login_act.setCheckable(True)
        self.login_act.setChecked(autostart_enabled())
        self.login_act.toggled.connect(self.set_login)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(win.quit_app)
        menu.aboutToShow.connect(self.update_menu)
        self.tray.setContextMenu(menu)

        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()
        self.tray.show()

    def option(self, text, key):
        act = self.menu.addAction(text)
        act.setCheckable(True)
        act.setChecked(self.settings.value(key, True, type=bool))
        act.toggled.connect(lambda on: self.settings.setValue(key, on))
        return act

    def icon(self, colour):
        if colour not in self.icons:
            self.icons[colour] = QIcon.fromTheme("asus_notif_" + colour, QIcon.fromTheme(FALLBACK_ICON))
        return self.icons[colour]

    def keep_running(self):
        return self.keep_act.isChecked()

    def can_control(self):
        return (self.state == "Running" and not self.win.dry_run and not self.win.auto_tab.calibrating
                and not (self.sample or {}).get("manual_control") and os.access(core.FAND_RUN / "control", os.W_OK))

    def poll(self):
        state, s = core.fan_service()
        wall, mono = time.time(), time.monotonic()
        due = self.model.observe(state, s, wall, mono, core.last_mode_request)
        self.state, self.sample = state, s
        colour, summary = classify(state, s, wall, self.model.last_boost)
        if colour != self.colour:
            self.colour = colour
            self.tray.setIcon(self.icon(colour))
        self.tray.setToolTip(self.tooltip(summary, s))
        for _kind, title, text in due:
            self.notify(title, text)
        if self.menu.isVisible():
            self.update_menu()

    def tooltip(self, summary, s):
        lines = ["ROG fans: " + summary]
        if s:
            floors = " / ".join(f"{s['floors'][f]:g}" for f in core.FANS)
            sensors = s["sensors"]
            gpu = "n/a" if sensors.get("gpu_w") is None else f"{sensors['gpu_w']:.0f} W"
            board = "n/a" if sensors.get("board_c") is None else f"{sensors['board_c']:.0f} °C"
            lines.append(f"Floors {floors} % (CPU / GPU / middle)  ·  GPU {gpu}  ·  Board {board}")
            usb = s["usb"]
            drops = usb_drops(s)
            lines.append("USB audio: " + (usb.get("error") or (f"{drops:g} drops/s" if usb.get("audio_active")
                                                              else "idle")))
        if self.win.monitor_running():
            rate = self.win.mon_samples[-1][0] if self.win.mon_samples else None
            lines.append("Stall monitor running" + ("" if rate is None else f": {rate:.2f} stalls/s")
                         + " (internal speakers in use)")
        return "\n".join(lines)

    def notify(self, title, text):
        if self.notify_act.isChecked():
            self.tray.showMessage(title, text, self.icon(self.colour or "white"), 8000)

    def update_menu(self):
        visible = self.win.isVisible() and not self.win.isMinimized()
        self.open_act.setText("Hide ROG Settings Panel" if visible else "Open ROG Settings Panel")
        mode = self.sample["mode"] if self.state == "Running" and self.sample else None
        for key, act in self.mode_acts.items():
            act.setChecked(key == mode)
        self.mode_menu.setEnabled(self.can_control())
        self.monitor_act.setText("Stop stall monitor" if self.win.monitor_running() else "Start stall monitor")

    def on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.toggle_panel()

    def toggle_panel(self):
        if self.win.isVisible() and not self.win.isMinimized():
            self.win.hide()
        else:
            self.open_panel()

    def open_panel(self):
        self.win.showNormal()
        self.win.raise_()
        self.win.activateWindow()

    def set_mode(self, key):
        if self.can_control():
            try:
                core.request_fan_mode(key)
            except (ValueError, OSError) as e:
                self.notify("Fan mode not changed", str(e))
        self.update_menu()

    def toggle_monitor(self):
        if self.win.monitor_running():
            self.win.monitor_stop("Stopped.")
        else:
            self.win.monitor_start()
            if not self.win.monitor_running():
                self.notify("Stall monitor not started", self.win.mon_msg.text())
        self.update_menu()

    def set_login(self, on):
        try:
            set_autostart(on)
        except (OSError, ValueError) as e:
            self.notify("Start at login not changed", str(e))
        self.login_act.blockSignals(True)
        self.login_act.setChecked(autostart_enabled())
        self.login_act.blockSignals(False)

    def hidden_notice(self):
        """Tell the user the panel went to the tray: once, or every time the stall monitor runs."""
        if self.win.monitor_running():
            self.notify("Still running in the tray",
                        "The stall monitor keeps the internal speakers in use. Stop it from the tray menu.")
        elif not self.hidden_told:
            self.notify("Still running in the tray", "Open or quit the panel from its tray icon.")
        self.hidden_told = True

    def remove(self):
        self.timer.stop()
        self.tray.hide()
