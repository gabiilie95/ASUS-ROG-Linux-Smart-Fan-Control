"""Every sensor the panel can read without root, for the Overview tab.

Reads the hwmon chips, the ACPI thermal zones that have no hwmon twin, the power supplies and the
CPU clocks from sysfs, and keeps five minutes of history per sensor plus the lowest and highest
value since the panel opened. A scan takes about 10 ms on the G814JZ (the NVMe and per-core reads
are the slow part), so the panel runs it on a worker thread and only while its window is shown.
Nothing here writes anything.
"""
from collections import deque
import os
from pathlib import Path
import re

GROUPS = ("CPU", "GPU", "Board", "Fans", "Power", "Other")
# hwmon input kind: (divisor, unit)
SCALE = {"temp": (1000, "°C"), "fan": (1, "rpm"), "in": (1000, "V"), "curr": (1000, "A"),
         "power": (1_000_000, "W")}
KIND_ORDER = ("temp", "fan", "power", "in", "curr")
# Attention levels (warm, hot) in C where sysfs limits are missing or misleading: coretemp's max is
# TjMax itself, and spd5118 reports its 55 C alarm default as "max". CPU 95 and GPU 85 are also
# where auto-tune stops.
LIMITS = {"cpu": (90, 95), "gpu": (80, 85), "memory": (75, 85)}
FAN_NAMES = {"cpu_fan": ("cpu_fan", "CPU fan"), "gpu_fan": ("gpu_fan", "GPU fan"),
             "mid_fan": ("mid_fan", "Middle fan")}
# ACPI thermal zones without an hwmon twin: type -> (id, group, name).
ZONES = {"SEN3": ("sen3", "Board", "Board · SEN3"), "TCPU": ("tcpu", "CPU", "CPU · platform"),
         "TCPU_PCI": ("tcpu_pci", "CPU", "CPU PCI · platform"),
         "x86_pkg_temp": ("x86_pkg", "CPU", "Package · x86_pkg_temp")}
SKIP_ZONES = {"INT3400 Thermal"}    # the DPTF participant: a fixed placeholder, not a measurement
CORE_RE = re.compile(r"Core (\d+)")


def _read(path):
    try:
        return path.read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def _int(path):
    try:
        return int(_read(path))
    except (TypeError, ValueError):
        return None


def _celsius(raw):
    """Millidegrees to C, or None outside -40..150 C (unset limits read as huge or negative)."""
    return None if raw is None or not -40000 <= raw <= 150000 else round(raw / 1000, 2)


def _order(path):
    prefix = path.name[:-len("_input")]
    kind = prefix.rstrip("0123456789")
    number = prefix[len(kind):]
    return (KIND_ORDER.index(kind) if kind in KIND_ORDER else 9, int(number) if number.isdigit() else 0)


def reading(rid, group, name, value, unit, source, warm=None, hot=None):
    return dict(id=rid, group=group, name=name, value=value, unit=unit, source=source, warm=warm, hot=hot)


def _limits(directory, prefix, preset=None):
    if preset:
        return LIMITS[preset]
    high, crit = _celsius(_int(directory / f"{prefix}_max")), _celsius(_int(directory / f"{prefix}_crit"))
    warm = high if high is not None and (crit is None or high < crit) else None
    return warm, crit


