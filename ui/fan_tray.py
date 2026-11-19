"""System tray icon and desktop notifications for the ROG settings panel.

The icon shows the rog-fand state with the ROG logos that asusctl installs into the hicolor icon
theme (asus_notif_<colour>). They are loaded by theme name only and never copied into this
repository: the logo is an ASUS trademark.
"""
from datetime import datetime
import os
import time

from PySide6.QtCore import QObject, QProcess, QTimer
from PySide6.QtGui import QActionGroup, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

import rogcore as core

FALLBACK_ICON = "preferences-system"
POLL_MS = 2000
BOOST_SHOW_S = 150     # a boost holds 30 s, then steps down (5 points per 20 s while hot)
NOTIFY_GAP_S = 300     # each kind of notification at most once per 5 minutes
OWN_REQUEST_S = 30     # a mode change this soon after the panel asked for it is the panel's own
BAD_POLLS = 2          # a down service must stay down this many polls before notifying (resume, restart)
BAD_STATES = {
    "Stopped": ("Fan service stopped", "The base fan curve applies until rog-fand starts again."),
    "Stale": ("Fan service not updating", "The last fan floors stay applied. Check the rog-fand journal."),
    "Unreadable": ("Fan service status unreadable", "Check the rog-fand journal."),
}
# Settings mirrored from the Settings tab (panel_settings.PanelSettings), in menu order.
MENU_OPTIONS = {"close_to_tray": "Keep running in the tray when closed", "notifications": "Show notifications",
                "check_updates": "Check for updates every 12 hours", "start_at_login": "Start at login"}


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
    if not core.fans_protected(s):
        return "blue", f"Paused: {core.profile_name(s['sensors'].get('profile'))} profile"
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
        self.told_profile = None    # the last profile notice shown: "profile" (paused) or "profile-back"

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
            now = s["sensors"].get("profile")
            if last is not None and now != last["sensors"].get("profile"):
                # rog-fand stands down in a profile it does not protect. A change between two protected
                # profiles needs no notice: the protection stays on.
                was_on, is_on = core.fans_protected(last), core.fans_protected(s)
                if was_on and not is_on:
                    due.append(("profile", "Fan protection paused",
                                f"The power profile is now {core.profile_name(now)}, which rog-fand does not "
                                "protect, so audio may crackle under load."
                                + (" Switch to Performance from the tray menu." if core.performance_protected(s)
                                   else "")))
                elif is_on and not was_on and self.told_profile == "profile":
                    due.append(("profile-back", "Fan protection resumed",
                                f"The power profile is now {core.profile_name(now)}, which rog-fand protects "
                                f"({mode} mode)."))
            self.down_sent = False
            self.last_running = s
        else:
            self.bad_polls = 0
        self.started = True

        shown = []
        for kind, title, text in due:
            # A pause right after a "resumed" notice always shows: the last profile notice must not say all is
            # well while the fans are unprotected. That still allows at most two pause notices per 5 minutes.
            urgent = kind == "profile" and self.told_profile == "profile-back"
            if urgent or mono - self.sent.get(kind, -NOTIFY_GAP_S) >= NOTIFY_GAP_S:
                self.sent[kind] = mono
                shown.append((kind, title, text))
                if kind in ("profile", "profile-back"):
                    self.told_profile = kind
        return shown


