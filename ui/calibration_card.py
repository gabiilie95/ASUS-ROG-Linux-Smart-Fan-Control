"""The Overview's fan calibration card: first-run setup, readiness checks and the calibration state.

Until an auto-tune table is installed (and unless the user chose "Not now"), the card asks to finish
setup with a quick or a full auto-tune and lists what the run needs, live. Afterwards it shrinks to
one line saying when and how the laptop was calibrated. Starting is always the user's click; the
Calibrate tab then asks once more before the fans get loud.
"""
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from charts import STATUS

QUICK_MINUTES, FULL_MINUTES = "20-40", "60-110"


def check_items(state, profile, ac, speakers_free, usb_found, usb_name):
    """[(ok, text, required)] for an auto-tune run, from the fan service state and live sensors."""
    return [
        (state == "Running", "Fan service running" if state == "Running" else
         f"Fan service: {state.lower()}", True),
        (profile == "performance", "Performance profile" if profile == "performance" else
         "Switch to the Performance profile (tray menu or Fn+F5)", True),
        (ac, "Power adapter connected" if ac else "Connect the power adapter", True),
        (speakers_free, "Laptop speakers free" if speakers_free else
         "Stop sound on the laptop speakers and the stall monitor", True),
        (usb_found, f"{usb_name} connected: USB audio drops are counted too" if usb_found else
         f"{usb_name} not found: only the laptop's internal stall counter is used", False),
    ]


def describe(calibration):
    """One line for the installed calibration (core.calibration_state())."""
    if calibration is None:
        return "Not calibrated yet: the fan service uses its built-in fan table."
    if calibration.get("error"):
        return "The installed fan table cannot be read: " + calibration["error"]
    kind = {"quick": "a quick auto-tune", "full": "a full auto-tune"}.get(calibration.get("preset"), "auto-tune")
    written = calibration.get("written")
    when = " on " + written.astimezone().strftime("%-d %b %Y") if written else ""
    return f"Calibrated with {kind}{when}."


class CalibrationCard(QGroupBox):
    startRequested = Signal(str)     # "quick" or "full"
    dismissed = Signal()             # "Not now": stop suggesting setup
    openCalibrate = Signal()         # show the Calibrate tab

    def __init__(self):
        super().__init__("Fan calibration")
        self.setObjectName("calibrationCard")
        self.suggest = True
        self.expanded = False
        self.mode = None
        self.last = None
        lay = QVBoxLayout(self)
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() * 1.15)
        self.title.setFont(font)
        lay.addWidget(self.title)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.PlainText)
        lay.addWidget(self.text)
        self.checks = QLabel()
        self.checks.setTextFormat(Qt.RichText)
        self.checks.setAccessibleName("Auto-tune readiness")
        lay.addWidget(self.checks)
        row = QHBoxLayout()
        self.quick = QPushButton(f"Quick auto-tune · about {QUICK_MINUTES} min")
        self.quick.setObjectName("primaryAction")
        self.quick.clicked.connect(lambda: self.startRequested.emit("quick"))
        self.full = QPushButton(f"Full auto-tune · about {FULL_MINUTES} min")
        self.full.clicked.connect(lambda: self.startRequested.emit("full"))
        self.later = QPushButton("Not now")
        self.later.clicked.connect(self.not_now)
        self.open = QPushButton("Calibrate…")
        self.open.clicked.connect(self.expand)
        self.show_tab = QPushButton("Show progress")
        self.show_tab.clicked.connect(self.openCalibrate.emit)
        for b in (self.quick, self.full, self.later, self.open, self.show_tab):
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setEnabled(False)
        lay.addWidget(self.note)

    def not_now(self):
        if self.expanded:
            self.expanded = False          # opened by hand: just close it again
        else:
            self.dismissed.emit()
        self.redraw()

    def expand(self):
        self.expanded = True
        self.redraw()

    def set_suggest(self, on):
        self.suggest = bool(on)
        self.redraw()

    def show_state(self, calibration, checks, running=False, proposal=False, dry_run=False):
        self.last = (calibration, checks, running, proposal, dry_run)
        self.redraw()

    def redraw(self):
        if self.last is None:
            return
        calibration, checks, running, proposal, dry_run = self.last
        if running:
            mode = "running"
        elif proposal:
            mode = "proposal"
        elif self.expanded or (calibration is None and self.suggest):
            mode = "setup"
        else:
            mode = "compact"
        self.mode = mode
        setup = mode == "setup"
        self.setProperty("prominent", mode in ("setup", "proposal"))
        self.style().unpolish(self)
        self.style().polish(self)
        for w in (self.quick, self.full, self.later, self.checks):
            w.setVisible(setup)
        self.open.setVisible(mode == "compact")
        self.show_tab.setVisible(mode in ("running", "proposal"))
        self.show_tab.setText("Review proposal" if mode == "proposal" else "Show progress")
        self.show_tab.setObjectName("primaryAction" if mode == "proposal" else "")
        self.show_tab.style().unpolish(self.show_tab)
        self.show_tab.style().polish(self.show_tab)
        self.title.setVisible(mode != "compact")
        self.note.setVisible(setup)
        if mode == "running":
            self.title.setText("Auto-tune is running")
            self.text.setText("The fans stay loud and the laptop speakers stay blocked until it finishes. "
                              "Follow it or cancel it in the Calibrate tab; cancelling restores your fan curves.")
        elif mode == "proposal":
            self.title.setText("Auto-tune has finished")
            self.text.setText("Review the proposed fan table and write it to finish calibration.")
        elif mode == "compact":
            self.text.setText(describe(calibration))
        else:
            self.title.setText("Finish setup: calibrate the fans for this laptop" if calibration is None
                               else "Calibrate the fans again")
            self.text.setText(
                "Auto-tune heats the laptop by itself and steps the fans from loud to quiet to find the "
                "quietest mix that keeps audio clean, then shows the fan table it proposes before anything is "
                f"written. Quick (about {QUICK_MINUTES} min) tests three heat levels with coarser steps, like "
                f"the first hand calibration. Full (about {FULL_MINUTES} min) tests four levels with finer "
                "steps and more tries at moving air away from the whiny middle fan. Both are loud.")
            self.later.setText("Close" if self.expanded else "Not now")
            lines = []
            for ok, text, required in checks:
                mark, colour = (("✓", STATUS["good"]) if ok else ("✗", STATUS["hot"]) if required
                                else ("–", None))
                style = f' style="color:{colour}"' if colour else ""
                lines.append(f"<span{style}>{mark}</span>&nbsp; {escape(text, quote=False)}")
            self.checks.setText("<br>".join(lines))
            ready = all(ok for ok, _, required in checks if required)
            self.quick.setEnabled(ready or dry_run)
            self.full.setEnabled(ready or dry_run)
            self.note.setText(
                ("Fix the items marked ✗ to start. " if not ready and not dry_run else "")
                + "Close games and other heavy apps first. The fan service switches to Off for the run and "
                  "back afterwards; any stop or cancel restores your fan curves.")
