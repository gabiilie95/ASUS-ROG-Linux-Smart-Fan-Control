#!/usr/bin/env python3
"""GPU heat for fan tuning: a floating-point kernel on the NVIDIA GPU, held at a target power.

    python3 tools/gpuload.py --watts 120 --seconds 60
    python3 tools/gpuload.py --duty 0.5 --seconds 20      # fixed duty cycle, no power target

Runs through the NVIDIA driver's own CUDA library (libcuda.so.1, which compiles the PTX below at
run time), so no toolkit or package is needed. CUDA only lists NVIDIA GPUs: the Intel iGPU is never
touched. Each kernel launch lasts about 2 ms, so the desktop (also drawn by this GPU) keeps getting
time. Power is held by a duty cycle: within every 50 ms the kernel runs for a share of the time and
the GPU idles for the rest. Once a second the share follows NVML's power reading.

Stops by itself after --seconds, on SIGTERM/SIGINT/SIGHUP, when the GPU reaches --max-temp, or when
the process that started it exits. Standard library only.

Measured on the G814JZ (RTX 4080 Laptop, 2026-11-17, Balanced profile): 40, 80 and 120 W held
within a few watts; full duty reaches about 175 W.
"""
import argparse
import ctypes as ct
import json
import math
import os
from pathlib import Path
import signal
import struct
import sys
import threading
import time

# rog_sensors.py is in service/ in a checkout and beside tools/ once installed.
_BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BASE / "service" if (_BASE / "service").is_dir() else _BASE))
from rog_sensors import Nvidia  # noqa: E402  (NVML through ctypes, works as the desktop user)

THREADS = 256           # per block
BLOCKS_PER_SM = 6       # 1536 resident threads per SM on Ada: full occupancy
GROUPS = 8              # rotation groups per loop pass
LAUNCH_S = 0.002        # target length of one kernel launch
PERIOD_S = 0.05         # duty-cycle period
SM_ATTRIBUTE = 16       # CU_DEVICE_ATTRIBUTE_MULTIPROCESSOR_COUNT
JIT_ERROR_LOG_BUFFER, JIT_ERROR_LOG_BUFFER_SIZE_BYTES = 5, 6


def f32(value):
    return "0f%08X" % struct.unpack("<I", struct.pack("<f", value))[0]


def kernel_ptx(groups=GROUPS):
    """PTX for the `burn` kernel: every thread rotates four 2-D points by 0.1 rad, `groups` times
    per loop pass (two multiplies and two multiply-adds per point).

    A rotation keeps each point's length, so values stay finite, and unlike x*m + c (which settles
    at a fixed point and stops switching bits) every operand keeps changing. That matters: on this
    GPU a settling multiply-add kernel tops out at 135-140 W, the rotation at about 175 W. Memory
    loads and tensor-core work were tried too; both stall the loop and lower the power."""
    pairs = ((1, 2), (3, 4), (5, 6), (7, 8))
    lines = [".version 6.0", ".target sm_52", ".address_size 64",
             ".visible .entry burn(.param .u64 out, .param .u32 iters)", "{",
             "    .reg .pred %p;", "    .reg .f32 %f<9>;", "    .reg .f32 %t<2>;",
             "    .reg .f32 %cs, %sn, %ns;", "    .reg .b32 %r<6>;", "    .reg .b64 %rd<4>;",
             "    ld.param.u64 %rd1, [out];", "    ld.param.u32 %r1, [iters];",
             "    mov.u32 %r2, %ctaid.x;", "    mov.u32 %r3, %ntid.x;", "    mov.u32 %r4, %tid.x;",
             "    mad.lo.u32 %r2, %r2, %r3, %r4;",
             "    cvt.rn.f32.u32 %f0, %r2;", "    mul.f32 %f0, %f0, 0f33800000;",
             "    mov.f32 %%cs, %s;" % f32(math.cos(0.1)),
             "    mov.f32 %%sn, %s;" % f32(math.sin(0.1)),
             "    mov.f32 %%ns, %s;" % f32(-math.sin(0.1))]
    lines += ["    add.f32 %%f%d, %%f0, %s;" % (i, f32(0.5 + i / 16)) for i in range(1, 9)]
    lines += ["    mov.u32 %r5, 0;", "LOOP:"]
    for _ in range(groups):
        for x, y in pairs:
            lines += ["    mul.f32 %%t0, %%f%d, %%ns;" % y, "    mul.f32 %%t1, %%f%d, %%sn;" % x,
                      "    fma.rn.f32 %%f%d, %%f%d, %%cs, %%t0;" % (x, x),
                      "    fma.rn.f32 %%f%d, %%f%d, %%cs, %%t1;" % (y, y)]
    lines += ["    add.u32 %r5, %r5, 1;", "    setp.lt.u32 %p, %r5, %r1;", "    @%p bra LOOP;"]
    lines += ["    add.f32 %%f1, %%f1, %%f%d;" % i for i in range(2, 9)]
    lines += ["    cvt.u64.u32 %rd2, %r2;", "    shl.b64 %rd2, %rd2, 2;", "    add.u64 %rd3, %rd1, %rd2;",
              "    st.global.f32 [%rd3], %f1;", "    ret;", "}", ""]
    return "\n".join(lines)


