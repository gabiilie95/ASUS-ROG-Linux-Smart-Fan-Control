#!/usr/bin/env python3
"""Shared logic for the ROG G18 settings panels (web and Qt): readers, presets, validation, whitelisted actions. Standard library only."""
import glob, json, os, re, subprocess, threading, time
import os as _os
# The laptop's own sound chip is ALSA card "PCH"; its number changes between boots
# (it was card 0 until 2026-11-16, then card 1), so look it up instead of assuming card 0.
HDA_LABEL = "snd_hda_intel:" + (_os.path.basename(_os.readlink("/proc/asound/PCH"))
                                if _os.path.islink("/proc/asound/PCH") else "card0")

UI = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(UI)
TOOLS = os.path.join(ROOT, "tools")
SETUP = os.path.join(ROOT, "root-tests", "setup-cool-performance.sh")
USB_DROP = os.path.join(ROOT, "root-tests", "usb-drop-count.sh")
STALL_STATUS = "/proc/asound/PCH/pcm0p/sub0/status"
PSTATE = "/sys/devices/system/cpu/intel_pstate"
CPUFREQ = "/sys/devices/system/cpu/cpu0/cpufreq"

# Preset -> (script, description, curves {fan: [(temp C, pwm 0-255), ...]}) as written in the scripts.
def _c(s):
    return [(int(a[:-1]), int(b)) for a, b in (p.split(":") for p in s.split(","))]

_CPU_ORIG = _c("40c:20,58c:35,62c:53,66c:76,70c:109,74c:147,78c:170,88c:216")
_GPU_ORIG = _c("40c:33,58c:38,62c:68,66c:73,70c:104,74c:130,78c:153,88c:178")
_MID_ORIG = _c("40c:35,58c:58,62c:71,66c:84,70c:94,74c:104,78c:112,88c:112")
_CPU_MAX = _c("40c:20,58c:35,62c:53,66c:150,70c:255,74c:255,78c:255,88c:255")
_GPU_MAX = _c("40c:33,58c:38,62c:68,66c:150,70c:255,74c:255,78c:255,88c:255")
_MID_MAX = _c("40c:35,58c:58,62c:71,66c:150,70c:255,74c:255,78c:255,88c:255")
_GPU_RAMP = _c("40c:33,58c:38,62c:68,66c:90,70c:130,74c:170,77c:215,80c:255")
_CPU_BAL = _c("40c:20,58c:35,62c:53,66c:90,70c:150,72c:204,80c:204,88c:255")
_GPU_BAL = _c("40c:33,58c:38,62c:68,66c:100,70c:153,72c:204,82c:204,86c:255")
_MID_BAL = _c("40c:35,52c:58,58c:153,62c:178,70c:178,72c:204,80c:204,86c:255")
PRESETS = {
    "balanced": ("fans-balanced.sh", "Recommended (calibrated 2026-11-16): airflow spread over all three fans, 80 % from 72 C, full only from 86-88 C",
                 {1: _CPU_BAL, 2: _GPU_BAL, 3: _MID_BAL}),
    "original": ("fans-original.sh", "Factory-like, quiet; crackle returns under load",
                 {1: _CPU_ORIG, 2: _GPU_ORIG, 3: _MID_ORIG}),
    "ramped": ("fans-ramped.sh", "CPU original, GPU full at 80 C, middle full from 70 C (louder)",
               {1: _CPU_ORIG, 2: _GPU_RAMP, 3: _MID_MAX}),
    "mid-max": ("fans-mid-max.sh", "Middle fan full only",
                {1: _CPU_ORIG, 2: _GPU_ORIG, 3: _MID_MAX}),
    "max": ("fans-max.sh", "All three full from 70 C; always clean, loudest",
            {1: _CPU_MAX, 2: _GPU_MAX, 3: _MID_MAX}),
}
FAN_NAMES = {1: "cpu", 2: "gpu", 3: "mid"}
FANS = ("cpu", "gpu", "mid")
PROFILES = ("performance", "balanced", "quiet")
FAN_RON = "/etc/asusd/fan_curves.ron"
PRESET_FILE = os.path.join(UI, "custom-presets.json")
MAX_PRESETS = 20
NAME_RE = re.compile(r"[A-Za-z0-9 _-]{1,32}")

