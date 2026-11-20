"""User-session controls for tools/fan-calibrate.py and tools/autotune.py."""
import json
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtCore import QProcess, QSaveFile, QIODevice, QTimer, Signal, Qt
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
import rogcore as core
from fan_auto import heading, label

QUICK_AUTOTUNE = "Quick auto-tune (about 30-50 min, loud)"
AUTOTUNE = "Full auto-tune (about 60-120 min, loud)"
AUTOTUNES = {QUICK_AUTOTUNE: "quick", AUTOTUNE: "full"}
MINUTES = {"quick": "30-50", "full": "60-120"}
OFF_WAIT_S = 20     # how long to wait for the fan service to acknowledge Off before a setup run
SAFETY_EXIT = 3     # autotune.py stopped at a safety limit; the reason is in its progress
STEP_COLUMNS = ["Step", "Samples", "Mean stalls/s", "Peak stalls/s", "GPU power (W)"]
AUTOTUNE_COLUMNS = STEP_COLUMNS + ["USB drops", "Verdict"]
CALIBRATE_COLUMNS = STEP_COLUMNS + ["Thermal response", "Assessment"]
NOISE_COLUMN = "Noise dB(A)"
TABLE_COLUMNS = ["From GPU W", "CPU %", "GPU %", "Middle %"]


def table_widget(columns):
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setAlternatingRowColors(True)
    return table


def fill_fan_table(widget, rows):
    widget.setRowCount(0)
    for row in rows or []:
        r = widget.rowCount()
        widget.insertRow(r)
        for col, value in enumerate([row["min_gpu_w"], *row["floors"]]):
            widget.setItem(r, col, QTableWidgetItem("%g" % value))


