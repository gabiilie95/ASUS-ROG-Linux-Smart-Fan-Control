"""The panel's Overview tab: fan service control, fan speeds, every readable sensor, a five-minute
history chart and the fan calibration setup."""
from collections import deque
import json
import os
import sys
import time

from PySide6.QtCore import QObject, QProcess, QRunnable, QThreadPool, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit, QProgressBar,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
import rogcore as core
import sensor_scan
from calibration_card import CalibrationCard, check_items
from charts import STATUS, SERIES, Sparkline, TimeChart, is_dark, series_color

FAN_LABELS = ("CPU", "GPU", "Middle")
# Full speed of each fan with every curve point at 255, measured on the G814JZ on 2026-11-16.
TOP_RPM = {"cpu": 6300, "gpu": 6900, "mid": 7400}
# Chart views: name -> (unit, ((key, series name), ...), source, from zero, fixed range).
# "sensors" keys are sensor ids (sensor_scan plus the service's own readings); "service" keys are
# per-sample service values.
VIEWS = {
    "Board temperatures": ("°C", (("sen3", "SEN3"), ("dimm_a", "Memory A"), ("dimm_b", "Memory B"),
                                  ("lan", "Ethernet"), ("ssd1", "SSD 1"), ("ssd2", "SSD 2"), ("wifi", "Wi-Fi")),
                           "sensors", False, None),
    "CPU and GPU temperatures": ("°C", (("cpu_pkg", "CPU package"), ("cpu_core_max", "Hottest core"),
                                        ("gpu_c", "GPU")), "sensors", False, None),
    "Fan speeds": ("rpm", (("cpu_fan", "CPU"), ("gpu_fan", "GPU"), ("mid_fan", "Middle")), "sensors", True, None),
    "Fan floors": ("%", (("cpu", "CPU"), ("gpu", "GPU"), ("mid", "Middle")), "service", True, (0, 100)),
    "Power": ("W", (("cpu_w", "CPU package"), ("gpu_w", "GPU"), ("heat_w", "Heat input")), "sensors", True, None),
    "USB drops": ("drops/s", (("usb_drops", "All USB audio"),), "service", True, None),
}
# Sensor cards: title -> ((id, row name), ...). Rows without history show text, not a sparkline.
CARDS = (
    ("CPU", (("cpu_pkg", "Package"), ("cpu_core_max", "Hottest core"), ("cpu_w", "Power"),
             ("cpu_clock_avg", "Clock · average"), ("cpu_clock_max", "Clock · fastest"), ("cpu_cap", "Performance cap"))),
    ("GPU", (("gpu_c", "Temperature"), ("gpu_w", "Power"), ("gpu_util", "Load"), ("gpu_mem", "Memory"))),
    ("Board", (("sen3", "SEN3"), ("dimm_a", "Memory A"), ("dimm_b", "Memory B"), ("lan", "Ethernet"),
               ("ssd1", "SSD 1"), ("ssd2", "SSD 2"), ("wifi", "Wi-Fi"))),
    ("Power", (("ac", "Adapter"), ("bat_pct", "Battery"), ("bat_w", "Battery power"), ("bat_v", "Battery voltage"))),
)
TEXT_ROWS = {"cpu_cap", "gpu_mem", "ac"}
CARD_ORDER = {rid: i for i, rid in enumerate(rid for _, rows in CARDS for rid, _ in rows)}
TABLE_COLUMNS = ["Group", "Sensor", "Now", "Lowest", "Highest", "Source"]


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


def first(*values):
    return next((v for v in values if v is not None), None)


# ---- sensor scan on a worker thread -------------------------------------------------------------
class ScanSignals(QObject):
    done = Signal(object)


class ScanJob(QRunnable):
    def __init__(self, signals, sys_root):
        super().__init__()
        self.signals, self.sys_root = signals, sys_root

    def run(self):
        try:
            result = sensor_scan.scan(self.sys_root)
        except Exception as e:      # a strange sysfs file must never kill the panel
            result = dict(readings=[], ac=None, battery_status=None, error=str(e))
        try:
            self.signals.done.emit(result)
        except RuntimeError:        # the tab was closed meanwhile
            pass


