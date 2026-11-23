#!/usr/bin/python3
"""Test and apply PLAYBACK_URB_FIXUP, a USB audio driver option that may shorten audio dropouts.

When this laptop's chipset runs hot, its USB controller misses about a millisecond of
isochronous audio packets at a time. Linux 7.3's snd-usb-audio driver has a quirk flag,
PLAYBACK_URB_FIXUP, that queues the most transfers it can (12) and lets the controller start each
one as soon as it can (SIA) instead of in a fixed frame. A USB kernel developer suggested it for
these dropouts. It may make each dropout shorter, but a missed slot cannot be sent later, so it is
unlikely to remove them. This program measures the difference and applies the flag for one device
(vendor:product), until the next restart or at every start.

  rog_usbaudio.py status [--json]        what the kernel supports and what is set (no root)
  rog_usbaudio.py try 2b5a:0021          on until the next restart (root)
  rog_usbaudio.py keep 2b5a:0021         on now and at every start (root)
  rog_usbaudio.py off 2b5a:0021          off now and at every start (root)
  rog_usbaudio.py test 2b5a:0021 [--phase-s 120]
        While audio plays, count dropouts without the flag, with it, and without it again (root,
        about 10 minutes at the default). Prints one JSON object per line. A line "stop", or the
        end of input when stdin is a pipe, cancels. The device's setting is put back either way.

The driver reads quirk flags only when it binds a device, so each change reconnects the device
(USB authorized 0, then 1): the sound pauses for a second or two. "keep" writes
/etc/modprobe.d/rog-fand-usb-audio.conf; nothing else outside the running kernel is changed.
"""
import argparse
import gzip
import json
import lzma
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

FLAG = "PLAYBACK_URB_FIXUP"
FIRST_KERNEL = "7.3"
CONF_NAME = "rog-fand-usb-audio.conf"
DRIVER = "snd-usb-audio"
DEVICE_RE = re.compile(r"[0-9a-f]{4}:[0-9a-f]{4}")
OPTIONS_RE = re.compile(r"^\s*options\s+snd[-_]usb[-_]audio\s+(.*)$")
CMDLINE_RE = re.compile(r"(^|\s)snd[-_]usb[-_]audio\.quirk_flags=")
FLAG_RE = re.compile(rb"(?<![A-Za-z0-9_])" + FLAG.encode() + rb"\x00")
CONF_HEADER = (
    "# Written by the ROG panel (rog-fand): the USB audio driver's PLAYBACK_URB_FIXUP option for the\n"
    "# devices below, which may shorten audio dropouts. Turn it off on the panel's Monitor tab, or\n"
    "# delete this file. It applies whenever the driver binds the device (at start or on plug-in).\n")

# Test plan: without, with (twice as long), without again. A temperature that drifts steadily
# during the test then weighs on both sides equally.
PLAN = ((False, 1), (True, 2), (False, 1))
SAMPLE_S = 2
MIN_OFF_EVENTS = 20      # dropouts without the flag needed before the comparison means anything
HELPS, WORSE = 0.67, 1.5  # lost-audio ratio (with / without) that counts as better or worse


class Paths:
    """Where everything lives; tests point root at a fake tree."""
    def __init__(self, root="/"):
        r = Path(root)
        self.param = r / "sys/module/snd_usb_audio/parameters/quirk_flags"
        self.usb = r / "sys/bus/usb/devices"
        self.thermal = r / "sys/class/thermal"
        self.asound = r / "proc/asound"
        self.cmdline = r / "proc/cmdline"
        self.modules = r / "lib/modules"
        self.conf = r / "etc/modprobe.d" / CONF_NAME
        self.modprobe_dirs = [r / d for d in ("etc/modprobe.d", "run/modprobe.d", "usr/local/lib/modprobe.d",
                                              "usr/lib/modprobe.d", "lib/modprobe.d")]


SYSTEM = Paths()


class Refused(Exception):
    """A request this program will not carry out; the message says why."""


def read(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeDecodeError):
        return None


def check_device(text):
    device = text.lower() if isinstance(text, str) else ""
    if not DEVICE_RE.fullmatch(device):
        raise ValueError("device must be vendor:product in hex, like 2b5a:0021")
    return device


