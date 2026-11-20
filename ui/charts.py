"""Small Qt charts for the Overview: a five-minute time chart with a hover read-out, and sparklines.

Series colours are a validated categorical palette in a fixed slot order (light and dark steps
chosen separately, checked against the panel surfaces), never cycled: a view has at most eight
series. Status colours are reserved for warm/hot readings. Text always uses the window palette's
ink, never a series colour.
"""
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

SERIES = {False: ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"),
          True: ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767")}
STATUS = {"good": "#0ca30c", "warm": "#fab219", "hot": "#d03b3b"}
GAP_S = 8          # a longer pause between samples breaks the line instead of bridging it
LABEL_SERIES = 4   # up to this many series also get a name at the end of their line


def is_dark(widget):
    return widget.palette().color(QPalette.Window).lightness() < 128


def series_color(widget, index):
    return QColor(SERIES[is_dark(widget)][index])


def muted(widget):
    return widget.palette().color(QPalette.PlaceholderText)


def nice_step(span):
    raw = max(span, 1e-9) / 2
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 2.5, 5, 10) if m * power >= raw)


def value_range(values, zero=True, fixed=None):
    """(low, high) for a y axis: fixed, from zero, or around the data (temperatures)."""
    if fixed:
        return fixed
    if not values:
        return (0, 1) if zero else (30, 60)
    lo, hi = min(values), max(values)
    if zero:
        step = nice_step(hi * 1.1 or 1)
        return 0, max(step, math.ceil(hi * 1.1 / step) * step)
    lo, hi = math.floor((lo - 2) / 5) * 5, math.ceil((hi + 2) / 5) * 5
    return (lo, max(hi, lo + 10))


def fmt_axis(v):
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:g}"