class SensorCard(QGroupBox):
    """One group of sensors: name, value, attention badge and a five-minute sparkline per row."""

    def __init__(self, title, rows):
        super().__init__(title)
        grid = QGridLayout(self)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)
        self.rows = {}
        for i, (rid, name) in enumerate(rows):
            value = QLabel("—")
            value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            font = value.font()
            font.setBold(True)
            value.setFont(font)
            badge = QLabel()
            badge.setTextFormat(Qt.RichText)
            spark = None if rid in TEXT_ROWS else Sparkline()
            grid.addWidget(QLabel(name), i, 0)
            grid.addWidget(value, i, 1)
            grid.addWidget(badge, i, 2)
            if spark:
                spark.setAccessibleName(f"{title} {name}, last five minutes")
                grid.addWidget(spark, i, 3)
            self.rows[rid] = (value, badge, spark)
        grid.setColumnStretch(0, 1)
        grid.setRowStretch(len(rows), 1)

    def set(self, rid, text, level=None, points=None, tip=""):
        value, badge, spark = self.rows[rid]
        value.setText(text)
        value.setToolTip(tip)
        badge.setText("" if not level else
                      f'<span style="color:{STATUS[level]}">●</span> {"Hot" if level == "hot" else "Warm"}')
        if spark is not None:
            spark.set_points(points or [], level)


