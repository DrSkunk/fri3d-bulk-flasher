"""Flashing logic: device detection, esptool/wchisp invocation, bulk loops.

Badges (esptool) flash in parallel: every newly plugged-in serial port claims
a free slot and gets its own worker thread. CH32 devices (wchisp) flash one
at a time because wchisp cannot address a specific unit.
"""

from __future__ import annotations

import itertools
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from serial.tools import list_ports

from .devices import Device
from .tools import ensure_wchisp, esptool_cmd

LogFn = Callable[[str], None]

# USB VIDs that can carry an ESP32 serial connection:
# Espressif native USB, CP210x, CH34x, FTDI
ESP_VIDS = {0x303A, 0x10C4, 0x1A86, 0x0403}

POLL_INTERVAL = 0.5
# Consecutive absent polls before a port counts as unplugged (survives the
# brief re-enumeration blip when the device resets after flashing).
REMOVAL_STABLE_POLLS = 5

_PROGRESS_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


@dataclass
class Stats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass
class Slot:
    """Status of one parallel flashing slot, shared with the UI thread."""

    index: int
    state: str = "waiting"  # waiting | flashing | success | failed
    port: str = ""
    progress: int = -1  # -1 = no progress bar
    detail: str = ""
    unit: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def update(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "index": self.index,
                "state": self.state,
                "port": self.port,
                "progress": self.progress,
                "detail": self.detail,
                "unit": self.unit,
            }


SlotFn = Callable[[Slot], None]


def _esp_candidate_ports() -> set[str]:
    return {
        p.device
        for p in list_ports.comports()
        if p.vid in ESP_VIDS
    }