class TimeChart(QWidget):
    """Several series over the last `window` seconds on one y axis, newest sample at the right.
    Hovering shows a crosshair and every series' value at that moment."""
    PL, PT, PB = 50, 10, 24

    def __init__(self, parent=None, window=300):
        super().__init__(parent)
        self.window = window
        self.series = []          # [(name, [(t, value or None)])]
        self.unit = ""
        self.zero = True
        self.fixed = None
        self.empty = "Waiting for samples"
        self.hover = None         # t under the mouse
        self.setMouseTracking(True)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setAccessibleName("Five-minute chart")

    def sizeHint(self):
        return QSize(600, 190)

    def set_series(self, series, unit, zero=True, fixed=None, empty="Waiting for samples"):
        self.series, self.unit, self.zero, self.fixed, self.empty = list(series), unit, zero, fixed, empty
        self.update()

    def end(self):
        return max((pts[-1][0] for _, pts in self.series if pts), default=None)

    def values(self):
        return [v for _, pts in self.series for _, v in pts if v is not None]

    def label_width(self):
        if not 0 < len(self.series) <= LABEL_SERIES:
            return 12
        fm = QFontMetricsF(self.font())
        return 16 + max(fm.horizontalAdvance(name) for name, _ in self.series)

    def plot_rect(self):
        return QRectF(self.PL, self.PT, max(1, self.width() - self.PL - self.label_width()),
                      max(1, self.height() - self.PT - self.PB))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        r = self.plot_rect()
        p.fillRect(r, pal.base())
        ink, quiet = pal.color(QPalette.Text), muted(self)
        grid = QColor(pal.color(QPalette.Mid))
        grid.setAlpha(110)
        values = self.values()
        lo, hi = value_range(values, self.zero, self.fixed)
        for frac in (0, 0.5, 1):
            y = r.bottom() - frac * r.height()
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(quiet)
            p.drawText(QRectF(0, y - 9, self.PL - 6, 18), Qt.AlignRight | Qt.AlignVCenter,
                       fmt_axis(lo + frac * (hi - lo)))
        p.drawText(QRectF(r.left(), r.bottom() + 4, 80, 18), Qt.AlignLeft, "−5 min")
        p.drawText(QRectF(r.center().x() - 40, r.bottom() + 4, 80, 18), Qt.AlignCenter, "−2.5 min")
        p.drawText(QRectF(r.right() - 60, r.bottom() + 4, 60, 18), Qt.AlignRight, "Now")
        end = self.end()
        if not values or end is None:
            p.setPen(ink)
            p.drawText(r, Qt.AlignCenter, self.empty if not values and end is None else "Reading unavailable")
            p.end()
            return
        x_of = lambda t: r.right() - (end - t) * r.width() / self.window
        y_of = lambda v: r.bottom() - (min(max(v, lo), hi) - lo) * r.height() / ((hi - lo) or 1)
        ends = []
        for i, (name, pts) in enumerate(self.series):
            color = series_color(self, i)
            p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            previous = None
            for t, v in pts:
                if v is None or end - t > self.window:
                    previous = None
                    continue
                pt = QPointF(x_of(t), y_of(v))
                if previous is not None and t - previous[0] <= GAP_S:
                    p.drawLine(previous[1], pt)
                else:
                    p.drawPoint(pt)
                previous = t, pt
            last = next((v for _, v in reversed(pts) if v is not None), None)
            if last is not None:
                ends.append([y_of(last), name, color])
        if 0 < len(self.series) <= LABEL_SERIES:
            self.draw_labels(p, r, ends, ink)
        if self.hover is not None:
            self.draw_hover(p, r, x_of, y_of)
        p.end()

    def draw_labels(self, p, r, ends, ink):
        """Series names at the right end of their lines, nudged apart so none overlap."""
        height = QFontMetricsF(self.font()).height()
        ends.sort(key=lambda e: e[0])
        for a, b in zip(ends, ends[1:]):
            b[0] = max(b[0], a[0] + height)
        overflow = ends[-1][0] - (r.bottom() + height / 2) if ends else 0
        for e in ends:
            y = e[0] - max(0, overflow)
            p.setPen(Qt.NoPen)
            p.setBrush(e[2])
            p.drawEllipse(QPointF(r.right() + 7, y), 3, 3)
            p.setPen(ink)
            p.drawText(QRectF(r.right() + 13, y - height / 2, self.width() - r.right() - 13, height),
                       Qt.AlignLeft | Qt.AlignVCenter, e[1])

    def nearest(self, pts, t):
        best = None
        for pt_t, v in pts:
            if v is not None and (best is None or abs(pt_t - t) < abs(best[0] - t)):
                best = pt_t, v
        return best if best and abs(best[0] - t) <= GAP_S else None

    def draw_hover(self, p, r, x_of, y_of):
        x = x_of(self.hover)
        line = QColor(self.palette().color(QPalette.Text))
        line.setAlpha(90)
        p.setPen(QPen(line, 1))
        p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
        for i, (_, pts) in enumerate(self.series):
            hit = self.nearest(pts, self.hover)
            if hit:
                p.setPen(QPen(self.palette().color(QPalette.Base), 2))
                p.setBrush(series_color(self, i))
                p.drawEllipse(QPointF(x_of(hit[0]), y_of(hit[1])), 4, 4)

    def readout(self, t):
        end = self.end()
        lines = ["%s ago" % ("%d:%02d" % divmod(round(end - t), 60)) if end - t >= 1 else "Now"]
        for name, pts in self.series:
            hit = self.nearest(pts, t)
            value = "—" if hit is None else (f"{hit[1]:,.0f}" if abs(hit[1]) >= 100 or hit[1] == int(hit[1])
                                             else f"{hit[1]:.1f}")
            lines.append(f"{name}: {value} {self.unit}")
        return "\n".join(lines)

    def mouseMoveEvent(self, e):
        r, end = self.plot_rect(), self.end()
        x = e.position().x()
        if end is None or not r.left() <= x <= r.right():
            self.leaveEvent(None)
            return
        self.hover = end - (r.right() - x) * self.window / r.width()
        QToolTip.showText(e.globalPosition().toPoint(), self.readout(self.hover), self)
        self.update()

    def leaveEvent(self, _e):
        if self.hover is not None:
            self.hover = None
            QToolTip.hideText()
            self.update()


class Sparkline(QWidget):
    """One sensor's last five minutes, scaled to its own range; a dot marks the newest value."""

    def __init__(self, parent=None, window=300):
        super().__init__(parent)
        self.window = window
        self.points = []
        self.level = None
        self.setFixedSize(84, 20)

    def set_points(self, points, level=None):
        self.points = [(t, v) for t, v in points if v is not None]
        self.level = level
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        track = QColor(self.palette().color(QPalette.Mid))
        track.setAlpha(90)          # the five-minute frame, so a young history reads as filling in
        p.setPen(QPen(track, 1))
        p.drawLine(QPointF(3, self.height() - 2.5), QPointF(self.width() - 3, self.height() - 2.5))
        if len(self.points) < 2:
            p.end()
            return
        end = self.points[-1][0]
        values = [v for _, v in self.points]
        lo, hi = min(values), max(values)
        if hi - lo < 1e-9:
            lo, hi = lo - 1, hi + 1
        w, h = self.width() - 6, self.height() - 6
        pts = [QPointF(3 + w - (end - t) * w / self.window, 3 + h - (v - lo) * h / (hi - lo))
               for t, v in self.points if end - t <= self.window]
        p.setPen(QPen(series_color(self, 0), 1.5, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        for a, b in zip(pts, pts[1:]):
            p.drawLine(a, b)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(STATUS[self.level]) if self.level else series_color(self, 0))
        p.drawEllipse(pts[-1], 2.5, 2.5)
        p.end()