def ours(device):
    return f"{device}:{FLAG}"


# ---- the driver's quirk_flags parameter -------------------------------------------------------
# An array of up to 32 strings; the driver reads them in order up to the first unset or empty one.
# Each holds entries "VID:PID:FLAG|FLAG" separated by ";" (a "!" before a flag clears it). An
# element that is a plain number is the pre-6.18 per-card form, which this program leaves alone.

def active_elements(text):
    out = []
    for element in (text or "").split(","):
        if element in ("", "(null)"):
            break
        out.append(element)
    return out


def entries(elements):
    out = []
    for element in elements:
        try:
            int(element, 0)
        except ValueError:
            out += [e for e in element.split(";") if e]
        else:
            raise Refused("the driver's quirk_flags holds a number (%s), set outside this panel; it is "
                          "left alone" % element)
    return out


def flag_state(found, device, skip=()):
    """True when the entries leave the flag on for the device, as the driver applies them in order."""
    vid, pid = (int(x, 16) for x in device.split(":"))
    on = False
    for entry in found:
        if entry.lower() in skip:
            continue
        parts = entry.split(":", 2)
        if len(parts) != 3:
            break                      # the driver stops at a malformed entry
        try:
            e_vid = None if parts[0] == "*" else int(parts[0], 16)
            e_pid = None if parts[1] == "*" else int(parts[1], 16)
        except ValueError:
            break
        if e_vid not in (None, vid) or e_pid not in (None, pid):
            continue
        for flag in parts[2].split("|"):
            clear = flag.startswith("!")
            name = flag[1:] if clear else flag
            try:
                int(name, 16)
                break                  # a numeric mask ends the entry in the driver too
            except ValueError:
                pass
            if name.upper() == FLAG:
                on = not clear
    return on


def updated(found, device, on):
    """The entries with this program's entry for the device added (on) or removed (off)."""
    mine = ours(device)
    out = [e for e in found if e.lower() != mine.lower()]
    return out + [mine] if on else out


def param_text(found):
    # The trailing comma empties the next element, which ends the driver's list there.
    return ";".join(found) + ","


# ---- persistent setting (modprobe.d) ----------------------------------------------------------
def conf_entries(text):
    out = []
    for line in (text or "").splitlines():
        m = OPTIONS_RE.match(line)
        if m:
            for option in m.group(1).split():
                if option.startswith("quirk_flags="):
                    out += entries(active_elements(option.split("=", 1)[1]))
    return out


def conflicts(paths=None):
    """Other places that set the driver's quirk_flags; modprobe passes them all and the last one wins."""
    paths = paths or SYSTEM
    found = []
    for directory in paths.modprobe_dirs:
        for conf in sorted(directory.glob("*.conf")):
            if conf == paths.conf:
                continue
            text = read(conf) or ""
            if any(OPTIONS_RE.match(line) and "quirk_flags=" in line for line in text.splitlines()):
                found.append(str(conf))
    if CMDLINE_RE.search(read(paths.cmdline) or ""):
        found.append("the kernel command line")
    return found


def atomic_write(path, text, mode=0o644):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + "-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_conf(paths, device, on):
    found = updated(conf_entries(read(paths.conf)), device, on)
    if not found:
        paths.conf.unlink(missing_ok=True)
        return
    paths.conf.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.conf, CONF_HEADER + "options snd_usb_audio quirk_flags=%s\n" % ";".join(found))


# ---- kernel support ---------------------------------------------------------------------------
_support = {}


def module_file(paths, release):
    base = paths.modules / release
    if re.search(r"(^|/)snd-usb-audio\.ko$", read(base / "modules.builtin") or "", re.M):
        return "builtin"
    for path in sorted((base / "kernel" / "sound" / "usb").glob("snd-usb-audio.ko*")):
        return path
    m = re.search(r"^(\S*/snd-usb-audio\.ko[^:]*):", read(base / "modules.dep") or "", re.M)
    return base / m.group(1) if m else None


