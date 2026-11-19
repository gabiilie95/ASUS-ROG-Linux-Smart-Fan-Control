"""User-session controls for tools/fan-calibrate.py."""
import json
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QProcess, QSaveFile, QIODevice, Signal, Qt
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
        self.step_duration = self.elapsed = 0
        self.outcome = ""
        self.stderr = ""
        layout = QVBoxLayout(self)
        layout.addWidget(heading("Fan calibration"))
        layout.addWidget(label("Measure thermal response and internal audio stalls under a steady workload. "
                               "Temperatures alone do not prove clean audio."))
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
        self.results = QTableWidget(0, 7)
        self.results.setAccessibleName("Calibration results with thermal response and audio assessment")
        self.results.setHorizontalHeaderLabels(["Step", "Samples", "Mean stalls/s", "Peak stalls/s", "GPU power (W)",
                                                "Thermal response", "Assessment"])
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
        self.description.setText(self.description.text() + (" Thermal trends need a stable workload."
            if preset == "Observe" else " Keep the workload steady. A step may extend by up to 120 s to check thermal settling."))

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
            self.step_duration, self.elapsed = event.get("duration_s", self.seconds.value()), 0
            self.message.setText(f"Step {self.index} of {self.total}  ·  {event['message']}")
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
            for col, key in enumerate(("name", "samples", "mean", "peak", "watts")):
                v = event[key]
                text = "—" if v is None else f"{v:.2f}" if isinstance(v, float) else str(v)
                self.results.setItem(row, col, QTableWidgetItem(text))
            self.show_analysis(row, event)
        elif kind in ("done", "restore"):
            self.outcome = (self.outcome + " " + event["message"]).strip()
            self.message.setText(self.outcome)
            if kind == "done" and not event["cancelled"]:
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
        for col, value in ((5, thermal), (6, assessment)):
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