def validate_points(points, label="points"):
    """Return a clean list of 8 (temp, pwm) int tuples or raise ValueError."""
    if not isinstance(points, list) or len(points) != 8:
        raise ValueError("%s must be a list of exactly 8 [temp, pwm] pairs" % label)
    out = []
    for i, p in enumerate(points, 1):
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            raise ValueError("%s: point %d must be a [temp, pwm] pair" % (label, i))
        for v in p:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError("%s: point %d must contain integers only" % (label, i))
        t, pw = p
        if not 30 <= t <= 100:
            raise ValueError("%s: point %d temperature %d is outside 30-100 C" % (label, i, t))
        if not 0 <= pw <= 255:
            raise ValueError("%s: point %d pwm %d is outside 0-255" % (label, i, pw))
        if out and t <= out[-1][0]:
            raise ValueError("%s: temperatures must be strictly increasing (point %d)" % (label, i))
        if out and pw < out[-1][1]:
            raise ValueError("%s: pwm must not decrease (point %d)" % (label, i))
        out.append((t, pw))
    if out[-1][1] < 102:
        raise ValueError("%s: the last point must be at least 102 pwm (40 %%) so a fan cannot stay nearly off when hot" % label)
    return out

def validate_curves(curves):
    if not isinstance(curves, dict) or set(curves) != set(FANS):
        raise ValueError("curves must be an object with exactly cpu, gpu and mid")
    return {f: validate_points(curves[f], f) for f in FANS}

def read_stored_curves():
    """Parse /etc/asusd/fan_curves.ron into {profile: {fan: {enabled, points}}}; None if unreadable."""
    try:
        with open(FAN_RON) as f:
            txt = f.read()
        heads = list(re.finditer(r"\b(performance|balanced|quiet)\s*:\s*\[", txt))
        out = {}
        for i, h in enumerate(heads):
            seg = txt[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(txt)]
            fans = {}
            for m in re.finditer(r"fan:\s*(CPU|GPU|MID)\s*,\s*pwm:\s*\(([\d,\s]+)\)\s*,\s*temp:\s*\(([\d,\s]+)\)\s*,\s*enabled:\s*(true|false)", seg):
                pw = [int(x) for x in m.group(2).replace(" ", "").strip(",").split(",")]
                tp = [int(x) for x in m.group(3).replace(" ", "").strip(",").split(",")]
                if len(pw) != 8 or len(tp) != 8:
                    return None
                fans[m.group(1).lower()] = {"enabled": m.group(4) == "true", "points": [list(x) for x in zip(tp, pw)]}
            if set(fans) != set(FANS):
                return None
            out[h.group(1)] = fans
        return out if set(out) == set(PROFILES) else None
    except Exception:
        return None

def load_custom_presets():
    try:
        with open(PRESET_FILE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        return {}

def store_custom_presets(d):
    tmp = PRESET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, PRESET_FILE)