def module_bytes(path):
    if path.suffix == ".xz":
        return lzma.decompress(path.read_bytes())
    if path.suffix == ".gz":
        return gzip.decompress(path.read_bytes())
    if path.suffix == ".zst":
        try:
            from compression import zstd
        except ImportError:
            return subprocess.run(["zstd", "-dcq", "--", str(path)], capture_output=True, check=True,
                                  timeout=30).stdout
        return zstd.decompress(path.read_bytes())
    return path.read_bytes()


def kernel_support(paths=None, release=None):
    """(True, False or None for unknown, explanation) for the running kernel's USB audio driver."""
    paths = paths or SYSTEM
    release = release or os.uname().release
    path = module_file(paths, release)
    if path == "builtin":
        return None, "The USB audio driver is built into this kernel, so its options cannot be checked."
    if path is None:
        return None, "The USB audio driver for Linux %s was not found." % release
    try:
        key = (str(path), path.stat().st_mtime_ns)
        if key not in _support:
            _support[key] = bool(FLAG_RE.search(module_bytes(path)))
    except (OSError, lzma.LZMAError, EOFError, subprocess.SubprocessError, ValueError) as e:
        return None, "Could not read the USB audio driver (%s)." % e
    if _support[key]:
        return True, "Linux %s's USB audio driver has %s." % (release, FLAG)
    return False, ("Linux %s's USB audio driver does not have %s; it arrived in Linux %s."
                   % (release, FLAG, FIRST_KERNEL))


# ---- devices ----------------------------------------------------------------------------------
def device_dirs(paths, device=None):
    out = []
    for d in sorted(paths.usb.glob("*")):
        if ":" in d.name or read(d / "idVendor") is None:
            continue
        if device is None or f"{read(d / 'idVendor')}:{read(d / 'idProduct')}".lower() == device:
            out.append(d)
    return out


def audio_interfaces(d):
    out = []
    for itf in sorted(d.glob(d.name + ":*")):
        driver = itf / "driver"
        if driver.exists() and os.path.basename(os.path.realpath(driver)) == DRIVER:
            out.append(itf)
    return out


def devices(paths=None):
    """USB devices the USB audio driver has bound, with their ALSA card and whether they play."""
    paths = paths or SYSTEM
    out = []
    for d in device_dirs(paths):
        interfaces = audio_interfaces(d)
        if not interfaces:
            continue
        cards = [c.name[4:] for itf in interfaces for c in sorted((itf / "sound").glob("card*"))]
        streams = [read(s) or "" for n in cards for s in sorted((paths.asound / f"card{n}").glob("stream*"))]
        out.append(dict(device=f"{read(d / 'idVendor')}:{read(d / 'idProduct')}".lower(), path=d.name,
                        name=read(d / "product") or d.name,
                        card=read(paths.asound / f"card{cards[0]}" / "id") if cards else None,
                        playback=any("Playback:" in s for s in streams) if streams else None))
    return out


# ---- what is set ------------------------------------------------------------------------------
def state(paths=None, release=None):
    paths = paths or SYSTEM
    support, support_text = kernel_support(paths, release)
    param = read(paths.param)
    problem = None
    try:
        now = entries(active_elements(param))
    except Refused as e:
        now, problem = [], str(e)
    try:
        kept = conf_entries(read(paths.conf))
    except Refused as e:
        kept, problem = [], str(e)
    return dict(support=support, support_text=support_text, loaded=param is not None, now=now,
                kept=kept, conf=str(paths.conf), conf_exists=paths.conf.exists(), problem=problem,
                conflicts=conflicts(paths), devices=devices(paths))


def device_state(st, device):
    """What is set for one device: on now (from any entry), on now from this program's entry, kept at
    every start, and on now from another entry (which this program cannot switch off)."""
    mine = ours(device).lower()
    return dict(on=flag_state(st["now"], device), ours=mine in (e.lower() for e in st["now"]),
                kept=mine in (e.lower() for e in st["kept"]),
                other=flag_state(st["now"], device, skip=(mine,)))


# ---- changing it (root) -----------------------------------------------------------------------
def write_sysfs(path, text):
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    try:
        os.write(fd, text.encode())
    finally:
        os.close(fd)


def set_now(paths, device, on):
    """Add or remove this program's entry in the running driver's quirk_flags; True if it changed."""
    param = read(paths.param)
    if param is None:
        return False
    found = entries(active_elements(param))
    new = updated(found, device, on)
    if new == found:
        return False
    write_sysfs(paths.param, param_text(new))
    return True


