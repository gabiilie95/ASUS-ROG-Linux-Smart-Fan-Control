"""The Monitor tab's USB audio driver fix: test PLAYBACK_URB_FIXUP on a USB audio interface, then
keep it or not.

Linux 7.3's USB audio driver can queue more audio and let the USB controller send each transfer as
soon as it can (the PLAYBACK_URB_FIXUP quirk flag). That may shorten the dropouts this laptop's hot
chipset causes. The test plays nothing itself: while the user's audio plays, the root helper
(service/rog_usbaudio.py, run through pkexec) counts dropouts without the flag, with it and without
it again, then puts the device's setting back. Keep on writes a modprobe.d file; Turn off removes it.
"""
import json
from html import escape

from PySide6.QtCore import QProcess, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
                               QVBoxLayout)

import rogcore as core
from charts import STATUS

STEPS = 3
LENGTHS = {120: "About 10 minutes", 240: "About 17 minutes"}
VERDICT_COLORS = {"helps": STATUS["good"], "worse": STATUS["hot"]}
INTRO = ("Linux 7.3's USB audio driver has an option, PLAYBACK_URB_FIXUP, that queues more audio and lets "
         "the USB controller send each transfer as soon as it can. A Linux USB developer suggested it for "
         "these dropouts: it may make each one shorter, though a missed packet cannot be sent later. Test it "
         "with your interface before keeping it. Each change reconnects the interface, so its sound pauses "
         "for a second or two.")


def device_choices(st):
    """[(device id, label, info)] for connected USB audio devices, then devices kept on but unplugged."""
    out, seen = [], set()
    for d in st["devices"]:
        seen.add(d["device"])
        out.append((d["device"], "%s · %s%s" % (d["name"], d["device"], "" if d["playback"] is not False
                                                else " (no playback)"), d))
    for entry in st["kept"]:
        device = entry.split(":", 2)
        device = ":".join(device[:2]).lower()
        if len(device) == 9 and device not in seen:
            seen.add(device)
            out.append((device, "%s · not connected" % device, None))
    return out


def describe(st, device, info, module):
    """[(ok: True/False/None, text)] for what the kernel supports and what is set for one device."""
    lines = [({True: True, False: False}.get(st["support"]), st["support_text"])]
    if st["problem"]:
        lines.append((False, st["problem"]))
    if device is None:
        lines.append((None, "No USB audio interface is connected."))
        return lines
    ds = module.device_state(st, device)
    if info is not None and info["playback"] is False:
        lines.append((None, "This device has no playback stream, so there is nothing for the fix to do."))
    if ds["kept"] and ds["on"]:
        lines.append((True, "On, and kept on at every start (%s)." % st["conf"]))
    elif ds["kept"] and not st["loaded"]:
        lines.append((True, "Kept on at every start; it applies when the device is connected."))
    elif ds["kept"]:
        lines.append((None, "Kept on at every start (%s), but the running driver does not have it: restart, "
                            "or use Try until restart." % st["conf"]))
    elif ds["other"]:
        lines.append((None, "On, set outside this panel (the driver's quirk_flags)."))
    elif ds["ours"]:
        lines.append((True, "On until the next restart."))
    else:
        lines.append((None, "Off."))
    if st["conflicts"]:
        lines.append((False, "The driver's quirk_flags is also set in %s, so Keep on is unavailable."
                             % ", ".join(st["conflicts"])))
    return lines


def phase_cells(row, module):
    t = module.totals([row])
    lost = "%s ms/min" % f"{t['lost_ms_per_min']:,.0f}" if t["lost_ms_per_min"] is not None else "—"
    events = "%s/min" % f"{t['events_per_min']:,.0f}" if t["events_per_min"] is not None else "—"
    length = "%.2f ms" % t["ms_per_event"] if t["ms_per_event"] is not None else "—"
    board = "%.0f °C" % t["board_c"] if t["board_c"] is not None else "—"
    return [str(row["index"]), "On" if row["fix"] else "Off", "%.0f min" % (row["seconds"] / 60), lost, events,
            length, board]


def set_primary(button, on):
    button.setObjectName("primaryAction" if on else "")
    button.style().unpolish(button)
    button.style().polish(button)


