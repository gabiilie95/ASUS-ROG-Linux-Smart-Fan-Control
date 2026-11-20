"""Panel settings, start at login and updates, shared by the Settings tab and the tray menu."""
from datetime import datetime
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QObject, QProcess, QSettings, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

import rogcore as core
from fan_auto import heading, label

PANEL = Path(core.UI) / "rog-panel-qt.py"
AUTOSTART_MARK = "X-ROG-Panel-Autostart=true"
UPDATE_FIRST_MS = 60_000              # first check a minute after start, then every 12 hours
UPDATE_EVERY_MS = 12 * 3600_000
OPTIONS = {   # key: (default, label)
    "tray": (True, "Show the tray icon"),
    "close_to_tray": (True, "Keep running in the tray when the window is closed"),
    "notifications": (True, "Show desktop notifications"),
    "start_at_login": (False, "Start at login"),
    "check_updates": (True, "Check for updates every 12 hours"),
    "calibration_setup": (True, "Suggest fan calibration on the Overview until it is done"),
}


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
            "Icon=asus_notif_red\n"
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


class PanelSettings(QObject):
    """One place for the panel's settings; `changed` carries the value actually in effect."""
    changed = Signal(str, bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store = QSettings("rog-fand", "rog-panel-qt")

    def get(self, key):
        if key == "start_at_login":
            return autostart_enabled()
        return self.store.value(key, OPTIONS[key][0], type=bool)

    def set(self, key, on):
        """Raises OSError or ValueError if start at login cannot be changed."""
        try:
            if key == "start_at_login":
                set_autostart(on)
            else:
                self.store.setValue(key, bool(on))
                self.store.sync()
        finally:
            self.changed.emit(key, self.get(key))


# ---- updates -----------------------------------------------------------------------------------
def check_updates():
    """Ask GitHub for the newest release (runs as the desktop user, in a worker thread)."""
    try:
        mod = core.load_updater()
        release = mod.latest()
        installed = mod.installed_version()
        tag = release["tag_name"] if release else None
        return dict(installed=installed, tag=tag, newer=bool(tag and mod.is_newer(tag, installed)))
    except Exception as e:      # network, TLS, JSON or import problems all mean "try again later"
        return dict(error=str(e) or type(e).__name__)


class Updater(QObject):
    """Checks for a newer release and installs it through pkexec (one password dialog)."""
    changed = Signal()
    message = Signal(str, str)
    _result = Signal(object)

    def __init__(self, win, prefs):
        super().__init__(win)
        self.win, self.prefs = win, prefs
        self.tag = self.latest = self.last_check = self.error = self.proc = None
        self.status = ""
        self.checking = self.manual = False
        try:
            self.installed = core.load_updater().installed_version()
        except Exception:
            self.installed = None
        self._result.connect(self._checked)
        self.first = QTimer(self)
        self.first.setSingleShot(True)
        self.first.timeout.connect(self.check)
        self.first.start(UPDATE_FIRST_MS)
        self.timer = QTimer(self)
        self.timer.setInterval(UPDATE_EVERY_MS)
        self.timer.timeout.connect(self.check)
        self.timer.start()

    def calibrating(self):
        return self.win.auto_tab.calibrating or bool(self.win.cal_tab.process)

    def can_install(self):
        return bool(self.tag) and self.proc is None and core.updater_installed() and not self.calibrating()

    def say(self, title, text):
        self.status = f"{title}: {text}"
        self.message.emit(title, text)
        self.changed.emit()

    def check(self, manual=False):
        if self.checking or self.proc is not None or not (manual or self.prefs.get("check_updates")):
            return
        self.checking, self.manual = True, bool(manual)
        threading.Thread(target=lambda: self._result.emit(check_updates()), daemon=True).start()
        self.changed.emit()

    def _checked(self, result):
        self.checking = False
        manual, self.manual = self.manual, False
        self.last_check = time.time()
        self.error = result.get("error")
        if self.error:
            if manual:
                self.say("Update check failed", self.error)
            else:
                self.changed.emit()
            return
        self.installed, self.latest = result["installed"], result["tag"]
        if result["newer"]:
            self.tag = result["tag"]
            if manual or self.prefs.store.value("update_notified", "") != self.tag:
                self.prefs.store.setValue("update_notified", self.tag)
                self.say("Update available", f"{self.tag} is published (installed: {self.installed}). "
                         "Install it from the Settings tab or the tray menu.")
                return
        else:
            self.tag = None
            if manual:
                self.say("Up to date", f"Installed {self.installed or 'version unknown'}; newest release "
                         f"{self.latest or 'none'}.")
                return
        self.changed.emit()

    def install(self):
        tag = self.tag
        if not tag or self.proc is not None or not core.updater_installed():
            return
        if self.calibrating():
            self.say("Update postponed", "Finish or cancel the calibration first.")
            return
        p = self.proc = QProcess(self)
        p.setProcessChannelMode(QProcess.MergedChannels)
        p.finished.connect(lambda code, _status: self._done(tag, code))
        p.errorOccurred.connect(lambda error: self._done(tag, None) if error == QProcess.FailedToStart else None)
        command = core.update_command(tag)
        p.start(command[0], command[1:])
        self.say("Installing " + tag, "A password dialog will appear.")

    def _done(self, tag, code):
        p, self.proc = self.proc, None
        if p is None:
            return
        output = bytes(p.readAll()).decode(errors="replace").strip()
        p.deleteLater()
        if code == 0:
            self.tag = None
            self.installed = tag[1:]
            self.say("Updated to " + tag, "The panel restarts to load the new version.")
            QTimer.singleShot(1500, self.restart_panel)
        elif code in (126, 127):
            self.say("Update not installed", "The password dialog was cancelled.")
        else:
            self.say("Update failed", output.splitlines()[-1] if output else "pkexec could not start")

    def restart_panel(self):
        """Start this panel again once this process has gone, then quit."""
        args = [str(PANEL)] + (["--dry-run"] if self.win.dry_run else []) + ([] if self.win.isVisible() else ["--tray"])
        started, _pid = QProcess.startDetached("/bin/sh", ["-c", 'sleep 2; exec "$0" -IB "$@"', sys.executable, *args])
        if started:
            self.win.quit_app()
        else:
            self.say("Restart the panel", "The update is installed; reopen the panel to load it.")

    def stop(self):
        self.first.stop()
        self.timer.stop()


# ---- Settings tab ------------------------------------------------------------------------------
class SettingsTab(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win, self.prefs, self.updater = win, win.prefs, win.updater
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addWidget(heading("Settings"))
        self.boxes = {}

        tray = QGroupBox("Tray and notifications")
        lay = QVBoxLayout(tray)
        for key in ("tray", "close_to_tray", "notifications", "start_at_login"):
            lay.addWidget(self.option(key))
        self.tray_note = label()
        lay.addWidget(self.tray_note)
        self.message = label()
        lay.addWidget(self.message)
        root.addWidget(tray)

        updates = QGroupBox("Updates")
        lay = QVBoxLayout(updates)
        self.version = label()
        lay.addWidget(self.version)
        lay.addWidget(self.option("check_updates"))
        row = QHBoxLayout()
        self.check_btn = QPushButton("Check for updates now")
        self.check_btn.clicked.connect(lambda: self.updater.check(manual=True))
        row.addWidget(self.check_btn)
        self.install_btn = QPushButton("Install update")
        self.install_btn.setObjectName("primaryAction")
        self.install_btn.clicked.connect(self.updater.install)
        row.addWidget(self.install_btn)
        row.addStretch()
        lay.addLayout(row)
        lay.addWidget(label("Updates come from published releases of the project on GitHub. Installing asks "
                            "for your password (pkexec), then the fan service and this panel restart."))
        self.update_status = label()
        lay.addWidget(self.update_status)
        root.addWidget(updates)

        calibration = QGroupBox("Fan calibration")
        lay = QVBoxLayout(calibration)
        lay.addWidget(self.option("calibration_setup"))
        lay.addWidget(label("The Overview offers a quick or full auto-tune until one has been written. "
                            "Calibrate… on the Overview starts one at any time."))
        root.addWidget(calibration)
        root.addStretch()

        self.prefs.changed.connect(self.pref_changed)
        self.updater.changed.connect(self.refresh)
        self.refresh()

    def option(self, key):
        box = QCheckBox(OPTIONS[key][1])
        box.setChecked(self.prefs.get(key))
        box.toggled.connect(lambda on, k=key: self.set_pref(k, on))
        self.boxes[key] = box
        return box

    def set_pref(self, key, on):
        try:
            self.prefs.set(key, on)
            self.message.clear()
        except (OSError, ValueError) as e:
            self.message.setText(f"{OPTIONS[key][1]}: not changed ({e})")

    def pref_changed(self, key, value):
        box = self.boxes.get(key)
        if box is not None and box.isChecked() != value:
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)
        self.refresh()

    def refresh(self):
        allowed = self.win.tray_allowed
        tray_on = allowed and self.prefs.get("tray")
        self.boxes["tray"].setEnabled(allowed)
        for key in ("close_to_tray", "notifications"):
            self.boxes[key].setEnabled(tray_on)
        login = "Start at login opens the panel " + ("in the tray." if tray_on else "window (the tray icon is off).")
        self.tray_note.setText(("No system tray is available in this session. " if not allowed else
                                "" if tray_on else "Notifications and keep-running need the tray icon. ") + login)
        up = self.updater
        checked = (" · checked " + datetime.fromtimestamp(up.last_check).strftime("%H:%M")) if up.last_check else ""
        latest = up.latest or ("unknown" if not up.last_check else "none")
        self.version.setText(f"Installed {up.installed or 'unknown'} · newest release {latest}{checked}"
                             + (f" · last check failed: {up.error}" if up.error else ""))
        self.check_btn.setEnabled(not up.checking and up.proc is None)
        self.check_btn.setText("Checking…" if up.checking else "Check for updates now")
        self.install_btn.setVisible(bool(up.tag))
        self.install_btn.setEnabled(up.can_install())
        self.install_btn.setText(f"Installing {up.tag}…" if up.proc else f"Install update {up.tag}…")
        status = up.status
        if up.tag and not core.updater_installed():
            status = f"{up.tag} is available. Install the service (sudo service/install.sh install) to update here."
        elif up.tag and up.calibrating():
            status = "Finish or cancel the calibration to install the update."
        self.update_status.setText(status)