def reattach(paths, device, sleep=time.sleep, clock=time.monotonic, timeout=15):
    """Reconnect the device so the driver binds it again with the current quirk flags."""
    dirs = device_dirs(paths, device)
    if not dirs:
        raise Refused("%s is not connected" % device)
    for d in dirs:
        write_sysfs(d / "authorized", "0")
    sleep(1)
    for d in dirs:
        write_sysfs(d / "authorized", "1")
    deadline = clock() + timeout
    while not all(audio_interfaces(d) for d in dirs):
        if clock() > deadline:
            raise Refused("%s did not come back after reconnecting; unplug it and plug it in again"
                          % (read(dirs[0] / "product") or device))
        sleep(0.25)
    return [d.name for d in dirs]


def act(action, device, paths=None, out=print, reconnect=reattach):
    """try, keep or off for one device. Returns the message printed last."""
    paths = paths or SYSTEM
    device = check_device(device)
    st = state(paths)
    if st["problem"] and action != "off":
        raise Refused(st["problem"])
    present = bool(device_dirs(paths, device))
    if action in ("try", "keep") and st["support"] is False:
        raise Refused(st["support_text"])
    if action == "try":
        if not st["loaded"]:
            raise Refused("The USB audio driver is not loaded; connect the device first.")
        if not present:
            raise Refused("%s is not connected." % device)
        set_now(paths, device, True)
        reconnect(paths, device)
        message = "On until the next restart. The device reconnected with %s." % FLAG
    elif action == "keep":
        if st["conflicts"]:
            raise Refused("The driver's quirk_flags is also set in %s, and only one setting can apply at "
                          "start. Nothing was changed." % ", ".join(st["conflicts"]))
        write_conf(paths, device, True)
        set_now(paths, device, True)
        if present and st["loaded"]:
            reconnect(paths, device)
        message = ("On at every start (%s). %s" % (paths.conf, "The device reconnected with it."
                   if present and st["loaded"] else "It applies when the device is connected."))
    elif action == "off":
        write_conf(paths, device, False)
        changed = set_now(paths, device, False)     # refuses a quirk_flags number set elsewhere
        if changed and present:
            reconnect(paths, device)
        message = "Off now and at every start." + (" The device reconnected without it." if changed and present else "")
        if flag_state(entries(active_elements(read(paths.param))), device):
            message += " Another quirk_flags entry still turns it on for this device."
    else:
        raise ValueError("unknown action")
    out(message)
    return message


# ---- the test (root) --------------------------------------------------------------------------
def board_c(paths=None):
    """The ACPI board sensor (SEN3 on the G814JZ), or None on other machines."""
    paths = paths or SYSTEM
    for zone in paths.thermal.glob("thermal_zone*"):
        if read(zone / "type") == "SEN3":
            try:
                return int(read(zone / "temp")) / 1000
            except (TypeError, ValueError):
                return None
    return None


def totals(rows):
    """Sums over phases, with per-minute rates and lost audio in milliseconds (one packet lasts
    seconds / packets: 125 us for a high-speed interface)."""
    seconds = sum(r["seconds"] for r in rows)
    packets, lost, events = (sum(r[k] for r in rows) for k in ("packets", "lost", "events"))
    weighted = [(r["board_c"], r["seconds"]) for r in rows if r.get("board_c") is not None]
    packet_ms = seconds * 1000 / packets if packets else None
    minutes = seconds / 60 if seconds else None
    return dict(seconds=round(seconds, 1), packets=packets, lost=lost, events=events,
                lost_ms_per_min=round(lost * packet_ms / minutes, 1) if packet_ms and minutes else None,
                events_per_min=round(events / minutes, 1) if minutes else None,
                ms_per_event=round(lost * packet_ms / events, 2) if packet_ms and events else None,
                board_c=round(sum(c * s for c, s in weighted) / sum(s for _, s in weighted), 1)
                if weighted and sum(s for _, s in weighted) else None)


def change(before, after):
    return ("%+.0f %%" % ((after / before - 1) * 100)).replace("-", "−") if before else "n/a"