class UsbFixCard(QGroupBox):
    runningChanged = Signal(bool)

    def __init__(self, dry_run=False, blocked=lambda: None):
        super().__init__("USB audio driver fix")
        self.dry_run = dry_run
        self.blocked = blocked       # () -> reason another panel action must finish first, or None
        self.st = None
        self.module = None
        self.process = None          # the test
        self.action = None           # try, keep or off
        self.action_name = None
        self.out = b""
        self.err = b""
        self.events = 0
        self.stopping = False
        self.restore_mode = None
        self.total_s = None
        self.name = None
        self.rows = []

        lay = QVBoxLayout(self)
        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        lay.addWidget(intro)
        row = QHBoxLayout()
        row.addWidget(QLabel("Interface:"))
        self.device = QComboBox()
        self.device.setMinimumContentsLength(28)
        self.device.currentIndexChanged.connect(self.redraw)
        row.addWidget(self.device, 1)
        lay.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)

        row = QHBoxLayout()
        self.test = QPushButton("Run test")
        self.test.setObjectName("primaryAction")
        self.test.clicked.connect(self.test_clicked)
        row.addWidget(self.test)
        self.length = QComboBox()
        for seconds, text in LENGTHS.items():
            self.length.addItem(text, seconds)
        row.addWidget(self.length)
        row.addSpacing(12)
        self.try_btn = QPushButton("Try until restart")
        self.try_btn.clicked.connect(lambda: self.run_action("try"))
        self.keep = QPushButton("Keep on")
        self.keep.clicked.connect(lambda: self.run_action("keep"))
        self.off = QPushButton("Turn off")
        self.off.clicked.connect(lambda: self.run_action("off"))
        for b in (self.try_btn, self.keep, self.off):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)

        self.phase = QLabel()
        self.phase.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setMaximumHeight(10)
        lay.addWidget(self.phase)
        lay.addWidget(self.bar)
        self.verdict = QLabel()
        self.verdict.setWordWrap(True)
        lay.addWidget(self.verdict)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Step", "Fix", "Measured", "Lost audio", "Dropouts",
                                              "Average length", "Board"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        lay.addWidget(self.table)
        self.message = QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        for w in (self.phase, self.bar, self.verdict, self.table, self.message):
            w.hide()

    # -- state ----------------------------------------------------------------------------------
    @property
    def running(self):
        return self.process is not None

    def busy(self):
        return self.process is not None or self.action is not None

    def showEvent(self, event):
        super().showEvent(event)
        if not self.busy():
            self.refresh()

    def refresh(self):
        try:
            self.module = core.usbaudio_module()
            self.st = self.module.state()
        except (OSError, ImportError, SyntaxError, AttributeError, ValueError) as e:
            self.st = None
            self.status.setText("Cannot read the USB audio driver's settings: %s" % escape(str(e)))
            self.update_buttons()
            return
        current = self.device.currentData()
        choices = device_choices(self.st)
        self.device.blockSignals(True)
        self.device.clear()
        for device, text, info in choices:
            self.device.addItem(text, device)
        ids = [c[0] for c in choices]
        if current in ids:
            self.device.setCurrentIndex(ids.index(current))
        else:
            playing = [i for i, c in enumerate(choices) if c[2] and c[2]["playback"]]
            self.device.setCurrentIndex(playing[0] if playing else 0 if choices else -1)
        self.device.blockSignals(False)
        self.redraw()

    def selected(self):
        """(device id, device info or None) for the chosen interface, or (None, None)."""
        device = self.device.currentData()
        if not device or self.st is None:
            return None, None
        info = next((d for d in self.st["devices"] if d["device"] == device), None)
        return device, info

    def redraw(self):
        if self.st is None:
            self.update_buttons()
            return
        device, info = self.selected()
        marks = {True: ("✓", STATUS["good"]), False: ("✗", STATUS["hot"]), None: ("•", None)}
        html = []
        for ok, text in describe(self.st, device, info, self.module):
            mark, color = marks[ok]
            style = ' style="color:%s"' % color if color else ""
            html.append("<span%s>%s</span> %s" % (style, mark, escape(text, quote=False)))
        self.status.setText("<br>".join(html))
        self.update_buttons()

    def update_buttons(self):
        device, info = self.selected()
        st, busy = self.st, self.busy()
        ds = self.module.device_state(st, device) if st and device else None
        supported = bool(st) and st["support"] is not False and not st["problem"]
        connected = info is not None
        self.test.setText("Stop test" if self.running else "Run test")
        self.test.setEnabled(not self.stopping if self.running else
                             not busy and supported and connected and info["playback"] is not False
                             and st["loaded"] and not ds["other"])
        self.length.setEnabled(not busy)
        self.device.setEnabled(not busy)
        self.try_btn.setEnabled(not busy and supported and connected and st["loaded"] and not ds["on"])
        self.keep.setEnabled(not busy and supported and device is not None and not ds["kept"]
                             and not st["conflicts"])
        self.off.setEnabled(not busy and bool(ds) and (ds["ours"] or ds["kept"]))

    def say(self, text):
        self.message.setText(text)
        self.message.setVisible(bool(text))

    def command(self, action, device, phase_s=None):
        """The pkexec argv, or None after saying why it cannot run here."""
        reason = self.blocked()
        if reason:
            self.say(reason)
            return None
        argv = core.usbaudio_command(action, device, phase_s)
        if self.dry_run:
            self.say("Dry run · " + " ".join(argv))
            return None
        if not core.root_owned(core.USBAUDIO_INSTALLED):
            self.say("The installed fan service has no USB audio helper yet. Install this version "
                     "(service/install.sh, or Install update in Settings), then try again.")
            return None
        return argv

    # -- try, keep, off -------------------------------------------------------------------------
    def run_action(self, name):
        device, _info = self.selected()
        if device is None or self.busy():
            return
        argv = self.command(name, device)
        if argv is None:
            return
        self.action, self.action_name = QProcess(self), name
        self.action.finished.connect(self.action_done)
        self.action.errorOccurred.connect(
            lambda e: self.action_done(127, None, self.action.errorString())
            if self.action and e == QProcess.FailedToStart else None)
        self.say("Waiting for the password dialog…")
        self.update_buttons()
        self.action.start(argv[0], argv[1:])

    def action_done(self, code, _status=None, failure=None):
        if not self.action:
            return
        out = failure or (bytes(self.action.readAllStandardOutput()) +
                          bytes(self.action.readAllStandardError())).decode(errors="replace").strip()
        self.action.deleteLater()
        self.action = None
        if code == 0:
            self.say(out or "Done.")
        elif code in (126, 127) and not out:
            self.say("Nothing changed: the password dialog was dismissed.")
        else:
            self.say("Nothing changed: " + (out.removeprefix("error: ")[-400:] or "exit code %d" % code))
        self.refresh()

    # -- the test -------------------------------------------------------------------------------
    def test_clicked(self):
        if self.running:
            self.stop()
        else:
            self.start_test()

    def fan_mode_to_switch(self):
        """The fan service's mode when the test should switch it to Off (it raises the fans when audio
        drops, which would hide the difference), else None."""
        state, s = core.fan_service()
        return s["mode"] if state == "Running" and s and s["mode"] != "off" else None

    def start_test(self):
        device, info = self.selected()
        if device is None or self.busy():
            return
        phase_s = self.length.currentData()
        argv = self.command("test", device, phase_s)
        if argv is None:
            return
        mode = self.fan_mode_to_switch()
        name = info["name"] if info else device
        text = ("For %s, play audio through %s without pausing, ideally while the laptop is hot enough to drop "
                "audio (a game running). The test counts dropouts without the fix, with it, and without it again.\n\n"
                "%s reconnects twice, so its sound pauses for a second or two each time, and its setting is "
                "put back at the end. %sA password dialog follows."
                % (LENGTHS[phase_s].lower(), name, name,
                   "The fan service switches to Off for the test, so it does not speed the fans up when audio "
                   "drops, and back to %s afterwards. " % core.FAND_MODES.get(mode, mode) if mode else ""))
        if QMessageBox.question(self, "Test the USB audio fix?", text) != QMessageBox.Yes:
            return
        if mode:
            try:
                core.request_fan_mode("off")
            except (ValueError, OSError) as e:
                self.say("Could not set the fan service to Off: %s" % e)
                return
            self.restore_mode = mode
        self.begin(argv, name)

    def begin(self, argv, name):
        self.process = QProcess(self)
        self.out, self.err, self.events, self.stopping = b"", b"", 0, False
        self.name, self.total_s, self.rows = name, None, []
        self.process.readyReadStandardOutput.connect(self.read_events)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.test_done)
        self.process.errorOccurred.connect(
            lambda e: self.test_done(127, None, self.process.errorString())
            if self.process and e == QProcess.FailedToStart else None)
        self.table.setRowCount(0)
        for w in (self.table, self.verdict):
            w.hide()
        self.phase.setText("Waiting for the password dialog…")
        self.bar.setValue(0)
        for w in (self.phase, self.bar):
            w.show()
        self.say("")
        set_primary(self.keep, False)
        set_primary(self.test, True)
        self.update_buttons()
        self.runningChanged.emit(True)
        self.process.start(argv[0], argv[1:])

    def stop(self):
        """Ask the helper to finish: it puts the device's setting back first."""
        if not self.process or self.stopping:
            return
        self.stopping = True
        self.phase.setText("Stopping: putting %s back as it was…" % self.name)
        self.process.write(b"stop\n")
        self.process.closeWriteChannel()
        self.update_buttons()

    def shutdown(self):
        if self.process:
            self.process.closeWriteChannel()     # end of input: the helper restores and exits

    def read_errors(self):
        if self.process:
            self.err = (self.err + bytes(self.process.readAllStandardError()))[-4096:]

    def read_events(self):
        if not self.process:
            return
        self.out += bytes(self.process.readAllStandardOutput())
        *lines, self.out = self.out.split(b"\n")
        for line in lines:
            if line.strip():
                try:
                    self.consume(json.loads(line))
                except (ValueError, TypeError, KeyError):
                    self.say("Invalid progress from the USB audio helper.")

    def consume(self, event):
        self.events += 1
        kind = event["kind"]
        fix = lambda e: "fix on" if e["fix"] else "fix off"
        if kind == "start":
            self.total_s, self.name = event["total_s"], event["name"]
            self.phase.setText("Starting…")
        elif kind == "phase-start":
            self.phase.setText("Step %d of %d · %s · %s" % (
                event["index"], STEPS, fix(event),
                "reconnecting and letting the sound settle" if event["index"] > 1 else "letting the sound settle"))
            self.progress(event.get("elapsed"))
        elif kind == "progress":
            left = max(0, event["seconds"] - event["phase_elapsed"])
            self.phase.setText("Step %d of %d · %s · %d:%02d left in this step · %s dropouts so far" % (
                event["index"], STEPS, fix(event), left // 60, left % 60, f"{event['events']:,}"))
            self.progress(event["elapsed"])
        elif kind == "phase":
            self.rows.append(event)
            self.fill_table(self.rows)
        elif kind == "result":
            self.rows = event["phases"]
            self.fill_table(self.rows)
            self.show_verdict(event)
        elif kind == "cancelled":
            self.say("Test stopped. %s is set as it was before the test." % self.name)
        elif kind == "error":
            self.say("The test could not run: %s" % event["message"])

    def progress(self, elapsed):
        if self.total_s and elapsed is not None:
            self.bar.setValue(min(990, int(1000 * elapsed / self.total_s)))

    def fill_table(self, rows):
        module = self.module or core.usbaudio_module()
        self.table.setRowCount(0)
        for row in rows:
            r = self.table.rowCount()
            self.table.insertRow(r)
            for c, text in enumerate(phase_cells(row, module)):
                self.table.setItem(r, c, QTableWidgetItem(text))
        frame = 2 * self.table.frameWidth()
        self.table.setFixedHeight(self.table.horizontalHeader().sizeHint().height() + frame
                                  + sum(self.table.rowHeight(r) for r in range(self.table.rowCount())))
        self.table.setVisible(bool(rows))

    def show_verdict(self, result):
        color = VERDICT_COLORS.get(result["verdict"], "#388ab9")
        self.verdict.setStyleSheet("border: 0; border-left: 3px solid %s; padding: 4px 8px;" % color)
        self.verdict.setText(result["text"])
        self.verdict.show()
        helps = result["verdict"] == "helps"
        set_primary(self.test, not helps)
        set_primary(self.keep, helps)

    def test_done(self, code, _status=None, failure=None):
        if not self.process:
            return
        self.read_events()
        self.read_errors()
        err = failure or self.err.decode(errors="replace").strip()
        self.process.deleteLater()
        self.process = None
        self.stopping = False
        self.phase.hide()
        self.bar.hide()
        if code in (126, 127) and not self.events:
            self.say("The test did not start: the password dialog was dismissed.")
        elif code != 0 and not self.message.text():
            self.say("The test ended with an error: %s" % (err[-400:] or "exit code %d" % code))
        self.give_back_mode()
        self.runningChanged.emit(False)
        self.refresh()

    def give_back_mode(self):
        mode, self.restore_mode = self.restore_mode, None
        if mode:
            try:
                core.request_fan_mode(mode)
            except (ValueError, OSError) as e:
                self.say(self.message.text() + " Set the fan service back to %s on the Overview (%s)."
                         % (core.FAND_MODES.get(mode, mode), e))
