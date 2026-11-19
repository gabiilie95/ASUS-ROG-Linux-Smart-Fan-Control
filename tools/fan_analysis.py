"""Conservative, observational summaries of a single fan calibration step.

Heuristics, not calibrated physical constants: valid samples must span >=80%
of a window, retain >=80% of nominal 2-second samples, and have no gap over
6 seconds. RPM must stay within 200 RPM or 5% for
30 seconds. rpm_settled_s is the CONFIRMATION time, 30 seconds into a
steady RPM window, not when the fans first reached speed. Thermal windows
span 60 seconds; equilibrium is +/-0.3 C/min.
Cooling onset requires two consecutive 30-second windows falling by >=0.5 C
relative to their first ten seconds, plus a negative robust slope. It is an
observed response after RPM stabilization, never a causal fan coefficient.
The returned onset is the START of the earliest confirmed window pair in step
elapsed seconds; confirmation arrives 60 seconds later. It estimates onset to
window resolution and is not an exact physical response delay. The accompanying
cooling_onset_c is the median temperature of the window's first ten seconds.
CPU watts are optional, but unknown/stale power prevents load attribution.
Optional cpu_w_age_s must be <=6 seconds; cpu_w_stale also rejects a reading.
Without freshness metadata, supplied CPU watts are assumed fresh. Equilibrium
also needs 90 seconds of temperature coverage and a <=1.5 C central range.
"""
import math
from bisect import bisect_left, bisect_right
import statistics

SENSORS = ("board_c", "cpu_c", "gpu_c")
FANS = ("cpu_rpm", "gpu_rpm", "mid_rpm")
# The shipped base curves reach full speed here. Never prolong a flat-fan
# experiment near these temperatures merely to collect a cleaner estimate.
EXTENSION_LIMITS = {"cpu_c": 88, "gpu_c": 86}


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


class Points(list):
    """Sorted telemetry with cached timestamps for logarithmic window lookup."""
    def __init__(self, values=()):
        super().__init__(values)
        self.times = [t for t, _ in self]


def covered(points, start, end):
    """Reject sparse windows rather than interpolate through missing telemetry."""
    return (len(points) >= max(10, .8*(end-start)/2) and end > start
            and points[-1][0] - points[0][0] >= .8 * (end-start)
            and points[0][0] <= start + 6 and points[-1][0] >= end - 6
            and all(0 < b[0]-a[0] <= 6 for a, b in zip(points, points[1:])))


def window(points, start, end):
    return points[bisect_left(points.times, start):bisect_right(points.times, end)]


def slope(points):
    """Theil-Sen slope, ignoring close pairs to resist sensor quantization."""
    values = [(b[1]-a[1])*60/(b[0]-a[0]) for i, a in enumerate(points)
              for b in points[i+1:] if b[0]-a[0] >= 15]
    return statistics.median(values) if values else None