class CudaError(RuntimeError):
    pass


class Cuda:
    """The handful of CUDA driver calls the burn kernel needs."""
    def __init__(self):
        try:
            self.lib = ct.CDLL("libcuda.so.1")
        except OSError as exc:
            raise CudaError("The NVIDIA CUDA driver library (libcuda.so.1) is unavailable") from exc
        self.ctx = self.module = self.buffer = None
        self.device = ct.c_int()
        self.call("cuInit", 0)
        count = ct.c_int()
        self.call("cuDeviceGetCount", ct.byref(count))
        if count.value < 1:
            raise CudaError("No NVIDIA GPU visible to CUDA")
        self.call("cuDeviceGet", ct.byref(self.device), 0)
        name = ct.create_string_buffer(128)
        self.call("cuDeviceGetName", name, 128, self.device)
        self.name = name.value.decode(errors="replace")
        bus = ct.create_string_buffer(32)
        self.call("cuDeviceGetPCIBusId", bus, 32, self.device)
        self.bus = bus.value.decode(errors="replace")
        sms = ct.c_int()
        self.call("cuDeviceGetAttribute", ct.byref(sms), SM_ATTRIBUTE, self.device)
        self.sms = sms.value
        ctx = ct.c_void_p()
        self.call("cuDevicePrimaryCtxRetain", ct.byref(ctx), self.device)
        self.ctx = ctx
        self.call("cuCtxSetCurrent", self.ctx)
        module = ct.c_void_p()
        log = ct.create_string_buffer(4096)
        options = (ct.c_int * 2)(JIT_ERROR_LOG_BUFFER, JIT_ERROR_LOG_BUFFER_SIZE_BYTES)
        values = (ct.c_void_p * 2)(ct.cast(log, ct.c_void_p), ct.c_void_p(len(log)))
        try:
            self.call("cuModuleLoadDataEx", ct.byref(module), ct.c_char_p(kernel_ptx().encode()),
                      2, options, values)
        except CudaError as exc:
            raise CudaError("%s: %s" % (exc, log.value.decode(errors="replace").strip())) from None
        self.module = module
        self.function = ct.c_void_p()
        self.call("cuModuleGetFunction", ct.byref(self.function), self.module, b"burn")
        self.blocks = self.sms * BLOCKS_PER_SM
        buffer = ct.c_uint64()
        self.call("cuMemAlloc_v2", ct.byref(buffer), ct.c_size_t(self.blocks * THREADS * 4))
        self.buffer = buffer

    def call(self, name, *args):
        result = getattr(self.lib, name)(*args)
        if result:
            text = ct.c_char_p()
            self.lib.cuGetErrorString(result, ct.byref(text))
            raise CudaError("%s failed: %s" % (name, (text.value or b"error %d" % result).decode()))

    def launch(self, iters):
        values = (ct.c_uint64(self.buffer.value), ct.c_uint32(iters))
        params = (ct.c_void_p * len(values))(*(ct.cast(ct.byref(v), ct.c_void_p) for v in values))
        self.call("cuLaunchKernel", self.function, self.blocks, 1, 1, THREADS, 1, 1, 0, None, params, None)
        self.call("cuCtxSynchronize")

    def close(self):
        try:
            if self.buffer:
                self.lib.cuMemFree_v2(self.buffer)
            if self.module:
                self.lib.cuModuleUnload(self.module)
            if self.ctx:
                self.lib.cuDevicePrimaryCtxRelease_v2(self.device)
        finally:
            self.buffer = self.module = self.ctx = None