def verdict(phases):
    off = totals([p for p in phases if not p["fix"]])
    on = totals([p for p in phases if p["fix"]])
    rate = lambda t: t["packets"] / t["seconds"] if t["seconds"] else 0
    if not off["packets"] or not on["packets"]:
        kind = "no-audio"
        text = ("No audio reached the device %s. Play audio through it for the whole test, and check that "
                "the sound comes back to it after it reconnects." % ("with the fix on" if not on["packets"]
                                                                       else "with the fix off"))
    elif min(rate(off), rate(on)) < 0.8 * max(rate(off), rate(on)):
        kind = "inconclusive"
        text = "Audio stopped for part of the test, so the counts cannot be compared. Run it again."
    elif off["events"] < MIN_OFF_EVENTS:
        kind = "inconclusive"
        text = ("Only %d dropouts without the fix: too few to compare. Run the test while the laptop is hot "
                "enough to drop audio, for example with a game running." % off["events"])
    else:
        ratio = on["lost_ms_per_min"] / off["lost_ms_per_min"]
        detail = ("Lost audio %s → %s ms per minute (%s); %s → %s dropouts per minute, %s → %s ms each."
                  % (f"{off['lost_ms_per_min']:,.0f}", f"{on['lost_ms_per_min']:,.0f}",
                     change(off["lost_ms_per_min"], on["lost_ms_per_min"]),
                     f"{off['events_per_min']:,.0f}", f"{on['events_per_min']:,.0f}",
                     off["ms_per_event"], on["ms_per_event"] if on["ms_per_event"] is not None else 0))
        if ratio <= HELPS:
            kind = "helps"
            text = ("The fix helped. %s Listen too: shorter dropouts can still be audible. If it sounds better, "
                    "keep it on." % detail)
        elif ratio >= WORSE:
            kind = "worse"
            text = "The fix made it worse. %s Leave it off." % detail
        else:
            kind = "same"
            text = "No clear difference. %s Keeping it on is not worth it." % detail
    return dict(verdict=kind, text=text, off=off, on=on)


def measure(seconds, paths_wanted, monitor, stop, clock, temp, progress):
    """Count the device's packets, skipped packets and dropouts for this many seconds."""
    row = dict(packets=0, lost=0, events=0)
    temps = []
    start = clock()
    monitor.snapshot(start)              # drop what was counted before this phase
    while not stop.is_set():
        left = seconds - (clock() - start)
        if left <= 0:
            break
        stop.wait(min(SAMPLE_S, left))
        snap = monitor.snapshot(clock())
        if snap.get("error"):
            raise Refused("USB monitoring failed: %s" % snap["error"])
        for d in snap["devices"]:
            if d.get("path") in paths_wanted:
                row["packets"] += d["packets"]
                row["lost"] += max(0, d["skipped"] - d.get("warmup_skipped", 0))
                row["events"] += d["events"]
        t = temp()
        if t is not None:
            temps.append(t)
        progress(dict(row, phase_elapsed=round(clock() - start, 1)))
    row["seconds"] = round(clock() - start, 1)
    row["board_c"] = round(sum(temps) / len(temps), 1) if temps else None
    return row