def rd(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return None

def rd_int(path):
    v = rd(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None

def run(args, timeout=10, **kw):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, cwd=ROOT, **kw)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return None, str(e)

def hwmon(name):
    for d in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        if rd(d + "/name") == name:
            return d
    return None

def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None

def read_temps():
    out = {"cpu_c": None, "board_c": None}
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t, v = rd(z + "/type"), rd_int(z + "/temp")
        if v is None:
            continue
        if t == "x86_pkg_temp":
            out["cpu_c"] = v / 1000
        elif t == "SEN3":
            out["board_c"] = v / 1000
    out["board_hot"] = out["board_c"] is not None and out["board_c"] >= 85
    return out

def read_fans():
    d = hwmon("asus")
    return {k: (rd_int(d + "/fan%d_input" % i) if d else None) for i, k in ((1, "cpu_rpm"), (2, "gpu_rpm"), (3, "mid_rpm"))}

def read_curves():
    d = hwmon("asus_custom_fan_curve")
    live = {}
    for n in (1, 2, 3):
        pts = []
        if d:
            for m in range(1, 9):
                t = rd_int("%s/pwm%d_auto_point%d_temp" % (d, n, m))
                p = rd_int("%s/pwm%d_auto_point%d_pwm" % (d, n, m))
                if t is None or p is None:
                    pts = None
                    break
                pts.append((t, p))
        live[n] = pts or None
    match = None
    if all(live.values()):
        for name, (_, _, curves) in PRESETS.items():
            if all(live[n] == curves[n] for n in (1, 2, 3)):
                match = name
                break
    fans = {}
    for n in (1, 2, 3):
        pts = live[n]
        fans[FAN_NAMES[n]] = {
            "enabled": rd_int("%s/pwm%d_enable" % (d, n)) if d else None,
            "top_pct": round(max(p for _, p in pts) * 100 / 255) if pts else None,
            "points": pts,
        }
    return {"fans": fans, "matches_preset": match}

# Slow or rarely-changing readings are cached so the 2 s poll stays cheap while a game is running.
_cache = {}

def cached(key, ttl, fn):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = fn()
    _cache[key] = (now, val)
    return val

def _tuned_active():
    _, tuned = run(["tuned-adm", "active"], 5)
    return tuned.replace("Current active profile:", "").strip() if tuned and "Current" in tuned else None

def read_cpu():
    tuned = cached("tuned", 10, _tuned_active)
    return {
        "max_perf_pct": rd_int(PSTATE + "/max_perf_pct"),
        "min_perf_pct": rd_int(PSTATE + "/min_perf_pct"),
        "governor": rd(CPUFREQ + "/scaling_governor"),
        "epp": rd(CPUFREQ + "/energy_performance_preference"),
        "tuned": tuned,
        "platform_profile": rd("/sys/firmware/acpi/platform_profile"),
    }

def read_gpu():
    out = {"temp_c": None, "power_w": None, "util_pct": None, "mem_used_mb": None,
           "mem_total_mb": None, "mem_high": False, "apps": []}
    rc, txt = run(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw,utilization.gpu,memory.used,memory.total",
                   "--format=csv,noheader,nounits"], 8)
    if rc == 0:
        f = [x.strip() for x in txt.strip().splitlines()[0].split(",")]
        if len(f) == 5:
            out.update(temp_c=num(f[0]), power_w=num(f[1]), util_pct=num(f[2]), mem_used_mb=num(f[3]), mem_total_mb=num(f[4]))
            out["mem_high"] = bool(out["mem_used_mb"] and out["mem_total_mb"] and out["mem_used_mb"] > 0.95 * out["mem_total_mb"])
    out["apps"] = cached("gpu_apps", 6, _gpu_apps)
    return out

def _gpu_apps():
    rc, txt = run(["nvidia-smi", "--query-compute-apps=pid,used_memory,name", "--format=csv,noheader,nounits"], 8)
    apps = []
    if rc == 0:
        for line in txt.strip().splitlines():
            f = [x.strip() for x in line.split(",", 2)]
            if len(f) == 3 and num(f[1]) is not None:
                apps.append({"pid": f[0], "mem_mb": num(f[1]), "name": f[2][:80]})
    return sorted(apps, key=lambda a: -a["mem_mb"])[:5]

def read_jellyfin():
    rc, txt = run(["bash", os.path.join(TOOLS, "jellyfin-software-ui.sh"), "status"], 10)
    txt = txt.strip()
    return txt if rc == 0 and txt in ("on", "off") else None