# ---- Qt ----------------------------------------------------------------------------------------
class FanTray(QObject):
    """The tray icon. Settings and updates live in the window (win.prefs, win.updater); the menu mirrors them."""

    def __init__(self, win):
        super().__init__(win)
        self.win, self.prefs, self.updater = win, win.prefs, win.updater
        self.model = TrayModel()
        self.icons = {}
        self.colour = None
        self.state, self.sample, self.summary = "", None, ""
        self.hidden_told = self.removed = False
        self.profile_proc = None
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(self.icon("white"))
        self.tray.setToolTip("ROG Settings Panel")
        self.tray.activated.connect(self.on_activated)
        self.tray.messageClicked.connect(self.open_panel)

        menu = self.menu = QMenu(win)
        self.open_act = menu.addAction("Open ROG Settings Panel")
        self.open_act.triggered.connect(self.toggle_panel)
        menu.addSeparator()
        self.profile_act = menu.addAction("Switch to Performance profile")
        self.profile_act.triggered.connect(self.switch_profile)
        self.profile_act.setVisible(False)
        self.mode_menu = menu.addMenu("Fan mode")
        group = QActionGroup(self.mode_menu)
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
        self.install_act = menu.addAction("Install update")
        self.install_act.triggered.connect(self.updater.install)
        self.install_act.setVisible(False)
        self.check_act = menu.addAction("Check for updates now")
        self.check_act.triggered.connect(self.check_now)
        menu.addSeparator()
        self.options = {}
        for key, text in MENU_OPTIONS.items():
            act = menu.addAction(text)
            act.setCheckable(True)
            act.setChecked(self.prefs.get(key))
            act.toggled.connect(lambda on, k=key: self.set_pref(k, on))
            self.options[key] = act
        self.keep_act, self.notify_act = self.options["close_to_tray"], self.options["notifications"]
        self.settings_act = menu.addAction("Settings…")
        self.settings_act.triggered.connect(self.win.show_settings)
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(win.quit_app)
        menu.aboutToShow.connect(self.update_menu)
        self.tray.setContextMenu(menu)

        # Bound methods, so these connections end with the tray when it is switched off.
        self.prefs.changed.connect(self.pref_changed)
        self.updater.changed.connect(self.updater_changed)
        self.updater.message.connect(self.notify)
        self.timer = QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.poll()
        self.tray.show()

    def icon(self, colour):
        if colour not in self.icons:
            self.icons[colour] = QIcon.fromTheme("asus_notif_" + colour, QIcon.fromTheme(FALLBACK_ICON))
        return self.icons[colour]

    def keep_running(self):
        return self.prefs.get("close_to_tray")

    def other_profile(self):
        """rog-fand runs and the platform profile is not Performance, protected or not."""
        return self.state == "Running" and bool(self.sample) and self.sample["sensors"].get("profile") != "performance"

    def can_control(self):
        return (self.state == "Running" and not self.win.dry_run and not self.win.auto_tab.calibrating
                and not (self.sample or {}).get("manual_control") and os.access(core.FAND_RUN / "control", os.W_OK))

    def poll(self):
        state, s = core.fan_service()
        wall, mono = time.time(), time.monotonic()
        due = self.model.observe(state, s, wall, mono, core.last_mode_request)
        self.state, self.sample = state, s
        colour, self.summary = classify(state, s, wall, self.model.last_boost)
        if colour != self.colour:
            self.colour = colour
            self.tray.setIcon(self.icon(colour))
            self.win.setWindowIcon(self.icon(colour))   # the window follows the tray colour
        self.tray.setToolTip(self.tooltip(self.summary, s))
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
        if self.updater.tag:
            lines.append(f"Update {self.updater.tag} available")
        return "\n".join(lines)

    def notify(self, title, text):
        if self.prefs.get("notifications"):
            self.tray.showMessage(title, text, self.icon(self.colour or "white"), 8000)

    def update_menu(self):
        visible = self.win.isVisible() and not self.win.isMinimized()
        self.open_act.setText("Hide ROG Settings Panel" if visible else "Open ROG Settings Panel")
        mode = self.sample["mode"] if self.state == "Running" and self.sample else None
        for key, act in self.mode_acts.items():
            act.setChecked(key == mode)
        self.mode_menu.setEnabled(self.can_control())
        other = self.other_profile()
        self.profile_act.setVisible(other)
        self.profile_act.setEnabled(other and self.profile_proc is None)
        self.profile_act.setText("Switching to Performance…" if self.profile_proc else "Switch to Performance profile")
        self.monitor_act.setText("Stop stall monitor" if self.win.monitor_running() else "Start stall monitor")
        up = self.updater
        self.install_act.setVisible(bool(up.tag))
        if up.tag:
            self.install_act.setEnabled(up.can_install())
            self.install_act.setText(f"Installing {up.tag}…" if up.proc else
                                     f"Install update {up.tag}…" if core.updater_installed() else
                                     f"Update {up.tag} available (install the service first)")
        self.check_act.setEnabled(not up.checking and up.proc is None)

    def set_pref(self, key, on):
        try:
            self.prefs.set(key, on)
        except (OSError, ValueError) as e:
            self.notify(MENU_OPTIONS[key] + ": not changed", str(e))

    def pref_changed(self, key, value):
        act = self.options.get(key)
        if act is not None and act.isChecked() != value:
            act.blockSignals(True)
            act.setChecked(value)
            act.blockSignals(False)

    def updater_changed(self):
        self.update_menu()
        self.tray.setToolTip(self.tooltip(self.summary, self.sample))

    def check_now(self):
        self.updater.check(manual=True)

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

    def switch_profile(self):
        """Set the Performance profile through asusd (no password), without blocking the tray."""
        if self.profile_proc is not None:
            return
        command = core.performance_profile_command()
        if self.win.dry_run:
            self.notify("Profile not changed", "Dry run: " + " ".join(command) + " was not run.")
            return
        p = self.profile_proc = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(lambda code, status: self.profile_done(code if status == QProcess.NormalExit else None))
        p.errorOccurred.connect(lambda error: self.profile_done(None) if error == QProcess.FailedToStart else None)
        p.start(command[0], command[1:])
        self.update_menu()

    def profile_done(self, code):
        p, self.profile_proc = self.profile_proc, None
        if p is None:
            return
        output = bytes(p.readAll()).decode(errors="replace").strip()
        error = p.errorString()
        p.deleteLater()
        if code != 0:
            self.notify("Profile not changed", output.splitlines()[-1] if output else
                        f"asusctl exited with code {code}" if code is not None else "asusctl: " + error)
        self.update_menu()

    def toggle_monitor(self):
        if self.win.monitor_running():
            self.win.monitor_stop("Stopped.")
        else:
            self.win.monitor_start()
            if not self.win.monitor_running():
                self.notify("Stall monitor not started", self.win.mon_msg.text())
        self.update_menu()

    def hidden_notice(self):
        """Tell the user the panel went to the tray: once, or every time the stall monitor runs."""
        if self.win.monitor_running():
            self.notify("Still running in the tray",
                        "The stall monitor keeps the internal speakers in use. Stop it from the tray menu.")
        elif not self.hidden_told:
            self.notify("Still running in the tray", "Open or quit the panel from its tray icon.")
        self.hidden_told = True

    def remove(self):
        """Idempotent: quitting reaches this both from quit_app() and from aboutToQuit."""
        self.timer.stop()
        if self.removed:
            return
        self.removed = True
        for signal, slot in ((self.prefs.changed, self.pref_changed), (self.updater.changed, self.updater_changed),
                             (self.updater.message, self.notify)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self.tray.hide()
        self.menu.deleteLater()