class CalibrateTab(QWidget):
    runningChanged = Signal(bool)
    proposalChanged = Signal(bool)     # an auto-tune proposal is waiting for review

    def __init__(self, dry_run, action_in_progress):
        super().__init__()
        self.dry_run = dry_run
        self.action_in_progress = action_in_progress
        self.process = self.workspace = self.output_path = None
        self.writer = None
        self.proposal = None
        self.autotune = False
        self.autotune_kind = "full"
        self.proposal_preset = None
        self.restore_mode = None       # the fan mode to give back after a setup run switched to Off
        self.mode_deadline = 0
        self.mode_timer = QTimer(self)
        self.mode_timer.setInterval(500)
        self.mode_timer.timeout.connect(self.wait_for_off)
        self.noise = False
        self.index = self.total = 0
        self.step_duration = self.elapsed = 0
        self.outcome = ""
        self.stderr = ""
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Fan calibration"))
        layout.addWidget(label("Measure thermal response and internal audio stalls under a steady workload "
                               "(temperatures alone do not prove clean audio), or let auto-tune generate "
                               "the heat and find the quietest clean fan mix."))
        setup = QGroupBox("Session")
        form = QFormLayout(setup)
        self.preset = QComboBox()
        self.preset.addItems(["Observe", "Light", "Heavy", "Custom", QUICK_AUTOTUNE, AUTOTUNE])
        form.addRow("Run type", self.preset)
        self.seconds = QSpinBox()
        self.seconds.setRange(2, 86400)
        self.seconds.setSuffix(" s")
        form.addRow("Duration per step", self.seconds)
        self.custom = QLineEdit("cpu=80,gpu=80,mid=80;mid=70")
        self.custom.setAccessibleName("Custom fan steps")
        form.addRow("Fan steps", self.custom)
        self.description = label()
        form.addRow(self.description)
        mic_row = QHBoxLayout()
        self.mic = QComboBox()
        self.mic.setAccessibleName("Microphone for noise measurement")
        mic_row.addWidget(self.mic, 1)
        self.mic_refresh = QPushButton("Refresh")
        self.mic_refresh.clicked.connect(self.load_mics)
        mic_row.addWidget(self.mic_refresh)
        form.addRow("Microphone (optional)", mic_row)
        self.mic_note = label("Measures fan noise as one A-weighted level per second; no audio is "
                              "recorded or saved. Place the microphone where you sit and keep the room "
                              "quiet. Auto-tune then keeps a quieter fan mix only if it measures quieter.")
        self.mic_note.setEnabled(False)
        form.addRow(self.mic_note)
        self.load_mics()
        self.preset.currentTextChanged.connect(self.preset_changed)
        self.form = form
        self.preset_changed(self.preset.currentText())
        layout.addWidget(setup)
        row = QHBoxLayout()
        self.start = QPushButton("Preview command" if dry_run else "Start session")
        self.start.setObjectName("primaryAction")
        self.cancel = QPushButton("Cancel session")
        self.cancel.setEnabled(False)
        self.export = QPushButton("Export CSV…")
        self.export.setEnabled(False)
        for btn, slot in ((self.start, self.start_clicked), (self.cancel, self.stop), (self.export, self.save)):
            row.addWidget(btn)
            btn.clicked.connect(slot)
        row.addStretch()
        layout.addLayout(row)
        self.progress = QProgressBar()
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.message = label("Ready")
        layout.addWidget(self.message)

        self.review = QGroupBox("Review the proposed fan table")
        review = QVBoxLayout(self.review)
        self.review_note = label()
        review.addWidget(self.review_note)
        side = QHBoxLayout()
        self.current_box = QGroupBox("Current")
        self.current_table = table_widget(TABLE_COLUMNS)
        QVBoxLayout(self.current_box).addWidget(self.current_table)
        self.proposed_box = QGroupBox("Proposed by auto-tune")
        self.proposed_table = table_widget(TABLE_COLUMNS)
        QVBoxLayout(self.proposed_box).addWidget(self.proposed_table)
        side.addWidget(self.current_box)
        side.addWidget(self.proposed_box)
        review.addLayout(side)
        buttons = QHBoxLayout()
        self.write_model = QPushButton("Preview write command" if dry_run else "Write model…")
        self.write_model.setObjectName("primaryAction")
        self.write_model.clicked.connect(self.write)
        self.discard = QPushButton("Discard")
        self.discard.clicked.connect(self.discard_proposal)
        buttons.addWidget(self.write_model)
        buttons.addWidget(self.discard)
        buttons.addStretch()
        review.addLayout(buttons)
        self.review_status = label()
        review.addWidget(self.review_status)
        self.review.setVisible(False)
        layout.addWidget(self.review)

        self.results = table_widget(CALIBRATE_COLUMNS)
        self.results.setAccessibleName("Calibration results with thermal response and audio assessment")
        layout.addWidget(self.results, 1)

    def preset_changed(self, preset):
        auto = preset in AUTOTUNES
        self.form.setRowVisible(self.custom, preset == "Custom")
        self.form.setRowVisible(self.seconds, not auto)
        if not auto:
            self.seconds.setValue({"Light": 180, "Heavy": 240, "Observe": 120, "Custom": 180}[preset])
        self.description.setText({
            "Observe": "Record the current curves and stall rate. Fan settings stay in place.",
            "Light": "Seven measured steps for light gaming, including middle-fan and assisted cooling.",
            "Heavy": "Four heavy-load steps, starting with all fans at full speed.",
            "Custom": "Separate steps with a semicolon. Omitted fans follow their saved curves.",
            QUICK_AUTOTUNE: "Unattended, like the first hand calibration: heats the GPU (40, 80 and 150 W) "
                            "and CPU. At each level the fans start as quiet as they go until the board has warmed "
                            "up, then rise in coarse steps until audio is clean. About 30-50 minutes with loud "
                            "fans; the laptop speakers stay blocked. Needs Performance, the fan service set to "
                            "Off and the power adapter. Nothing is written until you review the proposed table.",
            AUTOTUNE: "Unattended: heats the GPU (40, 80, 120 and 160 W) and CPU. At each level the fans "
                      "start as quiet as they go until the board has warmed up, then rise step by step until "
                      "audio is clean, then it tries moving air away from the middle fan. About 60-120 "
                      "minutes with loud fans; the laptop speakers stay blocked. Needs Performance, the fan "
                      "service set to Off and the power adapter. Nothing is written until you review the "
                      "proposed table.",
        }[preset])
        if auto:
            self.description.setText(self.description.text() + (
                " USB audio drops on the %s are counted too when it is connected." % core.AUTOTUNE_USB))
        else:
            self.description.setText(self.description.text() + (" Thermal trends need a stable workload."
                if preset == "Observe" else " Keep the workload steady. A step may extend by up to 120 s to check thermal settling."))

    def load_mics(self):
        """Off, then the PipeWire inputs; built-in ones are shown but cannot be chosen."""
        current = self.mic.currentData() if self.mic.count() else None
        self.mic.clear()
        self.mic.addItem("Off", None)
        for source in core.mic_sources():
            label_text = source["description"]
            if not source["usable"]:
                label_text += " (built-in: shares the stall counter's sound chip)"
            self.mic.addItem(label_text, source["name"])
            if not source["usable"]:
                item = self.mic.model().item(self.mic.count() - 1)
                item.setEnabled(False)
        index = self.mic.findData(current)
        self.mic.setCurrentIndex(index if index >= 0 else 0)

    def confirm_autotune(self, kind, switch_mode=False):
        return QMessageBox.question(
            self, "Start auto-tune?",
            "Auto-tune heats the laptop and runs the fans loud for about %s minutes. The laptop speakers "
            "stay blocked meanwhile. %sYou can cancel at any time; the saved fan curves are restored at the "
            "end." % (MINUTES[kind], "The fan service switches to Off for the run and back afterwards. "
                      if switch_mode else "")) == QMessageBox.Yes

    def service_sample_to_switch(self):
        """The running fan service's sample when it still owns the fans (it must hand them over by
        switching to Off before a run sets them), else None."""
        if self.dry_run:
            return None
        state, s = core.fan_service()
        if state == "Running" and s is not None and (s["mode"] != "off" or core.manual_fan_problem() is not None):
            return s
        return None

    def start_clicked(self):
        """Start session: auto-tune and stepped runs switch the fan service to Off themselves (after
        asking) and give its mode back afterwards, as the Overview's setup buttons do."""
        preset = self.preset.currentText()
        if preset in AUTOTUNES:
            self.start_autotune(AUTOTUNES[preset])
            return
        if self.process or self.mode_timer.isActive():
            return
        s = None if preset == "Observe" else self.service_sample_to_switch()
        if s is None:
            self.begin()
            return
        if self.action_in_progress():
            self.message.setText("Wait for the current panel action to finish.")
            return
        answer = QMessageBox.question(
            self, "Set the fan service to Off?",
            "This calibration sets the fans itself, so the fan service switches to Off for the run and "
            "back to %s afterwards." % core.FAND_MODES.get(s["mode"], s["mode"]))
        if answer == QMessageBox.Yes:
            self.switch_off_then_begin(s)

    def start_autotune(self, kind):
        """The Overview's setup buttons and Start session: choose the run, switch the fan service to
        Off if it is not, then start once the service has handed the fans over."""
        if self.process or self.mode_timer.isActive():
            return
        self.preset.setCurrentText(QUICK_AUTOTUNE if kind == "quick" else AUTOTUNE)
        s = self.service_sample_to_switch()
        if s is None:
            self.begin()
            return
        if self.action_in_progress():
            self.message.setText("Wait for the current panel action to finish.")
            return
        if self.confirm_autotune(kind, switch_mode=True):
            self.switch_off_then_begin(s)

    def switch_off_then_begin(self, s):
        previous = s["mode"]
        try:
            core.request_fan_mode("off")
        except (ValueError, OSError) as e:
            self.message.setText("Could not set the fan service to Off: %s" % e)
            return
        self.restore_mode = previous if previous != "off" else None
        self.mode_deadline = time.monotonic() + OFF_WAIT_S
        self.message.setText("Setting the fan service to Off for the run…")
        self.mode_timer.start()

    def wait_for_off(self):
        if core.manual_fan_problem() is None:
            self.mode_timer.stop()
            if not self.begin(confirmed=True):
                self.give_back_mode()
        elif time.monotonic() > self.mode_deadline:
            self.mode_timer.stop()
            self.message.setText("The fan service did not switch to Off, so the run was not started.")
            self.give_back_mode()

    def give_back_mode(self):
        mode, self.restore_mode = self.restore_mode, None
        if mode:
            try:
                core.request_fan_mode(mode)
            except (ValueError, OSError) as e:
                self.message.setText(self.message.text() + " Set %s again in the Overview (%s)."
                                     % (core.FAND_MODES.get(mode, mode), e))

    def begin(self, confirmed=False):
        """Start the chosen run; True when it started."""
        if self.process:
            return False
        if self.action_in_progress():
            self.message.setText("Wait for the current panel action to finish.")
            return False
        preset = self.preset.currentText()
        auto = preset in AUTOTUNES
        kind = AUTOTUNES.get(preset, "full")
        new_workspace = None
        try:
            if auto:
                output = core.log_dir() / time.strftime("autotune-%Y%m%d-%H%M%S.csv")
                usb = core.usb_audio_card()
                args = core.autotune_args(output, self.mic.currentData(), preset=kind, usb=usb)
            else:
                args = core.calibration_args(preset, self.seconds.value(), self.custom.text(), "samples.csv",
                                             self.mic.currentData())
            if self.dry_run:
                self.message.setText("Dry run · " + " ".join(args))
                return False
            if core.sound_device_busy():
                self.message.setText("The internal sound device is busy. Stop playback or the stall monitor first.")
                return False
            if preset != "Observe":
                problem = core.manual_fan_problem()
                if problem:
                    self.message.setText(problem)
                    return False
            if auto:
                if usb and core.fan_service()[0] != "Running":
                    self.message.setText("Auto-tune counts USB drops through the fan service. Start the service first.")
                    return False
                if not confirmed and not self.confirm_autotune(kind):
                    return False
                output.parent.mkdir(parents=True, exist_ok=True)
            else:
                new_workspace = tempfile.TemporaryDirectory(prefix="rog-fan-cal-")
        except (ValueError, OSError) as e:
            self.message.setText(str(e))
            return False
        if self.workspace:
            self.workspace.cleanup()
        self.workspace = new_workspace
        if auto:
            self.output_path = output
        else:
            self.output_path = Path(new_workspace.name) / "samples.csv"
            args = core.calibration_args(preset, self.seconds.value(), self.custom.text(), self.output_path,
                                         self.mic.currentData())
        self.autotune = auto
        self.autotune_kind = kind
        self.noise = bool(self.mic.currentData())
        self.discard_proposal()
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.stderr = self.outcome = ""
        self.index = self.total = 0
        self.results.setColumnCount(0)
        columns = (AUTOTUNE_COLUMNS if auto else CALIBRATE_COLUMNS) + ([NOISE_COLUMN] if self.noise else [])
        self.results.setColumnCount(len(columns))
        self.results.setHorizontalHeaderLabels(columns)
        self.results.setRowCount(0)
        self.progress.setValue(0)
        self.set_running(True)
        self.message.setText("Building the load generator and starting the counters…" if auto
                             else "Starting the stall counter…")
        self.process.start(sys.executable, args)
        return True

    def set_running(self, running):
        for w in (self.preset, self.seconds, self.custom, self.start, self.mic, self.mic_refresh):
            w.setEnabled(not running)
        self.cancel.setEnabled(running)
        self.export.setEnabled(not running and bool(self.output_path and self.output_path.is_file()))
        self.runningChanged.emit(running)

    def read_errors(self):
        if self.process:
            self.stderr = (self.stderr + bytes(self.process.readAllStandardError()).decode(errors="replace"))[-4096:]

    def read_progress(self):
        while self.process and self.process.canReadLine():
            try:
                self.consume(json.loads(bytes(self.process.readLine())))
            except (ValueError, TypeError, KeyError):
                self.message.setText("Invalid progress from calibration tool")

    def consume(self, event):
        kind = event["kind"]
        if kind == "step" and "total" in event:
            self.index, self.total = event["index"], event["total"]
            self.step_duration, self.elapsed = event.get("duration_s", self.seconds.value()), 0
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind in ("step", "level", "level-result"):
            self.message.setText(event["message"])
        elif kind == "sample" and "progress" in event:
            self.progress.setValue(round(100 * min(1, max(0, event["progress"]))))
            self.message.setText(f"{event['message']}  ·  {event['run_s'] / 60:.0f} min")
        elif kind == "sample" and self.total:
            self.elapsed = event["elapsed"]
            self.step_duration = event.get("duration_s", self.step_duration or self.seconds.value())
            self.update_progress()
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind == "extend":
            self.step_duration = event["duration_s"]
            self.update_progress()
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind == "result":
            row = self.results.rowCount()
            self.results.insertRow(row)
            keys = ["name", "samples", "mean", "peak", "watts"]
            if self.autotune:
                keys += ["usb_drops", "stalled"]
            for col, key in enumerate(keys):
                v = event[key]
                text = ("stalls" if v else "clean") if key == "stalled" else \
                    "—" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)
                self.results.setItem(row, col, QTableWidgetItem(text))
            if not self.autotune:      # auto-tune steps carry their own verdict columns
                self.show_analysis(row, event)
            if self.noise:
                db = event.get("noise_db")
                self.results.setItem(row, self.results.columnCount() - 1,
                                     QTableWidgetItem("—" if db is None else f"{db:.1f}"))
        elif kind == "proposal":
            self.show_proposal(event["table"], event.get("path"))
        elif kind in ("done", "restore", "safety"):
            self.outcome = (self.outcome + " " + event["message"]).strip()
            self.message.setText(self.outcome)
            if kind == "done" and not event["cancelled"] and not event.get("safety"):
                self.progress.setValue(100)

    def update_progress(self):
        if self.total and self.step_duration > 0:
            fraction = min(1, self.elapsed / self.step_duration)
            self.progress.setValue(round(100 * (self.index - 1 + fraction) / self.total))

    def show_analysis(self, row, event):
        analysis = event.get("analysis")
        thermal, assessment, details = "Not analyzed", "Not analyzed", []
        if analysis:
            sensors = analysis.get("sensors", {})
            board = sensors.get("board_c", {})
            slope, onset = board.get("slope_c_min"), board.get("cooling_onset_s")
            thermal = f"{slope:+.2f} °C/min" if slope is not None else "Board unavailable"
            if onset is not None:
                thermal += f" · cooling ~{onset:.0f} s"
            for key, name in (("board_c", "Board"), ("cpu_c", "CPU"), ("gpu_c", "GPU")):
                sensor = sensors.get(key, {})
                slope, onset = sensor.get("slope_c_min"), sensor.get("cooling_onset_s")
                details.append(f"{name}: {sensor.get('trend', 'unavailable')}; slope "
                               + (f"{slope:+.2f} °C/min" if slope is not None else "unavailable")
                               + "; estimated cooling onset " + (f"{onset:.0f} s" if onset is not None else "not detected"))
                onset_temp = sensor.get("cooling_onset_c")
                if onset is not None and onset_temp is not None:
                    details[-1] += f" at approximately {onset_temp:.1f} °C"
            settled = analysis.get("rpm_settled_s")
            details.append("RPM stabilization: " + (f"{settled:.0f} s" if settled is not None else "not confirmed"))
            details.append("Stable load: " + {True: "yes", False: "no", None: "unknown"}[analysis.get("load_stable")])
            assessment = analysis.get("reason") or analysis.get("status", "Not analyzed")
            details.append("Assessment: " + analysis.get("status", "unknown") + " · " + assessment)
        audio_clean = (analysis or {}).get("audio_clean")
        if "audio_clean" not in (analysis or {}):
            rates = [event[key] for key in ("mean", "peak") if event.get(key) is not None]
            audio_clean = not any(rate > 1 for rate in rates) if rates else None
        stalls = audio_clean is False
        if stalls:
            assessment = "Stalls detected · " + assessment
        elif audio_clean is None:
            assessment = "Audio unverified · " + assessment
        details.append("Audio assessment: " + {True: "clean", False: "stalls detected", None: "unverified"}[audio_clean])
        details.append("Internal audio stalls are assessed independently of thermal trends. "
                       "Cooling alone does not prove clean audio.")
        for col, value in ((len(STEP_COLUMNS), thermal), (len(STEP_COLUMNS) + 1, assessment)):
            item = QTableWidgetItem(value)
            item.setToolTip("\n".join(details))
            item.setData(Qt.AccessibleTextRole, value + ". " + " ".join(details))
            self.results.setItem(row, col, item)

    def process_error(self, error):
        if self.process and error == QProcess.FailedToStart:
            self.stderr = self.process.errorString()
            self.finished(1)

    def finished(self, code, *_):
        if not self.process:
            return
        self.read_progress()
        self.read_errors()
        self.process.deleteLater()
        self.process = None
        if code == 0 or (code == SAFETY_EXIT and self.autotune and self.outcome):
            text = self.outcome or "Session ended"
        else:
            text = " ".join(filter(None, [self.stderr.strip() or f"Calibration exited with code {code}",
                                          self.outcome]))
        self.message.setText(text)
        self.set_running(False)
        self.give_back_mode()

    def stop(self):
        if self.process:
            self.cancel.setEnabled(False)
            self.message.setText("Cancelling and finishing cleanup…")
            self.process.terminate()

    # -- auto-tune review ---------------------------------------------------------------------
    def show_proposal(self, table, path=None):
        try:
            self.proposal = core.check_model(table)
        except ValueError as e:
            self.message.setText("Auto-tune proposed an invalid table: %s" % e)
            return
        self.proposal_preset = self.autotune_kind
        current, source = core.current_table()
        self.current_box.setTitle("Current · " + source)
        self.proposed_box.setTitle("Proposed by %s auto-tune" % self.autotune_kind)
        fill_fan_table(self.current_table, current)
        fill_fan_table(self.proposed_table, self.proposal)
        self.review_note.setText(
            "Nothing has been written yet. Each row's floors (percent of full speed) apply from its GPU "
            "power up to the next row; they raise the base curve, never lower it. Write model installs "
            "the proposal as /etc/rog-fand/model.json (password needed) and restarts the fan service."
            + (" Proposal and step results: %s" % path if path else ""))
        self.review_status.setText("")
        self.write_model.setEnabled(True)
        self.discard.setEnabled(True)
        self.review.setVisible(True)
        self.proposalChanged.emit(True)

    def discard_proposal(self):
        if self.writer:
            return
        self.proposal = None
        self.review.setVisible(False)
        self.proposed_table.setRowCount(0)
        self.proposalChanged.emit(False)

    def write(self):
        if self.proposal is None or self.writer:
            return
        command = core.model_command(self.proposal, self.proposal_preset)
        if self.dry_run:
            self.review_status.setText("Dry run · " + " ".join(command))
            return
        if not core.root_owned(core.FAND_INSTALLED):
            self.review_status.setText("The fan service is not installed (service/install.sh), so there is "
                                       "nothing to write the model for.")
            return
        self.writer = QProcess(self)
        self.writer.finished.connect(self.written)
        self.writer.errorOccurred.connect(self.writer_error)
        self.write_model.setEnabled(False)
        self.discard.setEnabled(False)
        self.review_status.setText("Waiting for the password dialog…")
        self.writer.start(command[0], command[1:])

    def writer_error(self, error):
        if self.writer and error == QProcess.FailedToStart:
            self.written(127, None, self.writer.errorString())

    def written(self, code, _status=None, failure=None):
        if not self.writer:
            return
        out = failure or (bytes(self.writer.readAllStandardOutput()) +
                          bytes(self.writer.readAllStandardError())).decode(errors="replace").strip()
        self.writer.deleteLater()
        self.writer = None
        self.discard.setEnabled(True)
        if code == 0:
            self.proposal = None
            self.proposalChanged.emit(False)
            self.review_status.setText("Written to /etc/rog-fand/model.json. The fan service restarted "
                                       "with the new table; check its mode on the Overview.")
            current, source = core.current_table()
            self.current_box.setTitle("Current · " + source)
            fill_fan_table(self.current_table, current)
            return
        self.write_model.setEnabled(True)
        if "unrecognized arguments" in out:
            self.review_status.setText("The installed fan service is older and cannot write models. "
                                       "Update it first (Settings tab), then write again.")
        elif code in (126, 127) and not out:
            self.review_status.setText("Not written: the password dialog was dismissed.")
        else:
            self.review_status.setText("Not written: " + (out[-400:] or "exit code %d" % code))

    # -- export -------------------------------------------------------------------------------
    def save(self):
        default = self.output_path.name if self.autotune and self.output_path else "fan-calibration.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export calibration samples", default, "CSV files (*.csv)")
        if path:
            try:
                self.export_csv(path)
                self.message.setText("Exported calibration samples.")
            except OSError as e:
                self.message.setText(str(e))

    def export_csv(self, path):
        dst = QSaveFile(str(path))
        if not dst.open(QIODevice.WriteOnly):
            raise OSError(dst.errorString())
        try:
            with self.output_path.open("rb") as src:
                while chunk := src.read(65536):
                    if dst.write(chunk) != len(chunk):
                        raise OSError(dst.errorString())
            if not dst.commit():
                raise OSError(dst.errorString())
        finally:
            if dst.isOpen():
                dst.cancelWriting()
