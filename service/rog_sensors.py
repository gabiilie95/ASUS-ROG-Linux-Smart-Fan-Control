"""Read-only laptop sensors and interval counters for rog-fand."""
import ctypes as ct
import math
import os
from pathlib import Path
import threading
import time
from collections import defaultdict

PROFILE = Path("/sys/firmware/acpi/platform_profile")
USBMON = "/sys/kernel/debug/usb/usbmon/0u"
FANS = ("cpu", "gpu", "mid")


def read(path):
    try:
        return path.read_text().strip()
    except OSError:
        return None


def integer(path):
    try:
        return int(read(path))
    except (TypeError, ValueError):
        return None


class Rapl:
    def __init__(self):
        self.zone = next((p for p in Path("/sys/class/powercap").glob("intel-rapl:*")
                          if read(p / "name") == "package-0"), None)
        self.previous = None

    def watts(self, now):
        energy = integer(self.zone / "energy_uj") if self.zone else None
        prev = self.previous
        self.previous = None if energy is None else (now, energy)
        if energy is None or prev is None or now <= prev[0]:
            return None
        delta = energy - prev[1]
        if delta < 0:
            limit = integer(self.zone / "max_energy_range_uj")
            if not limit:
                return None
            delta += limit
        return delta / ((now - prev[0]) * 1e6) if delta >= 0 else None


class Nvidia:
    """Look up the NVIDIA display controller by its sysfs vendor and PCI address."""
    def __init__(self):
        self.lib = None
        self.handle = ct.c_void_p()
        self.retry_after = 0

    def connect(self):
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
            dev = card / "device"
            if read(dev / "vendor") == "0x10de":
                break
        else:
            return False
        lib = ct.CDLL("libnvidia-ml.so.1")
        if lib.nvmlInit_v2() != 0:
            return False
        self.lib = lib
        address = dev.resolve().name.encode("ascii")
        if lib.nvmlDeviceGetHandleByPciBusId_v2(address, ct.byref(self.handle)) != 0:
            self.close()
            return False
        return True

    def sample(self, now):
        try:
            if self.lib is None:
                if now < self.retry_after:
                    return None, None
                self.retry_after = now + 30
                if not self.connect():
                    return None, None
            power, temp = ct.c_uint(), ct.c_uint()
            pw = self.lib.nvmlDeviceGetPowerUsage(self.handle, ct.byref(power))
            tc = self.lib.nvmlDeviceGetTemperature(self.handle, 0, ct.byref(temp))
            if pw or tc:
                self.close()
            return (None if pw else power.value / 1000, None if tc else temp.value)
        except (AttributeError, OSError):
            self.close()
            return None, None

    def close(self):
        if self.lib is not None:
            self.lib.nvmlShutdown()
            self.lib = None


class Sensors:
    def __init__(self):
        self.rapl = Rapl()
        self.gpu = Nvidia()

    def sample(self, now):
        gpu_w, gpu_c = self.gpu.sample(now)
        values = dict(cpu_w=self.rapl.watts(now), gpu_w=gpu_w, gpu_c=gpu_c,
                      cpu_c=None, board_c=None, profile=read(PROFILE))
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            key = {"SEN3": "board_c", "x86_pkg_temp": "cpu_c"}.get(read(zone / "type"))
            if key:
                value = integer(zone / "temp")
                values[key] = value / 1000 if value is not None else None
        rpm = dict.fromkeys(FANS)
        for hw in Path("/sys/class/hwmon").glob("hwmon*"):
            if read(hw / "name") == "asus":
                for label in hw.glob("fan*_label"):
                    fan = (read(label) or "").removesuffix("_fan")
                    if fan in rpm:
                        rpm[fan] = integer(label.with_name(label.name.replace("_label", "_input")))
        values.update({f"{fan}_rpm": value for fan, value in rpm.items()})
        return values