def status():
    return {"temps": read_temps(), "fans": read_fans(), "curves": read_curves(), "cpu": read_cpu(),
            "gpu": read_gpu(), "jellyfin_software_ui": cached("jellyfin", 10, read_jellyfin),
            "presets": {k: v[1] for k, v in PRESETS.items()},
            "preset_curves": {k: {FAN_NAMES[n]: [list(p) for p in c] for n, c in v[2].items()} for k, v in PRESETS.items()},
            "custom_presets": load_custom_presets(),
            "stored_curves": cached("ron", 5, read_stored_curves)}

# ---- actions -------------------------------------------------------------------------------
busy = threading.Lock()

def build_action(name, body):
    """Return (argv, timeout, note) or raise ValueError. Only whitelisted commands exist."""
    if name == "fan-preset":
        p = body.get("preset")
        if not isinstance(p, str) or p not in PRESETS:
            raise ValueError("unknown preset")
        return ["bash", os.path.join(TOOLS, PRESETS[p][0])], 30, None
    if name == "fan-curve":
        prof, fan = body.get("profile"), body.get("fan")
        if not isinstance(prof, str) or prof not in PROFILES:
            raise ValueError("profile must be one of: " + ", ".join(PROFILES))
        if not isinstance(fan, str) or fan not in FANS:
            raise ValueError("fan must be one of: " + ", ".join(FANS))
        pts = validate_points(body.get("points"))
        data = ",".join("%dc:%d" % p for p in pts)
        return [["asusctl", "fan-curve", "--mod-profile", prof, "--fan", fan, "--data", data],
                ["asusctl", "fan-curve", "--mod-profile", prof, "--enable-fan-curves", "true"]], 20, None
    if name == "jellyfin":
        v = body.get("value")
        if v not in ("on", "off"):
            raise ValueError("value must be on or off")
        return ["bash", os.path.join(TOOLS, "jellyfin-software-ui.sh"), v], 30, "Applies at Jellyfin's next start (not restarted)."
    if name == "cpu-cap":
        c = body.get("cap")
        if isinstance(c, bool) or not isinstance(c, int) or not 40 <= c <= 100:
            raise ValueError("cap must be an integer from 40 to 100")
        return ["pkexec", "env", "CAP=%d" % c, "bash", SETUP], 180, "A system password dialog will appear."
    if name == "cpu-undo":
        return ["pkexec", "bash", SETUP, "undo"], 180, "A system password dialog will appear."
    if name == "stall-test":
        return ["python3", "-I", os.path.join(TOOLS, "stall-counter.py"), "10", "panel"], 40, None
    if name == "usb-drop-check":
        return ["pkexec", "bash", USB_DROP, "10"], 90, "A system password dialog will appear. Percent and counts are estimates (usbmon's text interface shows only the first 5 packets of each URB)."
    raise ValueError("unknown action")

def execute_all(cmds, timeout):
    outs = []
    for argv in cmds:
        r = execute("fan-curve", argv, timeout)
        outs.append(r.get("output", ""))
        if not r["ok"]:
            r["output"] = "\n".join(o for o in outs if o)
            return r
    return {"ok": True, "returncode": 0, "output": "\n".join(o for o in outs if o) or "Applied."}

def preset_action(name, body):
    if name == "save-preset":
        pn = body.get("name")
        if not isinstance(pn, str) or not NAME_RE.fullmatch(pn) or not pn.strip():
            raise ValueError("name must be 1-32 characters: letters, digits, space, dash, underscore")
        curves = validate_curves(body.get("curves"))
        d = load_custom_presets()
        if pn not in d and len(d) >= MAX_PRESETS:
            raise ValueError("at most %d custom presets; delete one first" % MAX_PRESETS)
        d[pn] = {f: [list(p) for p in curves[f]] for f in FANS}
        store_custom_presets(d)
        return {"ok": True, "output": "Saved preset '%s' (not applied)." % pn}
    pn = body.get("name")
    d = load_custom_presets()
    if not isinstance(pn, str) or pn not in d:
        raise ValueError("no such custom preset")
    del d[pn]
    store_custom_presets(d)
    return {"ok": True, "output": "Deleted preset '%s'." % pn}

