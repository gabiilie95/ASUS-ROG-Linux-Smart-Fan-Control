"""Smart fan dashboard for the native ROG settings panel."""
from collections import deque
import json
import math
import os
import sys
import time

from PySide6.QtCore import QProcess, QRectF, Qt, QTimer, QPointF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout,
                               QWidget)
import rogcore as core

FAN_LABELS = ("CPU", "GPU", "Middle")
COLORS = ("#4288cf", "#dc8e32", "#329b87")
# Full speed of each fan with every curve point at 255, measured on the G814JZ on 2026-11-16.
TOP_RPM = {"cpu": 6300, "gpu": 6900, "mid": 7400}
SERIES = {"Fan floors": ("% floor", FAN_LABELS), "Fan speeds": ("RPM", FAN_LABELS),
          "Heat input": ("W", ("Smoothed total", "Smoothed GPU")),
          "USB drops": ("packets/s", ("All devices",))}


def label(text=""):
    widget = QLabel(text)
    widget.setTextFormat(Qt.PlainText)
    widget.setWordWrap(True)
    return widget


def heading(text):
    w = label(text)
    font = w.font()
    font.setPointSizeF(font.pointSizeF() * 1.5)
    font.setBold(True)
    w.setFont(font)
    return w


def metric(value, unit):
    return "Unavailable" if value is None else f"{value:g} {unit}"


class History(QWidget):
    def __init__(self):
        super().__init__()
        self.samples = deque(maxlen=180)
        self.kind = "Fan floors"
        self.setMinimumHeight(146)
        self.setAccessibleName("Five-minute fan service history")

    def append(self, stamp, s):
        if self.samples:
            previous = self.samples[-1][0]
            if stamp == previous:
                return
            if stamp < previous or stamp - previous > 300:
                self.samples.clear()
        usb = s["usb"]
        drops = None if usb.get("error") else sum(d["drops_per_s"] for d in usb["devices"])
        self.samples.append((stamp, {
            "Fan floors": list(s["floors"][f] for f in core.FANS),
            "Fan speeds": list(s["sensors"].get(f + "_rpm") for f in core.FANS),
            "Heat input": [s["heat_w"], s["smoothed_gpu_w"]], "USB drops": [drops]}))
        while self.samples and stamp - self.samples[0][0] > 300:
            self.samples.popleft()
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = QRectF(47, 12, max(1, self.width()-60), max(1, self.height()-40))
        p.fillRect(r, pal.base())
        points = [(t, row[self.kind]) for t, row in self.samples]
        values = [v for _, row in points for v in row if v is not None]
        top = 100 if self.kind == "Fan floors" else max(1, math.ceil(max(values, default=1)))
        for frac in (0, 0.5, 1):
            y = r.bottom() - frac*r.height()
            p.setPen(pal.mid().color())
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(pal.text().color())
            p.drawText(QRectF(0, y-9, 41, 18), Qt.AlignRight | Qt.AlignVCenter, f"{top*frac:g}")
        p.drawText(QRectF(r.left(), r.bottom()+4, 80, 20), Qt.AlignLeft, "−5 min")
        p.drawText(QRectF(r.right()-55, r.bottom()+4, 55, 20), Qt.AlignRight, "Now")
        if not values:
            p.drawText(r, Qt.AlignCenter, "Waiting for samples" if not points else "Reading unavailable")
        elif points:
            end = points[-1][0]
            for i in range(len(SERIES[self.kind][1])):
                previous = None
                p.setPen(QPen(QColor(COLORS[i]), 2))
                for t, row in points:
                    if row[i] is None:
                        previous = None
                        continue
                    pt = QPointF(r.right() - (end-t)*r.width()/300, r.bottom()-row[i]*r.height()/top)
                    if previous and t-previous[0] <= 8:
                        p.drawLine(previous[1], pt)
                    else:
                        p.drawPoint(pt)
                    previous = t, pt
        p.end()