def _terminate(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _kill_on_cancel(proc: subprocess.Popen, cancel: threading.Event) -> None:
    """Terminate proc when cancel fires, so a blocked pipe read gets EOF."""
    while proc.poll() is None:
        if cancel.wait(timeout=0.5):
            if proc.poll() is None:
                _terminate(proc)
            return


def _run_streamed(
    cmd: list[str], log: LogFn, cancel: threading.Event, timeout: float | None = None
) -> int:
    """Run a command, streaming output lines (handles \\r progress) to log."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )
    assert proc.stdout is not None
    watchdog = threading.Thread(
        target=_kill_on_cancel, args=(proc, cancel), daemon=True
    )
    watchdog.start()
    buf = b""
    last_progress = 0.0
    while True:
        if cancel.is_set():
            _terminate(proc)
            return -1
        chunk = proc.stdout.read(1)
        if chunk == b"":
            break
        if chunk in (b"\n", b"\r"):
            line = buf.decode(errors="replace").rstrip()
            buf = b""
            if not line:
                continue
            # esptool emits hundreds of \r progress updates; throttle them
            if chunk == b"\r" and "%" in line:
                now = time.monotonic()
                if now - last_progress < 1.0:
                    continue
                last_progress = now
            log(f"  {line}")
        else:
            buf += chunk
    if buf:
        log(f"  {buf.decode(errors='replace').rstrip()}")
    return proc.wait(timeout=timeout)


def _run_quiet(cmd: list[str], timeout: float = 10) -> int:
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=creationflags,
        )
        return proc.returncode
    except subprocess.TimeoutExpired:
        return -1


# ------------------------------------------------------- esptool (parallel) ---


class _PortWatcher:
    """Latest set of candidate ports, polled once by the manager thread and
    read by all slot workers (so 10 workers don't each enumerate USB)."""

    def __init__(self) -> None:
        self._ports: set[str] = set()
        self._lock = threading.Lock()

    def set_ports(self, ports: set[str]) -> None:
        with self._lock:
            self._ports = ports

    def has(self, port: str) -> bool:
        with self._lock:
            return port in self._ports


def _wait_for_removal_watched(
    port: str, watcher: _PortWatcher, cancel: threading.Event
) -> None:
    absent = 0
    while not cancel.is_set():
        if watcher.has(port):
            absent = 0
        else:
            absent += 1
            if absent >= REMOVAL_STABLE_POLLS:
                return
        time.sleep(POLL_INTERVAL)


def _flash_esptool_slot(
    device: Device,
    fw: Path,
    port: str,
    slot: Slot,
    on_slot_change: SlotFn,
    log: LogFn,
    cancel: threading.Event,
    unit: int,
) -> bool:
    cmd = esptool_cmd() + [
        "--chip", device.chip,
        "--port", port,
        "--baud", str(device.baud),
        "write-flash", hex(device.flash_offset), str(fw),
    ]

    def on_line(raw: str) -> None:
        line = raw.strip()
        match = _PROGRESS_RE.search(line)
        if match:
            slot.update(progress=min(100, int(float(match.group(1)))), detail=line)
        else:
            slot.update(detail=line)
        on_slot_change(slot)
        lowered = line.lower()
        if "error" in lowered or "fatal" in lowered:
            log(f"[#{unit}] {port}: {line}")

    return _run_streamed(cmd, on_line, cancel) == 0


def _slot_worker(
    device: Device,
    fw: Path,
    port: str,
    slot: Slot,
    unit: int,
    watcher: _PortWatcher,
    cancel: threading.Event,
    stats: Stats,
    on_stats_change: Callable[[], None],
    on_slot_change: SlotFn,
    log: LogFn,
    release: Callable[[str, Slot], None],
) -> None:
    try:
        slot.update(
            state="flashing", port=port, unit=unit, progress=0, detail="Preparing..."
        )
        on_slot_change(slot)
        log(f"[#{unit}] {port}: flashing {fw.name}...")
        # Give the OS a moment to finish setting up the port
        if cancel.wait(1.0):
            return
        ok = _flash_esptool_slot(
            device, fw, port, slot, on_slot_change, log, cancel, unit
        )
        if cancel.is_set():
            return
        with stats.lock:
            stats.attempted += 1
            if ok:
                stats.succeeded += 1
            else:
                stats.failed += 1
        on_stats_change()
        if ok:
            slot.update(state="success", progress=100, detail="Unplug to free slot")
            log(f"[#{unit}] {port}: SUCCESS — unplug when ready.")
        else:
            slot.update(state="failed", progress=-1, detail="Unplug to free slot")
            log(f"[#{unit}] {port}: FAILED — check the connection and try again.")
        on_slot_change(slot)
        _wait_for_removal_watched(port, watcher, cancel)
    except Exception as exc:  # noqa: BLE001 — a dead slot must not kill the run
        log(f"[#{unit}] {port}: ERROR: {exc}")
    finally:
        slot.update(state="waiting", port="", progress=-1, detail="", unit=0)
        on_slot_change(slot)
        release(port, slot)


def parallel_flash_loop(
    device: Device,
    fw: Path,
    cancel: threading.Event,
    log: LogFn,
    stats: Stats,
    on_stats_change: Callable[[], None],
    slots: list[Slot],
    on_slot_change: SlotFn,
) -> None:
    """Flash every newly plugged-in badge, up to len(slots) at once.

    Runs in a worker thread. Each claimed port keeps its slot until the
    device is unplugged, so a unit is never flashed twice.
    """
    log(
        f"=== Bulk flashing {device.name} — up to {len(slots)} at once, "
        "press Stop to end ==="
    )
    if device.instructions:
        log(device.instructions)

    watcher = _PortWatcher()
    claim_lock = threading.Lock()
    claimed: dict[str, Slot] = {}
    available = list(slots)
    threads: list[threading.Thread] = []
    unit_counter = itertools.count(1)

    def release(port: str, slot: Slot) -> None:
        with claim_lock:
            claimed.pop(port, None)
            available.append(slot)

    while not cancel.is_set():
        ports = _esp_candidate_ports()
        watcher.set_ports(ports)
        with claim_lock:
            new_ports = sorted(ports - claimed.keys())
            for port in new_ports:
                if not available:
                    break
                slot = available.pop(0)
                claimed[port] = slot
                thread = threading.Thread(
                    target=_slot_worker,
                    args=(
                        device, fw, port, slot, next(unit_counter), watcher,
                        cancel, stats, on_stats_change, on_slot_change, log,
                        release,
                    ),
                    daemon=True,
                )
                threads.append(thread)
                thread.start()
        time.sleep(POLL_INTERVAL)

    for thread in threads:
        thread.join(timeout=10)

    log("")
    log(
        f"=== Stopped. {stats.succeeded} flashed OK, "
        f"{stats.failed} failed, {stats.attempted} total. ==="
    )


# --------------------------------------------------------- wchisp (serial) ---


def _wait_for_wch_device(
    wchisp: Path, cancel: threading.Event, log: LogFn, instructions: str
) -> bool:
    announced = False
    while not cancel.is_set():
        if _run_quiet([str(wchisp), "info"]) == 0:
            return True
        if not announced:
            log(f"Waiting for a device in bootloader mode... {instructions}")
            announced = True
        time.sleep(1.0)
    return False


def _wait_for_wch_removal(wchisp: Path, cancel: threading.Event, log: LogFn) -> None:
    log("Unplug the device to continue.")
    absent = 0
    while not cancel.is_set():
        if _run_quiet([str(wchisp), "info"]) == 0:
            absent = 0
        else:
            absent += 1
            if absent >= 2:
                return
        time.sleep(1.0)


def _flash_wchisp(
    wchisp: Path, fw: Path, log: LogFn, cancel: threading.Event
) -> bool:
    log(f"Flashing {fw.name} with wchisp...")
    rc = _run_streamed([str(wchisp), "flash", str(fw)], log, cancel)
    return rc == 0


def wchisp_flash_loop(
    device: Device,
    fw: Path,
    cancel: threading.Event,
    log: LogFn,
    stats: Stats,
    on_stats_change: Callable[[], None],
    slot: Slot,
    on_slot_change: SlotFn,
) -> None:
    """Repeatedly flash CH32 devices one at a time until cancelled."""
    wchisp = ensure_wchisp(log)

    log(f"=== Bulk flashing {device.name} — press Stop to end ===")
    if device.instructions:
        log(device.instructions)

    unit = 0
    while not cancel.is_set():
        unit += 1
        log("")
        log(f"--- Device #{unit} ---")
        slot.update(
            state="waiting", port="", progress=-1, unit=unit,
            detail=device.instructions,
        )
        on_slot_change(slot)

        if not _wait_for_wch_device(wchisp, cancel, log, device.instructions):
            break
        slot.update(state="flashing", port="USB bootloader", progress=0,
                    detail="Flashing...")
        on_slot_change(slot)
        ok = _flash_wchisp(wchisp, fw, log, cancel)

        if cancel.is_set():
            break

        with stats.lock:
            stats.attempted += 1
            if ok:
                stats.succeeded += 1
            else:
                stats.failed += 1
        on_stats_change()

        if ok:
            slot.update(state="success", progress=100, detail="Unplug to continue")
            log(f"SUCCESS — device #{unit} flashed.")
        else:
            slot.update(state="failed", progress=-1, detail="Unplug to continue")
            log(f"FAILED — device #{unit}. Check the connection and try again.")
        on_slot_change(slot)

        _wait_for_wch_removal(wchisp, cancel, log)

    slot.update(state="waiting", port="", progress=-1, detail="", unit=0)
    on_slot_change(slot)
    log("")
    log(
        f"=== Stopped. {stats.succeeded} flashed OK, "
        f"{stats.failed} failed, {stats.attempted} total. ==="
    )