def execute(name, argv, timeout):
    if name == "stall-test":
        st = rd(STALL_STATUS)
        if st is not None and "RUNNING" in st:
            return {"ok": False, "output": "internal sound device busy"}
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "timed out after %d s" % timeout}
    except Exception as e:
        return {"ok": False, "output": str(e)}
    out = ((r.stdout or "") + (r.stderr or "")).strip()
    res = {"ok": r.returncode == 0, "returncode": r.returncode, "output": out}
    if name == "stall-test":
        for line in out.splitlines():
            m = re.search(r"DMA/FIFO errors ~(\d+) \(([\d.]+)/s\)", line)
            if m:
                res["stalls_per_s"] = float(m.group(2))
                res["first_line"] = line
                break
            if "=>" in line:
                res["first_line"] = line
                break
    return res


# ---- live stall monitor (no root) and USB drop check parsing ---------------------------------
MONITOR_APLAY = ["aplay", "-q", "-D", "hw:CARD=PCH,DEV=0", "-f", "S16_LE", "-r", "48000", "-c", "2",
                 "--period-size=8192", "--buffer-size=32768", "/dev/zero"]
NORMAL_IRQ_PER_S = 48000 / 8192     # 5.86 period interrupts per second while the silence plays

def sound_device_busy():
    st = rd(STALL_STATUS)
    return st is not None and "RUNNING" in st

def parse_irq_total(text, label=HDA_LABEL):
    """Sum of the per-CPU counts on the /proc/interrupts line for label; None if absent."""
    for line in (text or "").splitlines():
        if label in line:
            total, seen = 0, False
            for tok in line.split(":", 1)[1].split():
                if not tok.isdigit():
                    break
                total += int(tok)
                seen = True
            return total if seen else None
    return None

def read_irq_total():
    return parse_irq_total(rd("/proc/interrupts"))

class StallRate:
    """Turns successive IRQ totals into stalls per second (extra interrupts above the normal period rate).

    The first `warmup` intervals after a (re)start are dropped: while aplay opens the device and
    starts the stream the controller raises a burst of extra interrupts (63.5/s measured on the
    first interval, then about 0), which would otherwise show up as a false peak."""
    def __init__(self, warmup=2):
        self.last = None
        self.warmup = warmup
    def feed(self, total, now):
        prev, self.last = self.last, (total, now)
        if prev is None or total is None or now <= prev[1] or total < prev[0]:
            return None
        if self.warmup > 0:
            self.warmup -= 1
            return None
        return max(0.0, (total - prev[0]) / (now - prev[1]) - NORMAL_IRQ_PER_S)

_KV = re.compile(r'(\w+)=("[^"]*"|\S+)')

def parse_usbdrop(text):
    """Parse usb-drop-count.sh stdout. Returns {"devices": [...], "none": bool, "error": str|None}."""
    out = {"devices": [], "none": False, "error": None}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line.startswith("USBDROP"):
            continue
        rest = line[7:].strip()
        if rest == "none":
            out["none"] = True
        elif rest.startswith("error"):
            m = re.search(r'msg="([^"]*)"', rest)
            out["error"] = m.group(1) if m else rest[5:].strip() or "error"
        else:
            kv = {k: v.strip('"') for k, v in _KV.findall(rest)}
            try:
                out["devices"].append({
                    "name": kv.get("name") or kv.get("path") or "device",
                    "packets": int(kv["packets"]), "skipped": int(kv["skipped"]),
                    "pct": float(kv["pct"]), "events_per_s": float(kv["events_per_s"])})
            except (KeyError, ValueError):
                continue
    return out