def _describe(chip, device, prefix, kind, label, dimms):
    """(id, group, name, limit preset) for one hwmon input; known parts get stable names."""
    if chip == "coretemp":
        if label and label.startswith("Package"):
            return "cpu_pkg", "CPU", "Package", "cpu"
        core = CORE_RE.fullmatch(label or "")
        if core:
            return f"cpu_core{core[1]}", "CPU", f"Core {core[1]}", "cpu"
        return f"coretemp/{prefix}", "CPU", label or prefix, "cpu"
    if chip == "spd5118" and kind == "temp":
        letter = "AB"[dimms.index(device)] if device in dimms and dimms.index(device) < 2 else device
        return f"dimm_{letter.lower()}", "Board", f"Memory {letter}", "memory"
    if chip == "nvme" and device.startswith("nvme") and device[4:].isdigit():
        n = int(device[4:]) + 1
        if label in (None, "Composite") and prefix == "temp1":
            return f"ssd{n}", "Board", f"SSD {n}", None
        return f"ssd{n}_{prefix}", "Board", f"SSD {n} · {label or prefix}", None
    if chip.startswith("r8169") and kind == "temp":
        return "lan", "Board", "Ethernet", None
    if chip.startswith("iwlwifi") and kind == "temp":
        return "wifi", "Board", "Wi-Fi", None
    if chip == "acpitz" and kind == "temp":
        return ("acpitz" if prefix == "temp1" else f"acpitz_{prefix}"), "Board", "ACPI zone", None
    if chip == "asus" and kind == "fan" and label in FAN_NAMES:
        rid, name = FAN_NAMES[label]
        return rid, "Fans", name, None
    if chip.startswith("BAT"):
        names = {"in": ("bat_v", "Battery voltage"), "power": ("bat_w", "Battery power"),
                 "curr": ("bat_a", "Battery current")}
        if kind in names:
            return (*names[kind][:1], "Power", names[kind][1], None)
    if chip.startswith("ucsi"):
        names = {"in": ("usbc_v", "USB-C voltage"), "curr": ("usbc_a", "USB-C current")}
        if kind in names:
            return (*names[kind][:1], "Power", names[kind][1], None)
    return f"{chip}@{device}/{prefix}", "Other", f"{chip} · {label or prefix}", None


def hwmon_readings(root):
    chips = []
    for d in root.glob("hwmon*"):
        device = os.path.basename(os.path.realpath(d / "device")) if (d / "device").exists() else d.name
        chips.append((_read(d / "name") or "unknown", device, d))
    chips.sort()
    dimms = sorted(device for chip, device, _ in chips if chip == "spd5118")
    out, twins = [], set()
    for chip, device, d in chips:
        twins.add(device)
        for inp in sorted(d.glob("*_input"), key=_order):
            prefix = inp.name[:-len("_input")]
            kind = prefix.rstrip("0123456789")
            if kind not in SCALE:
                continue
            raw = _int(inp)
            divisor, unit = SCALE[kind]
            label = _read(d / f"{prefix}_label")
            rid, group, name, preset = _describe(chip, device, prefix, kind, label, dimms)
            if kind == "temp":
                value = _celsius(raw)
                warm, hot = _limits(d, prefix, preset)
            else:
                value = None if raw is None else round(raw / divisor, 3)
                warm = hot = None
            source = f"{chip} · {label or prefix}"
            out.append(reading(rid, group, name, value, unit, source, warm, hot))
    return out, twins


def zone_readings(root, twins=()):
    out = []
    for z in sorted(root.glob("thermal_zone*"), key=lambda p: int(p.name[12:] or 0)):
        kind = _read(z / "type")
        if not kind or kind in SKIP_ZONES or z.name in twins:
            continue
        trips = {}
        for t in z.glob("trip_point_*_type"):
            value = _celsius(_int(t.with_name(t.name[:-len("type")] + "temp")))
            if value is not None and value > 0:
                trips.setdefault(_read(t), value)
        rid, group, name = ZONES.get(kind, (f"zone/{kind}", "Other", kind))
        preset = LIMITS["cpu"] if group == "CPU" else (trips.get("hot"), trips.get("critical"))
        out.append(reading(rid, group, name, _celsius(_int(z / "temp")), "°C", f"thermal zone · {kind}",
                           *preset))
    return out