class AutoTab(QWidget):
    def __init__(self, dry_run, sys_root="/sys"):
        super().__init__()
        self.dry_run = dry_run
        self.sys_root = sys_root
        self.preview = None
        self.preview_sample = None
        self.source = None
        self.pending = None
        self.sample_time = None
        self.calibrating = False
        self.proposal = False
        self.state = ""
        self.confirmed = None
        self.last_sample = None       # the service sample shown now (None when not fresh)
        self.system = {}              # the window's status poll: CPU policy and nvidia-smi readings
        self.scan_result = None
        self.scanning = False
        self.sensors = sensor_scan.SensorHistory()
        self.service_log = deque(maxlen=180)   # (service time, per-sample values), deduplicated
        self.scan_signals = ScanSignals(self)
        self.scan_signals.done.connect(self.take_scan)
        self.colors = SERIES[is_dark(self)]
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
        self.calibration = CalibrationCard()
        root.addWidget(self.calibration)

        top = QHBoxLayout()
        fans = QGroupBox("Fans")
        fg = QGridLayout(fans)
        for i, title in enumerate(("Fan", "Speed now", "Requested floor")):
            fg.addWidget(label(title), 0, i)
        self.speeds, self.bars = {}, {}
        for i, (fan, title) in enumerate(zip(core.FANS, FAN_LABELS), 1):
            fg.addWidget(label(title), i, 0)
            colour = QColor(self.colors[i - 1])
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
        top.addWidget(fans, 3)
        audio = QGroupBox("Audio and heat input")
        form = QFormLayout(audio)
        self.heat = {}
        for k, title in (("heat_w", "Heat input · smoothed"), ("smoothed_gpu_w", "GPU · smoothed")):
            self.heat[k] = label()
            font = self.heat[k].font()
            font.setBold(True)
            self.heat[k].setFont(font)
            form.addRow(title, self.heat[k])
        self.usb = label()
        form.addRow("USB audio", self.usb)
        self.margin = label()
        form.addRow("GPU-bin margins", self.margin)
        top.addWidget(audio, 2)
        root.addLayout(top)

        sensors = QHBoxLayout()
        self.cards = {}
        for title, rows in CARDS:
            card = SensorCard(title, rows)
            self.cards[title] = card
            sensors.addWidget(card, 1)
        root.addLayout(sensors)

        history = QGroupBox("Last five minutes")
        hl = QVBoxLayout(history)
        row = QHBoxLayout()
        self.series = QComboBox()
        self.series.addItems(VIEWS)
        self.series.setAccessibleName("Chart measurement")
        row.addWidget(self.series)
        self.legend = QLabel()
        self.legend.setTextFormat(Qt.RichText)
        self.legend.setWordWrap(True)
        row.addWidget(self.legend, 1)
        hl.addLayout(row)
        self.chart = TimeChart()
        hl.addWidget(self.chart)
        root.addWidget(history)
        self.series.currentTextChanged.connect(self.update_chart)

        every = QHBoxLayout()
        self.table_btn = QPushButton("Show every sensor")
        self.table_btn.setCheckable(True)
        self.table_btn.toggled.connect(self.toggle_table)
        every.addWidget(self.table_btn)
        self.table_note = label("Lowest and highest are since the panel opened.")
        self.table_note.setEnabled(False)
        every.addWidget(self.table_note, 1)
        root.addLayout(every)
        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(TABLE_COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        head = self.table.horizontalHeader()
        head.setSectionResizeMode(QHeaderView.ResizeToContents)
        head.setSectionResizeMode(len(TABLE_COLUMNS) - 1, QHeaderView.Stretch)
        self.table.setAccessibleName("Every sensor with its lowest and highest value")
        self.table.hide()
        self.table_note.hide()
        self.table_ids = []
        root.addWidget(self.table)

        self.events = QPlainTextEdit()
        self.events.setReadOnly(True)
        self.events.setMaximumHeight(72)
        self.events.setAccessibleName("Recent service events")
        root.addWidget(label("Recent service events"))
        root.addWidget(self.events)
        root.addStretch()
        self.update_chart()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh()

    # -- service preview ------------------------------------------------------------------------
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

    # -- polling --------------------------------------------------------------------------------
    def refresh(self):
        state, s = core.fan_service()
        if self.preview:
            state, s = "Dry run", self.preview_sample
        self.display(state, s)
        self.start_scan()

    def start_scan(self):
        if not self.scanning:
            self.scanning = True
            QThreadPool.globalInstance().start(ScanJob(self.scan_signals, self.sys_root))

    def take_scan(self, result):
        """A finished sensor scan: record it with the service's own readings and redraw."""
        self.scanning = False
        self.scan_result = result
        self.sensors.add(time.monotonic(), result["readings"] + self.service_readings())
        self.render_sensors()
        self.update_calibration()
        if VIEWS[self.series.currentText()][2] == "sensors":
            self.update_chart()

    def set_system(self, status):
        """The window's 2 s status poll (CPU policy and nvidia-smi), shown in the CPU and GPU cards."""
        self.system = status or {}

    def service_readings(self):
        """Readings only the fan service (root: RAPL, NVML) or nvidia-smi provide, as sensor rows."""
        s = self.last_sample or {}
        sens = s.get("sensors") or {}
        gpu = self.system.get("gpu") or {}
        r = sensor_scan.reading
        return [r("cpu_w", "CPU", "Package power", sens.get("cpu_w"), "W", "rog-fand · RAPL"),
                r("gpu_c", "GPU", "Temperature", first(sens.get("gpu_c"), gpu.get("temp_c")), "°C",
                  "NVIDIA driver", *sensor_scan.LIMITS["gpu"]),
                r("gpu_w", "GPU", "Power", first(sens.get("gpu_w"), gpu.get("power_w")), "W", "NVIDIA driver"),
                r("gpu_util", "GPU", "Load", gpu.get("util_pct"), "%", "nvidia-smi"),
                r("heat_w", "Power", "Heat input · smoothed", s.get("heat_w"), "W", "rog-fand")]

    # -- rendering ------------------------------------------------------------------------------
    def display(self, state, s):
        if self.source != state:
            self.service_log.clear()
            self.source = state
            self.confirmed = None
            self.feedback.clear()
        self.state = state
        self.last_sample = s
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
                if not core.fans_protected(s):
                    detail += "  ·  Paused: rog-fand does not protect this profile, so audio may crackle under load."
                    if core.performance_protected(s):
                        detail += " Switch to Performance from the tray menu or with: asusctl profile set Performance"
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
            self.log_service(core.validate_fand_sample(s), s)
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
            lab.setText(metric(s.get(k) if s else None, "W"))
        usb = s["usb"] if s else {}
        devices = [f"{d.get('name') or d.get('path') or 'USB device'} · {d['drops_per_s']:g} drops/s"
                   for d in usb.get("devices", [])]
        audio = "Audio active" if usb.get("audio_active") else "Idle"
        self.usb.setText((usb.get("error") or "  /  ".join([audio, *devices])) if s else "—")
        margins = sorted(s["margins"].items(), key=lambda x: float(x[0])) if s else []
        self.margin.setText("   ·   ".join(f"≥{w} W  +{v:g} pp" for w, v in margins) or "—")
        events = "\n".join(f"{e['t'][11:19]} UTC   {e['message']}" for e in reversed(s["events"])) if s else ""
        if self.events.toPlainText() != events:
            self.events.setPlainText(events)
        self.events.setPlaceholderText("Events appear when the controller reports a change.")
        self.update_calibration()
        if VIEWS[self.series.currentText()][2] == "service":
            self.update_chart()

    def log_service(self, stamp, s):
        """One row per new service sample; a gap or a clock jump starts the history again."""
        if self.service_log:
            previous = self.service_log[-1][0]
            if stamp == previous:
                return
            if stamp < previous or stamp - previous > 300:
                self.service_log.clear()
        usb = s["usb"]
        drops = None if usb.get("error") else sum(d["drops_per_s"] for d in usb["devices"])
        self.service_log.append((stamp, dict(s["floors"], usb_drops=drops)))
        while self.service_log and stamp - self.service_log[0][0] > 300:
            self.service_log.popleft()

    def render_sensors(self):
        h = self.sensors
        info = h.info
        for card in self.cards.values():
            for rid in card.rows:
                if rid in TEXT_ROWS:
                    continue
                r = info.get(rid)
                value = r["value"] if r else None
                tip = ""
                if r:
                    tip = r["source"]
                    if r.get("warm") is not None or r.get("hot") is not None:
                        tip += "\nWarm from %s, hot from %s" % (sensor_scan.fmt(r.get("warm"), r["unit"]),
                                                               sensor_scan.fmt(r.get("hot"), r["unit"]))
                    if rid in h.low:
                        tip += "\nSince the panel opened: %s to %s" % (sensor_scan.fmt(h.low[rid], r["unit"]),
                                                                       sensor_scan.fmt(h.high[rid], r["unit"]))
                text = "—" if r is None else sensor_scan.fmt(value, r["unit"])
                if rid == "bat_pct" and value is not None and (self.scan_result or {}).get("battery_status"):
                    text += " · " + self.scan_result["battery_status"].lower()
                card.set(rid, text, sensor_scan.level(r) if r else None, h.series(rid), tip)
        cpu, gpu = self.system.get("cpu") or {}, self.system.get("gpu") or {}
        cap = cpu.get("max_perf_pct")
        self.cards["CPU"].set("cpu_cap", "—" if cap is None else f"{cap} %",
                              tip="intel_pstate max_perf_pct · governor %s" % (cpu.get("governor") or "unknown"))
        used, total = gpu.get("mem_used_mb"), gpu.get("mem_total_mb")
        self.cards["GPU"].set("gpu_mem", "—" if used is None or not total else
                              f"{used / 1024:.1f} / {total / 1024:.0f} GB", "warm" if gpu.get("mem_high") else None,
                              tip="nvidia-smi")
        ac = (self.scan_result or {}).get("ac")
        self.cards["Power"].set("ac", {True: "Connected", False: "Unplugged"}.get(ac, "—"))
        self.fill_table()

    def toggle_table(self, on):
        self.table.setVisible(on)
        self.table_note.setVisible(on)
        self.fill_table()

    def fill_table(self):
        h = self.sensors
        ids = sorted(h.info, key=lambda rid: (
            sensor_scan.GROUPS.index(h.info[rid]["group"]) if h.info[rid]["group"] in sensor_scan.GROUPS else 99,
            CARD_ORDER.get(rid, len(CARD_ORDER))))     # a group's card rows first, then the rest as found
        self.table_btn.setText(("Hide" if self.table_btn.isChecked() else "Show") + f" every sensor ({len(ids)})")
        if not self.table_btn.isChecked():
            return
        if ids != self.table_ids:
            self.table.setRowCount(len(ids))
            self.table_ids = ids
        for row, rid in enumerate(ids):
            r = h.info[rid]
            unit = r["unit"]
            values = [r["group"], r["name"], sensor_scan.fmt(r["value"], unit),
                      sensor_scan.fmt(h.low.get(rid), unit), sensor_scan.fmt(h.high.get(rid), unit), r["source"]]
            level = sensor_scan.level(r)
            if level:
                values[2] += "  ● " + ("Hot" if level == "hot" else "Warm")
            for col, text in enumerate(values):
                item = self.table.item(row, col)
                if item is None:
                    item = QTableWidgetItem()
                    if col in (2, 3, 4):
                        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                    self.table.setItem(row, col, item)
                if item.text() != text:
                    item.setText(text)
        height = self.table.horizontalHeader().height() + sum(self.table.rowHeight(i) for i in range(len(ids)))
        self.table.setFixedHeight(height + 2 * self.table.frameWidth())

    def update_chart(self, *_):
        view = self.series.currentText()
        unit, series, source, zero, fixed = VIEWS[view]
        if source == "sensors":
            data = [(name, self.sensors.series(key)) for key, name in series]
            empty = "Waiting for sensor readings"
        else:
            data = [(name, [(t, row.get(key)) for t, row in self.service_log]) for key, name in series]
            empty = "Waiting for fan service samples"
        self.chart.set_series(data, unit, zero, fixed, empty)
        self.chart.setAccessibleName(f"{view}, last five minutes")
        dots = [f'<span style="color:{series_color(self, i).name()}">●</span>&nbsp;{name}'
                for i, (_, name) in enumerate(series)]
        self.legend.setText(" &nbsp; ".join(dots) + f" &nbsp; ({unit})")

    def calibration_checks(self):
        ac = (self.scan_result or {}).get("ac")
        profile = ((self.last_sample or {}).get("sensors") or {}).get("profile") or core.rd(core.PROFILE_FILE)
        return check_items(self.state, profile, ac, not core.sound_device_busy(),
                           core.usb_audio_card(), core.AUTOTUNE_USB)

    def update_calibration(self):
        self.calibration.show_state(core.calibration_state(), self.calibration_checks(), self.calibrating,
                                    self.proposal, self.dry_run)

    def set_proposal(self, ready):
        self.proposal = bool(ready)
        self.update_calibration()

    # -- control --------------------------------------------------------------------------------
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
