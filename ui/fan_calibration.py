"""User-session controls for tools/fan-calibrate.py."""
import json
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QProcess, QSaveFile, QIODevice, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLineEdit, QProgressBar, QPushButton,
                               QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
import rogcore as core
from fan_auto import heading, label


class CalibrateTab(QWidget):
    runningChanged = Signal(bool)

    def __init__(self, dry_run, action_in_progress):
        super().__init__()
        self.dry_run = dry_run
        self.action_in_progress = action_in_progress
        self.process = self.workspace = self.output_path = None
        self.index = self.total = 0
        self.outcome = ""
        self.stderr = ""
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Fan calibration"))
        layout.addWidget(label("Measure internal audio stalls under your current workload."))
        setup = QGroupBox("Session")
        form = QFormLayout(setup)
        self.preset = QComboBox()
        self.preset.addItems(["Observe", "Light", "Heavy", "Custom"])
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
        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["Step", "Samples", "Mean stalls/s", "Peak stalls/s", "GPU power (W)"])
        self.results.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.results.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results.verticalHeader().setVisible(False)
        self.results.setAlternatingRowColors(True)
        layout.addWidget(self.results, 1)

    def preset_changed(self, preset):
        self.form.setRowVisible(self.custom, preset == "Custom")
        self.seconds.setValue({"Light": 180, "Heavy": 240, "Observe": 120, "Custom": 180}[preset])
        self.description.setText({
            "Observe": "Record the current curves and stall rate. Fan settings stay in place.",
            "Light": "Seven measured steps for light gaming, including middle-fan and assisted cooling.",
            "Heavy": "Four heavy-load steps, starting with all fans at full speed.",
            "Custom": "Separate steps with a semicolon. Omitted fans follow their saved curves."
        }[preset])

    def begin(self):
        if self.process:
            return
        if self.action_in_progress():
            self.message.setText("Wait for the current panel action to finish.")
            return
        preset = self.preset.currentText()
        try:
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
            new_workspace = tempfile.TemporaryDirectory(prefix="rog-fan-cal-")
        except (ValueError, OSError) as e:
            self.message.setText(str(e))
            return
        if self.workspace:
            self.workspace.cleanup()
        self.workspace = new_workspace
        self.output_path = Path(new_workspace.name) / "samples.csv"
        args = core.calibration_args(preset, self.seconds.value(), self.custom.text(), self.output_path)
        self.process = QProcess(self)
        self.process.readyReadStandardOutput.connect(self.read_progress)
        self.process.readyReadStandardError.connect(self.read_errors)
        self.process.finished.connect(self.finished)
        self.process.errorOccurred.connect(self.process_error)
        self.stderr = self.outcome = ""
        self.index = self.total = 0
        self.results.setRowCount(0)
        self.progress.setValue(0)
        self.set_running(True)
        self.message.setText("Starting the stall counter…")
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
        if kind == "step":
            self.index, self.total = event["index"], event["total"]
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind == "sample" and self.total:
            fraction = min(1, event["elapsed"]/self.seconds.value())
            self.progress.setValue(round(100*(self.index-1+fraction)/self.total))
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
        elif kind == "result":
            row = self.results.rowCount()
            self.results.insertRow(row)
            for col, key in enumerate(("name", "samples", "mean", "peak", "watts")):
                v = event[key]
                text = "—" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)
                self.results.setItem(row, col, QTableWidgetItem(text))
        elif kind in ("done", "restore"):
            self.outcome = (self.outcome + " " + event["message"]).strip()
            self.message.setText(self.outcome)
            if kind == "done" and not event["cancelled"]:
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
        self.message.setText((self.stderr.strip() or f"Calibration exited with code {code}") if code
                             else (self.outcome or "Session ended"))
        self.set_running(False)

    def stop(self):
        if self.process:
            self.cancel.setEnabled(False)
            self.message.setText("Cancelling and finishing cleanup…")
            self.process.terminate()

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export calibration samples", "fan-calibration.csv",
                                               "CSV files (*.csv)")
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