def calibrate(cuda, stop):
    """Loop count for one launch of about LAUNCH_S seconds."""
    iters = 16
    while not stop.is_set():
        start = time.monotonic()
        cuda.launch(iters)
        took = time.monotonic() - start
        if took >= LAUNCH_S / 2 or iters >= 1 << 24:
            return max(1, int(iters * LAUNCH_S / max(took, 1e-6)))
        iters *= 4
    return iters


class Controller:
    """Integral control of the duty cycle towards a power target.

    Each step moves the duty by half the error over the generator's watts per unit duty, so it
    settles in a few seconds without much overshoot. NVML's reading averages about a second."""
    def __init__(self, target, duty=None, idle_w=20.0, span_w=160.0):
        self.target = target
        self.span = span_w
        self.duty = duty if duty is not None else min(1.0, max(0.02, (target - idle_w) / span_w))
        self.saturated = False

    def update(self, watts):
        if self.target is None or watts is None:
            return self.duty
        self.duty = min(1.0, max(0.0, self.duty + 0.5 * (self.target - watts) / self.span))
        self.saturated = self.duty >= 1.0 and watts < self.target - 5
        return self.duty


def run(args, stop, emit, cuda_factory=Cuda, nvml_factory=Nvidia):
    parent = os.getppid()
    nvml = nvml_factory()
    try:
        cuda = cuda_factory()
    except BaseException:
        nvml.close()
        raise
    try:
        emit(kind="device", name=cuda.name, bus=cuda.bus, sms=cuda.sms)
        iters = calibrate(cuda, stop)
        control = Controller(args.watts, args.duty)
        start = next_report = time.monotonic()
        busy_s = 0.0
        while not stop.is_set() and time.monotonic() - start < args.seconds:
            period = time.monotonic()
            while time.monotonic() - period < control.duty * PERIOD_S and not stop.is_set():
                launched = time.monotonic()
                cuda.launch(iters)
                busy_s += time.monotonic() - launched
            rest = PERIOD_S - (time.monotonic() - period)
            if rest > 0:
                stop.wait(rest)
            now = time.monotonic()
            if now >= next_report:
                watts, temp = nvml.sample(now)
                if temp is not None and temp >= args.max_temp:
                    emit(kind="stop", reason="GPU %d C reached the %d C limit" % (temp, args.max_temp))
                    return 3
                if os.getppid() != parent:
                    emit(kind="stop", reason="parent process exited")
                    return 4
                control.update(watts)
                emit(kind="sample", elapsed=round(now - start, 1), watts=watts, temp=temp,
                     duty=round(control.duty, 3), busy=round(busy_s, 2), saturated=control.saturated)
                busy_s = 0.0
                next_report = now + 1
        return 0
    finally:
        try:
            cuda.close()
        finally:
            nvml.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    how = ap.add_mutually_exclusive_group(required=True)
    how.add_argument("--watts", type=float, help="hold GPU power near this many watts")
    how.add_argument("--duty", type=float, help="fixed share of time the kernel runs (0-1)")
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--max-temp", type=int, default=85, help="stop at this GPU temperature (C)")
    ap.add_argument("--json", action="store_true", help="one JSON object per line")
    args = ap.parse_args(argv)
    if args.watts is not None and not 5 <= args.watts <= 200:
        ap.error("--watts must be between 5 and 200")
    if args.duty is not None and not 0 <= args.duty <= 1:
        ap.error("--duty must be between 0 and 1")
    if not 0 < args.seconds <= 4 * 3600:
        ap.error("--seconds must be positive and at most four hours")
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: stop.set())

    def emit(**event):
        if args.json:
            print(json.dumps(event), flush=True)
        elif event["kind"] == "sample":
            print("%6.1f s  %6s W  %4s C  duty %.2f%s" % (
                event["elapsed"], "--" if event["watts"] is None else "%.1f" % event["watts"],
                event["temp"], event["duty"], "  (at full duty)" if event["saturated"] else ""), flush=True)
        else:
            print(" ".join("%s=%s" % kv for kv in event.items()), flush=True)
    return run(args, stop, emit)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CudaError as exc:
        sys.exit(str(exc))