class AutoTab(QWidget):
    def __init__(self, dry_run):
        super().__init__()
        self.dry_run = dry_run
        self.preview = None
        self.preview_sample = None
        self.source = None
        self.pending = None
        self.sample_time = None
        self.calibrating = False
        self.state = ""
        root = QVBoxLayout(self)
        root.setSpacing(10)
        header = QHBoxLayout()
        header.addWidget(heading("Smart fan control"))
        header.addStretch()
        self.state_label = label()
        header.addWidget(self.state_label)
        root.addLayout(header)
        self.detail = label()
        root.addWidget(self.detail)
        controls = QHBoxLayout()
        controls.addWidget(label("Control mode"))
        self.mode = QComboBox()
        for key, name in core.FAND_MODES.items():
            self.mode.addItem(name, key)
        self.mode.setAccessibleName("Control mode")
        controls.addWidget(self.mode)
        self.apply = QPushButton("Apply mode")
        self.apply.setObjectName("primaryAction")
        self.apply.clicked.connect(self.change_mode)
        controls.addWidget(self.apply)
        controls.addStretch()
        self.preview_btn = QPushButton("Preview live sensors")
        self.preview_btn.clicked.connect(self.toggle_preview)
        controls.addWidget(self.preview_btn)
        root.addLayout(controls)
        self.feedback = label()
        root.addWidget(self.feedback)

        grid = QGridLayout()
        fans = QGroupBox("Fan response")
        fg = QGridLayout(fans)
        for i, title in enumerate(("Fan", "Speed now", "Requested floor")):
            fg.addWidget(label(title), 0, i)
        self.speeds, self.bars = {}, {}
        for i, (fan, title) in enumerate(zip(core.FANS, FAN_LABELS), 1):
            fg.addWidget(label(title), i, 0)
            colour = QColor(COLORS[i-1])
            speed = QProgressBar()
            speed.setRange(0, TOP_RPM[fan])
            speed.setStyleSheet("QProgressBar::chunk {background:" + colour.name() + ";}")
            speed.setAccessibleName(title + " fan speed")
            self.speeds[fan] = speed
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setStyleSheet("QProgressBar::chunk {background: rgba(%d, %d, %d, 110);}" % colour.getRgb()[:3])
            bar.setAccessibleName(title + " fan floor")
            self.bars[fan] = bar
            fg.addWidget(speed, i, 1)
            fg.addWidget(bar, i, 2)
        fg.setColumnStretch(1, 3)
        fg.setColumnStretch(2, 2)
        note = label("Speed is the fan sensor now, against that fan's full speed (%s rpm). The floor is the "
                     "minimum the service requests; the laptop's curve may run the fan faster."
                     % " / ".join(f"{TOP_RPM[f]:,}" for f in core.FANS))
        note.setEnabled(False)
        fg.addWidget(note, len(core.FANS) + 1, 0, 1, 3)
        grid.addWidget(fans, 0, 0)
        thermal = QGroupBox("Heat input")
        form = QFormLayout(thermal)
        self.heat = {}
        for k, title in (("heat_w", "Smoothed total"), ("cpu_w", "CPU package"),
                         ("gpu_w", "NVIDIA GPU"), ("board_c", "Board · SEN3")):
            self.heat[k] = label()
            font = self.heat[k].font()
            font.setBold(True)
            self.heat[k].setFont(font)
            form.addRow(title, self.heat[k])
        grid.addWidget(thermal, 0, 1)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        root.addLayout(grid)
        history = QGroupBox("Last five minutes")
        hl = QVBoxLayout(history)
        row = QHBoxLayout()
        self.series = QComboBox()
        self.series.addItems(SERIES)
        self.series.setAccessibleName("Chart measurement")
        row.addWidget(self.series)
        self.legend = QLabel()
        row.addWidget(self.legend)
        row.addStretch()
        hl.addLayout(row)
        self.chart = History()
        hl.addWidget(self.chart)
        self.series.currentTextChanged.connect(self.select_series)
        self.select_series(self.series.currentText())
        root.addWidget(history)
        self.usb = label()
        self.margin = label()
        root.addWidget(self.usb)
        root.addWidget(self.margin)
        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumHeight(72)
        self.events.setAccessibleName("Recent service events")
        root.addWidget(label("Recent service events"))
        root.addWidget(self.events)
        root.addStretch()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    def select_series(self, kind):
        self.chart.kind = kind
        unit, names = SERIES[kind]
        self.legend.setText(" &nbsp; ".join(f'<span style="color:{c}">●</span> {name}'
                                          for c, name in zip(COLORS, names)) + f" &nbsp; ({unit})")
        self.chart.update()

    def toggle_preview(self):
        if self.preview:
            self.preview.terminate()
            return
        p = QProcess(self)
        self.preview = p
        self.preview_sample = None
        p.readyReadStandardOutput.connect(self.preview_output)
        p.finished.connect(self.preview_done)
        p.errorOccurred.connect(lambda error: self.preview_done() if error == QProcess.FailedToStart else None)
        p.setStandardErrorFile(QProcess.nullDevice())
        p.start(sys.executable, ["-IB", str(core.FAND_PROGRAM), "--dry-run"])
        self.refresh()

    def preview_output(self):
        while self.preview and self.preview.canReadLine():
            try:
                s = json.loads(bytes(self.preview.readLine()))
                core.validate_fand_sample(s)
                self.preview_sample = s
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        self.refresh()

    def preview_done(self, *_):
        if self.preview:
            self.preview.deleteLater()
        self.preview = self.preview_sample = None
        self.refresh()

    def refresh(self):
        state, s = core.fan_service()
        if self.preview:
            state, s = "Dry run", self.preview_sample
        self.display(state, s)

    def display(self, state, s):
        if self.source != state:
            self.chart.samples.clear()
            self.chart.update()
            self.source = state
            self.confirmed = None
            self.feedback.clear()
        self.state = state
        self.state_label.setText("rog-fand  ·  " + state)
        self.preview_btn.setText("End preview" if self.preview else "Preview live sensors")
        self.preview_btn.setVisible(bool(self.preview) or state != "Running")
        can_control = (state == "Running" and not self.dry_run and not self.calibrating and not (s or {}).get("manual_control")
                       and os.access(core.FAND_RUN / "control", os.W_OK))
        self.mode.setEnabled(can_control)
        self.apply.setEnabled(can_control)
        info = {"Not installed": "Install the root service with sudo service/install.sh install, or preview live sensors.",
                "Stopped": "The unit is installed and has no current status. Check service/install.sh status.",
                "Stale": "Service updates have stopped. Waiting for a fresh sample.",
                "Unreadable": "Cannot read service status. Check the rog-fand journal.",
                "Dry run": "Read-only sensor preview. Floors show what the controller would request."}
        detail = info.get(state, "")
        if s:
            profile = s["sensors"].get("profile") or "unknown"
            if state == "Running":
                detail = f"{profile.capitalize()} profile  ·  {core.FAND_MODES[s['mode']]} mode"
                if s["sensors"].get("profile") != "performance":
                    detail += ("  ·  Paused: rog-fand only controls the fans in Performance. Switch back from the "
                               "tray menu or with: asusctl profile set Performance")
                if s.get("manual_control"):
                    detail += "  ·  Calibration owns the fans"
                elif not s.get("applied"):
                    detail += "  ·  No override applied"
                if not can_control and not self.calibrating and not self.dry_run and not s.get("manual_control"):
                    detail += ("  ·  Update the installed service for panel mode control"
                               if not (core.FAND_RUN / "control").is_dir() else
                               "  ·  Mode control requires the configured desktop user")
            if s["unavailable"]:
                detail += "\n" + " · ".join(s["unavailable"])
            if s.get("invalid_mode"):
                detail += "\nThe service rejected the mode file and kept the previous mode."
            if self.confirmed != s["mode"]:
                self.feedback.clear()
                self.mode.setCurrentIndex(self.mode.findData(s["mode"]))
                self.confirmed = s["mode"]
            self.chart.append(core.validate_fand_sample(s), s)
        if self.pending:
            wanted, deadline, previous_sample = self.pending
            if (state == "Running" and s and s["t"] != previous_sample and s["mode"] == wanted
                    and not s.get("invalid_mode")):
                self.feedback.setText("Service confirmed " + core.FAND_MODES[wanted] + ".")
                self.pending = None
            elif time.monotonic() >= deadline or state != "Running":
                self.feedback.setText("Mode request was not confirmed.")
                self.pending = None
        self.sample_time = s["t"] if s else None
        self.feedback.setVisible(bool(self.feedback.text()) or self.pending is not None)
        self.detail.setText(detail)
        try:
            live = core.read_fans()      # the sensors themselves, so speeds show without the service too
        except OSError:
            live = {}
        for fan in core.FANS:
            floor = s["floors"][fan] if s else None
            self.bars[fan].setValue(round(floor or 0))
            self.bars[fan].setFormat("—" if floor is None else f"{floor:g}%")
            rpm = live.get(fan + "_rpm")
            if rpm is None and s:
                rpm = s["sensors"].get(fan + "_rpm")
            speed = self.speeds[fan]
            speed.setValue(min(rpm or 0, TOP_RPM[fan]))
            speed.setFormat("—" if rpm is None else f"{rpm:,.0f} rpm · {100 * rpm / TOP_RPM[fan]:.0f}%")
        for k, lab in self.heat.items():
            value = (s["heat_w"] if k == "heat_w" else s["sensors"].get(k)) if s else None
            lab.setText(metric(value, "°C" if k == "board_c" else "W"))
        usb = s["usb"] if s else {}
        devices = [f"{d.get('name') or d.get('path') or 'USB device'} · {d['drops_per_s']:g} drops/s"
                   for d in usb.get("devices", [])]
        audio = "Audio active" if usb.get("audio_active") else "Idle"
        self.usb.setText("USB audio   " + (usb.get("error") or "  /  ".join([audio, *devices]) if s else "—"))
        margins = sorted(s["margins"].items(), key=lambda x: float(x[0])) if s else []
        self.margin.setText("GPU-bin margins   " + ("   ·   ".join(f"≥{w} W  +{v:g} pp" for w,v in margins) or "—"))
        events = "\n".join(f"{e['t'][11:19]} UTC   {e['message']}" for e in reversed(s["events"])) if s else ""
        if self.events.toPlainText() != events:
            self.events.setPlainText(events)
        self.events.setPlaceholderText("Events appear when the controller reports a change.")

    def change_mode(self):
        if self.dry_run or self.calibrating or self.preview:
            return
        self.feedback.show()
        wanted = self.mode.currentData()
        try:
            core.request_fan_mode(wanted)
        except (ValueError, OSError) as e:
            self.feedback.setText(str(e))
            return
        self.pending = wanted, time.monotonic()+10, self.sample_time
        self.feedback.setText("Waiting for service confirmation…")

    def shutdown(self):
        self.timer.stop()
        p = self.preview
        if p:
            p.terminate()
            if not p.waitForFinished(3000):
                p.kill()
                p.waitForFinished(1000)