def parse_usbmon(line):
    # usb-drop-count.sh's tail estimate: only the first five descriptors are visible.
    if " C Zo:" not in line:
        return None
    parts = line.split(maxsplit=12)
    try:
        if parts[2] != "C":
            return None
        kind, bus, dev, endpoint = parts[3].split(":")
        if kind != "Zo":
            return None
        ts, bus, dev, nd = map(int, (parts[1], bus, dev, parts[5]))
        if nd <= 0:
            return None
        bad = []
        for i in range(min(nd, 5)):
            status, _, _length = map(int, parts[6 + i].split(":"))
            # Match usb-drop-count.sh: EXDEV can retain a nonzero descriptor length.
            bad.append(status != 0)
        skipped = sum(bad) + (nd - len(bad) if bad[-1] else 0)
        return (bus, dev), ts, nd, skipped
    except (IndexError, ValueError):
        return None


class UsbCounts:
    """One writer and one sampler. Keep stall grouping across interval boundaries."""
    def __init__(self):
        self.interval = defaultdict(lambda: [0, 0, 0])
        self.last_bad = {}
        self.active_at = -math.inf

    def add(self, packet, now):
        device, ts, count, skipped = packet
        row = self.interval[device]
        row[0] += count
        row[1] += skipped
        if skipped:
            previous = self.last_bad.get(device)
            if previous is None or ts - previous > 1500 or ts < previous:
                row[2] += 1
            self.last_bad[device] = ts
        self.active_at = now


def usb_names():
    names = {}
    for dev in Path("/sys/bus/usb/devices").glob("*"):
        bus, number = integer(dev / "busnum"), integer(dev / "devnum")
        if bus is not None and number is not None:
            names[bus, number] = (dev.name, read(dev / "product") or dev.name)
    return names


class UsbMonitor:
    def __init__(self, stop):
        self.stop = stop
        self.lock = threading.Lock()
        self.counts = UsbCounts()
        self.error = "usbmon requires root" if os.geteuid() else "usbmon starting"
        self.names = {}
        self.refresh_at = 0
        self.thread = threading.Thread(target=self.run, daemon=True, name="usbmon")
        self.sample_at = time.monotonic()

    def start(self):
        if os.geteuid() == 0:
            self.thread.start()

    def run(self):
        while not self.stop.is_set():
            try:
                fd = os.open(USBMON, os.O_RDONLY | os.O_NONBLOCK)
                try:
                    with self.lock:
                        self.error = None
                    while not self.stop.is_set():
                        try:
                            raw = os.read(fd, 65536)
                        except BlockingIOError:
                            self.stop.wait(0.01)
                            continue
                        if not raw:
                            raise OSError("usbmon closed")
                        # The kernel text interface returns one event per read.
                        event = parse_usbmon(raw.decode("ascii", errors="replace"))
                        if event:
                            with self.lock:
                                self.counts.add(event, time.monotonic())
                finally:
                    os.close(fd)
            except OSError as exc:
                with self.lock:
                    self.error = f"usbmon unavailable: {exc.strerror or exc}"
                self.stop.wait(10)

    def snapshot(self, now):
        with self.lock:
            counts = self.counts.interval
            self.counts.interval = defaultdict(lambda: [0, 0, 0])
            active = now - self.counts.active_at <= 5
            error = self.error
        elapsed = max(now - self.sample_at, 0.001)
        self.sample_at = now
        if counts and (now >= self.refresh_at or not counts.keys() <= self.names.keys()):
            self.names = usb_names()
            self.refresh_at = now + 30
        devices = []
        for (bus, dev), (packets, skipped, events) in sorted(counts.items()):
            path, name = self.names.get((bus, dev), (None, None))
            devices.append(dict(bus=bus, dev=dev, path=path, name=name, packets=packets,
                                skipped=skipped, events=events, drops_per_s=round(skipped/elapsed, 2),
                                events_per_s=round(events/elapsed, 2)))
        return dict(audio_active=active and error is None, devices=devices, error=error)