def run_test(device, phase_s, settle_s, monitor, emit, stop, paths=None, clock=time.monotonic,
             temp=None, reconnect=reattach):
    """Off, on, off. Emits start, phase-start, progress, phase and result (or cancelled) events.
    Always puts the device's setting back as it was."""
    paths = paths or SYSTEM
    device = check_device(device)
    temp = temp or (lambda: board_c(paths))
    st = state(paths)
    if st["problem"]:
        raise Refused(st["problem"])
    if st["support"] is False:
        raise Refused(st["support_text"])
    if not st["loaded"]:
        raise Refused("The USB audio driver is not loaded; connect the device and play audio first.")
    dirs = device_dirs(paths, device)
    if not dirs:
        raise Refused("%s is not connected." % device)
    ds = device_state(st, device)
    if ds["other"]:
        raise Refused("Another quirk_flags entry turns %s on for this device, so the test cannot switch it off."
                      % FLAG)
    wanted = {d.name for d in dirs}
    plan = [(on, weight * phase_s) for on, weight in PLAN]
    total_s = sum(s for _, s in plan) + settle_s * len(plan)
    original = current = ds["on"]
    start = clock()
    emit(dict(kind="start", device=device, name=read(dirs[0] / "product") or device, phases=len(plan),
              total_s=total_s, original=original))
    phases = []

    def switch(on):
        set_now(paths, device, on)
        reconnect(paths, device)

    try:
        for index, (on, seconds) in enumerate(plan, 1):
            if stop.is_set():
                break
            if on != current:
                switch(on)
                current = on
            emit(dict(kind="phase-start", index=index, fix=on, seconds=seconds, settle_s=settle_s,
                      elapsed=round(clock() - start, 1)))
            stop.wait(settle_s)          # the sound server reopens the stream after a reconnect
            if stop.is_set():
                break
            row = measure(seconds, wanted, monitor, stop, clock, temp,
                          lambda p: emit(dict(p, kind="progress", index=index, fix=on, seconds=seconds,
                                              elapsed=round(clock() - start, 1), total_s=total_s)))
            if stop.is_set():
                break
            phases.append(dict(row, index=index, fix=on))
            emit(dict(row, kind="phase", index=index, fix=on))
    finally:
        if current != original:
            switch(original)
    if len(phases) < len(plan):
        emit(dict(kind="cancelled", restored=original))
        return None
    result = dict(verdict(phases), kind="result", phases=phases, restored=original)
    emit(result)
    return result


def watch_input(stop):
    """Cancel on a "stop" line or the end of a piped stdin (the panel closing). A terminal's stdin
    is left alone; Ctrl+C cancels there."""
    try:
        piped = stat.S_ISFIFO(os.fstat(0).st_mode)
    except OSError:
        piped = False
    if not piped:
        return

    def run():
        for line in sys.stdin:
            if line.strip() == "stop":
                break
        stop.set()
    threading.Thread(target=run, daemon=True, name="stdin").start()


def usb_monitor(stop):
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(here))
    import rog_sensors
    if not os.path.exists(rog_sensors.USBMON):
        subprocess.run(["/usr/sbin/modprobe", "usbmon"], capture_output=True, timeout=30)
    if not os.path.exists(rog_sensors.USBMON):
        raise Refused("usbmon is not available (%s)." % rog_sensors.USBMON)
    monitor = rog_sensors.UsbMonitor(stop)
    monitor.start()
    return monitor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status").add_argument("--json", action="store_true")
    for name in ("try", "keep", "off"):
        sub.add_parser(name).add_argument("device")
    test = sub.add_parser("test")
    test.add_argument("device")
    test.add_argument("--phase-s", type=int, default=120, help="seconds without the fix (twice as long with it)")
    test.add_argument("--settle-s", type=int, default=20, help="seconds ignored after each reconnect")
    args = parser.parse_args(argv)
    if args.command == "status":
        st = state()
        if args.json:
            print(json.dumps(st, indent=1))
        else:
            print(st["support_text"])
            for d in st["devices"]:
                ds = device_state(st, d["device"])
                print("%s (%s, %s): %s%s" % (d["name"], d["device"], d["path"], "on" if ds["on"] else "off",
                                             ", kept at every start" if ds["kept"] else ""))
            for problem in filter(None, [st["problem"], *("quirk_flags also set in " + c for c in st["conflicts"])]):
                print(problem)
        return 0
    if os.geteuid() != 0:
        parser.error("%s needs root" % args.command)
    try:
        check_device(args.device)
    except ValueError as e:
        parser.error(str(e))
    if args.command != "test":
        try:
            act(args.command, args.device)
        except (Refused, OSError) as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        return 0
    if not 30 <= args.phase_s <= 900 or not 5 <= args.settle_s <= 120:
        parser.error("--phase-s must be 30-900 and --settle-s 5-120")
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())
    watch_input(stop)

    def emit(event):
        try:
            print(json.dumps(event), flush=True)
        except BrokenPipeError:      # the panel is gone: finish, putting the setting back
            stop.set()
    try:
        run_test(args.device, args.phase_s, args.settle_s, usb_monitor(stop), emit, stop)
    except (Refused, OSError) as e:
        emit(dict(kind="error", message=str(e)))
        return 1
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