def power_state(root):
    """{"ac": True/False/None, "battery_status": text or None} and the battery charge reading."""
    state, out = {"ac": None, "battery_status": None}, []
    for supply in sorted(root.glob("*")):
        kind = _read(supply / "type")
        if kind == "Mains":
            online = _read(supply / "online")
            if online in ("0", "1"):
                state["ac"] = state["ac"] or online == "1"
        elif kind == "Battery":
            state["battery_status"] = state["battery_status"] or _read(supply / "status")
            pct = _int(supply / "capacity")
            if pct is not None and not any(r["id"] == "bat_pct" for r in out):
                out.append(reading("bat_pct", "Power", "Battery charge", pct, "%", f"{supply.name} · capacity"))
    return state, out


def clock_readings(root):
    khz = [_int(p) for p in root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq")]
    khz = [v for v in khz if v]
    if not khz:
        return []
    return [reading("cpu_clock_avg", "CPU", "Clock · average", round(sum(khz) / len(khz) / 1e6, 2), "GHz",
                    "cpufreq · %d cores" % len(khz)),
            reading("cpu_clock_max", "CPU", "Clock · fastest core", round(max(khz) / 1e6, 2), "GHz",
                    "cpufreq · %d cores" % len(khz))]


def scan(sys_root=Path("/sys")):
    """{"readings": [...], "ac": ..., "battery_status": ...}. Each reading: id, group, name, value
    (None when unreadable), unit, source (chip and label as sysfs names them), warm and hot (C)."""
    sys_root = Path(sys_root)
    readings, twins = hwmon_readings(sys_root / "class" / "hwmon")
    readings += zone_readings(sys_root / "class" / "thermal", twins)
    state, battery = power_state(sys_root / "class" / "power_supply")
    readings += battery + clock_readings(sys_root / "devices" / "system" / "cpu")
    cores = [r for r in readings if r["id"].startswith("cpu_core") and r["value"] is not None]
    if cores:
        hottest = max(cores, key=lambda r: r["value"])
        readings.append(reading("cpu_core_max", "CPU", "Hottest core", hottest["value"], "°C",
                                "coretemp · " + hottest["name"], *LIMITS["cpu"]))
    seen = set()
    for r in readings:             # two chips of one kind would otherwise share an id
        base, n = r["id"], 2
        while r["id"] in seen:
            r["id"] = f"{base}#{n}"
            n += 1
        seen.add(r["id"])
    return dict(readings=readings, **state)


def level(r):
    """None, "warm" or "hot" for a reading against its limits."""
    v = r.get("value")
    if not isinstance(v, (int, float)):
        return None
    if r.get("hot") is not None and v >= r["hot"]:
        return "hot"
    if r.get("warm") is not None and v >= r["warm"]:
        return "warm"
    return None


def fmt(value, unit):
    if value is None:
        return "—"
    if unit == "rpm":
        return f"{value:,.0f} rpm"
    if unit in ("%", "W") and abs(value) >= 10:
        return f"{value:.0f} {unit}"
    if unit in ("V", "A", "GHz", "W"):
        return f"{value:.1f} {unit}" if unit != "GHz" else f"{value:.2f} {unit}"
    return f"{value:.1f} {unit}" if isinstance(value, float) and value != int(value) else f"{value:g} {unit}"


class SensorHistory:
    """Five minutes of values per sensor id, and the range seen since the panel opened."""

    def __init__(self, window=300):
        self.window = window
        self.points = {}
        self.low, self.high, self.info = {}, {}, {}

    def add(self, t, readings):
        for r in readings:
            rid, value = r["id"], r["value"]
            self.info[rid] = r
            self.points.setdefault(rid, deque()).append((t, value))
            if isinstance(value, (int, float)):
                self.low[rid] = min(self.low.get(rid, value), value)
                self.high[rid] = max(self.high.get(rid, value), value)
        for q in self.points.values():
            while q and t - q[0][0] > self.window:
                q.popleft()

    def series(self, rid):
        return list(self.points.get(rid, ()))

    def latest(self, rid):
        q = self.points.get(rid)
        return q[-1][1] if q else None

    def clear(self):
        self.points.clear()