def stable_power(points):
    values = [v for _, v in points]
    ordered = sorted(values)
    lo, hi = ordered[int(.1*(len(ordered)-1))], ordered[int(.9*(len(ordered)-1))]
    limit = max(8, .2 * statistics.median(values))
    edge = max(3, len(points)//4)
    # Percentiles miss a short transition near the end. Four or more successive
    # 2-second observations outside the band reject it; isolated spikes do not.
    center = statistics.median(values)
    began = None
    side = 0
    for t, value in points:
        direction = 1 if value > center+limit else -1 if value < center-limit else 0
        if not direction or direction != side:
            began = t if direction else None
            side = direction
        elif t-began >= 6:
            return False
    return (hi-lo <= limit and abs(statistics.median(values[:edge])
            - statistics.median(values[-edge:])) <= limit)


def rpm_suffix(points, end):
    """O(n) suffix facts avoid rescanning every later RPM sample per candidate."""
    n = len(points)
    lo, hi, gap = [0]*n, [0]*n, [0]*n
    for i in range(n-1, -1, -1):
        value = points[i][1]
        lo[i] = min(value, lo[i+1]) if i+1<n else value
        hi[i] = max(value, hi[i+1]) if i+1<n else value
        gap[i] = max(gap[i+1], points[i+1][0]-points[i][0]) if i+1<n else 0
        if i+1<n and points[i+1][0] <= points[i][0]:
            gap[i] = math.inf
    def valid(start):
        i = bisect_left(points.times, start)
        if i == n:
            return False
        duration = end-start
        return (duration >= 30 and n-i >= max(10, .8*duration/2)
                and points[-1][0]-points[i][0] >= .8*duration
                and points[i][0] <= start+6 and points[-1][0] >= end-6
                and gap[i] <= 6 and hi[i]-lo[i] <= max(250, .06*lo[i]))
    return valid


def analyze_step(readings, *, observe=False):
    result = dict(status="short", reason="More samples are needed.", load_stable=None,
                  rpm_settled_s=None, sensors={k: dict(slope_c_min=None,
                  cooling_onset_s=None, cooling_onset_c=None, trend="unknown") for k in SENSORS},
                  audio_clean=None, can_extend=False)
    rows = []
    for elapsed, row in readings:
        t = number(elapsed)
        if t is not None and t >= 0 and isinstance(row, dict):
            rows.append((t, row))
    rows.sort(key=lambda p: p[0])
    if not rows:
        return result
    start, end = rows[0][0], rows[-1][0]
    def series(key):
        points = []
        for t, row in rows:
            v = number(row.get(key))
            if key == "cpu_w":
                age = number(row.get("cpu_w_age_s", 0))
                if row.get("cpu_w_stale") or age is None or not 0 <= age <= 6:
                    v = None
            if v is not None and (key not in FANS or v >= 0):
                points.append((t, v))
        return Points(points)
    # A missing counter or invalid rate cannot count as evidence of clean audio.
    rates = [(t, number(r.get("stalls_per_s"))) for t, r in rows]
    valid_audio = all(r.get("counter_ok") in (1, True, "1") and v is not None and v >= 0
                      for (_, r), (_, v) in zip(rows, rates))
    if any(v is not None and v > 1 for _, v in rates):
        result["audio_clean"] = False
    elif valid_audio and covered([(t, v) for t, v in rates], start, end):
        result["audio_clean"] = True
    if end-start < 60:
        if observe:
            result.update(status="observe", reason="Observing; more samples are needed.")
        return result
    powers = [series("gpu_w"), series("cpu_w")]
    power_complete = all(covered(p, start, end) and all(v >= 0 for _, v in p) for p in powers)
    known_change = any(covered(p, start, end) and not stable_power(p) for p in powers)
    result["load_stable"] = False if known_change else True if power_complete else None
    rpms = {k: series(k) for k in FANS}
    # Use the earliest sustained stable RPM window; reject later speed excursions.
    settled = None
    suffix_checks = [rpm_suffix(p, end) for p in rpms.values()]
    for candidate, _ in rows:
        if candidate-start < 30:
            continue
        slices = [window(p, candidate-30, candidate) for p in rpms.values()]
        if all(covered(p, candidate-30, candidate) and
               max(v for _, v in p)-min(v for _, v in p) <= max(200, .05*statistics.median(v for _, v in p))
               for p in slices):
            if all(check(candidate) for check in suffix_checks):
                settled = candidate
                break
    result["rpm_settled_s"] = settled
    good_temps = True
    quiet_temps = True
    for key in SENSORS:
        points = series(key)
        recent = window(points, end-60, end)
        sensor = result["sensors"][key]
        if not covered(recent, end-60, end):
            good_temps = False
            continue
        if settled is not None and end-settled >= 90:
            if not covered(window(points, end-90, end), end-90, end):
                good_temps = False
        ordered = sorted(v for _, v in recent)
        if ordered[int(.9*(len(ordered)-1))]-ordered[int(.1*(len(ordered)-1))] > 1.5:
            quiet_temps = False
        s = slope(recent)
        sensor["slope_c_min"] = round(s, 3) if s is not None else None
        sensor["trend"] = "cooling" if s is not None and s < -.3 else "warming" if s is not None and s > .3 else "steady"
        if settled is None or result["load_stable"] is not True:
            continue
        # Two adjacent 30s windows must both show sustained, meaningful cooling.
        for at, _ in points:
            if at < settled or at+60 > end:
                continue
            chunks = [window(points, at+j, at+j+30) for j in (0, 30)]
            falling = True
            for j, chunk in zip((0, 30), chunks):
                if not covered(chunk, at+j, at+j+30):
                    falling = False
                    break
                early = [v for t, v in chunk if t <= at+j+10]
                late = [v for t, v in chunk if t >= at+j+20]
                if not early or not late or statistics.median(early)-statistics.median(late) < .5 or slope(chunk) >= -.3:
                    falling = False
                    break
            if falling:
                sensor["cooling_onset_s"] = round(at, 2)
                sensor["cooling_onset_c"] = round(statistics.median(
                    v for t, v in chunks[0] if t <= at+10), 2)
                break
    if known_change:
        result.update(status="load_changed", reason="Power changed; temperature movement cannot be attributed to this fan step.")
    elif not power_complete:
        result.update(status="missing_data", reason="Fresh CPU and GPU power are needed to compare cooling under steady load.")
    elif settled is None:
        result.update(status="rpm_unstable", reason="Actual fan speeds have not demonstrated sustained stability.")
    elif not good_temps:
        result.update(status="missing_data", reason="Temperature samples have missing values or gaps.")
    else:
        trends = [v["trend"] for v in result["sensors"].values()]
        if "warming" in trends:
            result.update(status="warming", reason="At least one temperature is still rising under steady load.")
        elif "cooling" in trends:
            result.update(status="cooling", reason="Temperatures are still falling under steady load.")
        elif end-settled >= 90 and quiet_temps:
            result.update(status="settled", reason="Temperatures are approximately steady after fan stabilization; audio is judged separately.")
        else:
            result.update(status="unsettled", reason="More stable temperature history after fan stabilization is needed to confirm equilibrium.")
        result["can_extend"] = (result["status"] in ("warming", "cooling")
                                and result["audio_clean"] is True and not observe)
        if result["can_extend"] and any(
                any(v >= limit for _, v in window(series(key), end-30, end))
                for key, limit in EXTENSION_LIMITS.items()):
            result["can_extend"] = False
            result["reason"] += " Near full-speed curve temperatures; extra settling is skipped."
    if observe:
        result.update(status="observe", reason="Observational trends only; no controlled fan-response attribution.", can_extend=False)
        for sensor in result["sensors"].values():
            sensor["cooling_onset_s"] = None
            sensor["cooling_onset_c"] = None
    return result
