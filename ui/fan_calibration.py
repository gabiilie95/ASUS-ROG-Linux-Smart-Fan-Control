"""User-session controls for tools/fan-calibrate.py and tools/autotune.py."""
import json
from pathlib import Path
import sys
import tempfile
import time

from PySide6.QtCore import QProcess, QSaveFile, QIODevice, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLineEdit, QMessageBox, QProgressBar,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
import rogcore as core
from fan_auto import heading, label

AUTOTUNE = "Auto-tune (about 60-90 min, loud)"
SAFETY_EXIT = 3     # autotune.py stopped at a safety limit; the reason is in its progress
STEP_COLUMNS = ["Step", "Samples", "Mean stalls/s", "Peak stalls/s", "GPU power (W)"]
AUTOTUNE_COLUMNS = STEP_COLUMNS + ["USB drops", "Verdict"]
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

    def __init__(self, dry_run, action_in_progress):
        super().__init__()
        self.dry_run = dry_run
        self.action_in_progress = action_in_progress
        self.process = self.workspace = self.output_path = None
        self.writer = None
        self.proposal = None
        self.autotune = False
        self.index = self.total = 0
        self.outcome = ""
        self.stderr = ""
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Fan calibration"))
        layout.addWidget(label("Measure internal audio stalls under your current workload, or let "
                               "auto-tune generate the heat and find the quietest clean fan mix."))
        setup = QGroupBox("Session")
        form = QFormLayout(setup)
        self.preset = QComboBox()
        self.preset.addItems(["Observe", "Light", "Heavy", "Custom", AUTOTUNE])
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
        for btn, slot in ((self.start, self.begin), (self.cancel, self.stop), (self.export, self.save)):
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

        self.results = table_widget(STEP_COLUMNS)
        layout.addWidget(self.results, 1)

    def preset_changed(self, preset):
        auto = preset == AUTOTUNE
        self.form.setRowVisible(self.custom, preset == "Custom")
        self.form.setRowVisible(self.seconds, not auto)
        if not auto:
            self.seconds.setValue({"Light": 180, "Heavy": 240, "Observe": 120, "Custom": 180}[preset])
        self.description.setText({
            "Observe": "Record the current curves and stall rate. Fan settings stay in place.",
            "Light": "Seven measured steps for light gaming, including middle-fan and assisted cooling.",
            "Heavy": "Four heavy-load steps, starting with all fans at full speed.",
            "Custom": "Separate steps with a semicolon. Omitted fans follow their saved curves.",
            AUTOTUNE: "Unattended: heats the GPU (160, 120, 80 and 40 W) and CPU, and at each level steps "
                      "the fans from loud to quiet to find the quietest mix with clean audio. About 60-90 "
                      "minutes with loud fans; the laptop speakers stay blocked. Needs Performance, Auto set "
                      "to Off, the power adapter and the USB audio interface. Nothing is written until you "
                      "review the proposed table.",
        }[preset])

    def begin(self):
        if self.process:
            return
        if self.action_in_progress():
            self.message.setText("Wait for the current panel action to finish.")
            return
        preset = self.preset.currentText()
        auto = preset == AUTOTUNE
        new_workspace = None
        try:
            if auto:
                output = core.log_dir() / time.strftime("autotune-%Y%m%d-%H%M%S.csv")
                args = core.autotune_args(output)
            else:
                args = core.calibration_args(preset, self.seconds.value(), self.custom.text(), "samples.csv")
            if self.dry_run:
                self.message.setText("Dry run · " + " ".join(args))
                return
            if core.sound_device_busy():
                self.message.setText("The internal sound device is busy. Stop playback or the stall monitor first.")
                return
            if preset != "Observe":
                problem = core.manual_fan_problem()
                if problem:
                    self.message.setText(problem)
                    return
            if auto:
                if core.fan_service()[0] != "Running":
                    self.message.setText("Auto-tune counts USB drops through the fan service. Start the service first.")
                    return
                answer = QMessageBox.question(
                    self, "Start auto-tune?",
                    "Auto-tune heats the laptop and runs the fans loud for about 60-90 minutes. The "
                    "laptop speakers stay blocked meanwhile. You can cancel at any time; the saved fan "
                    "curves are restored at the end.")
                if answer != QMessageBox.Yes:
                    return
                output.parent.mkdir(parents=True, exist_ok=True)
            else:
                new_workspace = tempfile.TemporaryDirectory(prefix="rog-fan-cal-")
        except (ValueError, OSError) as e:
            self.message.setText(str(e))
            return
        if self.workspace:
            self.workspace.cleanup()
        self.workspace = new_workspace
        if auto:
            self.output_path = output
        else:
            self.output_path = Path(new_workspace.name) / "samples.csv"
            args = core.calibration_args(preset, self.seconds.value(), self.custom.text(), self.output_path)
        self.autotune = auto
        self.discard_proposal()
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.stderr = self.outcome = ""
        self.index = self.total = 0
        self.results.setColumnCount(0)
        columns = AUTOTUNE_COLUMNS if auto else STEP_COLUMNS
        self.results.setColumnCount(len(columns))
        self.results.setHorizontalHeaderLabels(columns)
        self.results.setRowCount(0)
        self.progress.setValue(0)
        self.set_running(True)
        self.message.setText("Building the load generator and starting the counters…" if auto
                             else "Starting the stall counter…")
        self.process.start(sys.executable, args)

    def set_running(self, running):
        for w in (self.preset, self.seconds, self.custom, self.start):
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
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind in ("step", "level", "level-result"):
            self.message.setText(event["message"])
        elif kind == "sample" and "progress" in event:
            self.progress.setValue(round(100 * min(1, max(0, event["progress"]))))
            self.message.setText(f"{event['message']}  ·  {event['run_s'] / 60:.0f} min")
        elif kind == "sample" and self.total:
            fraction = min(1, event["elapsed"]/self.seconds.value())
            self.progress.setValue(round(100*(self.index-1+fraction)/self.total))
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
        elif kind == "proposal":
            self.show_proposal(event["table"], event.get("path"))
        elif kind in ("done", "restore", "safety"):
            self.outcome = (self.outcome + " " + event["message"]).strip()
            self.message.setText(self.outcome)
            if kind == "done" and not event["cancelled"] and not event.get("safety"):
                self.progress.setValue(100)

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
        current, source = core.current_table()
        self.current_box.setTitle("Current · " + source)
        fill_fan_table(self.current_table, current)
        fill_fan_table(self.proposed_table, self.proposal)
        self.review_note.setText(
            "Nothing has been written yet. Each row's floors (percent of full speed) apply from its GPU "
            "power up to the next row; they raise the base curve, never lower it. Write model installs "
            "the proposal as /etc/rog-fand/model.json (password needed) and restarts the fan service; "
            "set Auto again afterwards." + (" Proposal and step results: %s" % path if path else ""))
        self.review_status.setText("")
        self.write_model.setEnabled(True)
        self.discard.setEnabled(True)
        self.review.setVisible(True)

    def discard_proposal(self):
        if self.writer:
            return
        self.proposal = None
        self.review.setVisible(False)
        self.proposed_table.setRowCount(0)

    def write(self):
        if self.proposal is None or self.writer:
            return
        command = core.model_command(self.proposal)
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
            self.review_status.setText("Written to /etc/rog-fand/model.json. The fan service restarted "
                                       "with the new table; set Auto in the Auto tab to use it.")
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
