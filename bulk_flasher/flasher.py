"""Flashing logic: device detection, esptool/wchisp invocation, bulk loop."""

from __future__ import annotations

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


@dataclass
class Stats:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


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


# ---------------------------------------------------------------- esptool ---


def _wait_for_esp_port(cancel: threading.Event, log: LogFn) -> str | None:
    announced = False
    while not cancel.is_set():
        ports = sorted(_esp_candidate_ports())
        if ports:
            if len(ports) > 1:
                log(f"Multiple candidate ports {ports}; using {ports[0]}")
            return ports[0]
        if not announced:
            log("Waiting for a badge... plug one in.")
            announced = True
        time.sleep(POLL_INTERVAL)
    return None


def _wait_for_port_removal(port: str, cancel: threading.Event, log: LogFn) -> None:
    log("Unplug the device to continue.")
    absent = 0
    while not cancel.is_set():
        if port in _esp_candidate_ports():
            absent = 0
        else:
            absent += 1
            if absent >= REMOVAL_STABLE_POLLS:
                return
        time.sleep(POLL_INTERVAL)


def _flash_esptool(
    device: Device, fw: Path, port: str, log: LogFn, cancel: threading.Event
) -> bool:
    cmd = esptool_cmd() + [
        "--chip", device.chip,
        "--port", port,
        "--baud", str(device.baud),
        "write-flash", hex(device.flash_offset), str(fw),
    ]
    log(f"Flashing {fw.name} to {port} at {device.baud} baud...")
    rc = _run_streamed(cmd, log, cancel)
    return rc == 0


# ----------------------------------------------------------------- wchisp ---


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


# -------------------------------------------------------------- bulk loop ---


def bulk_flash_loop(
    device: Device,
    fw: Path,
    cancel: threading.Event,
    log: LogFn,
    stats: Stats,
    on_stats_change: Callable[[], None],
) -> None:
    """Repeatedly flash devices until cancelled. Runs in a worker thread."""
    wchisp: Path | None = None
    if device.method == "wchisp":
        wchisp = ensure_wchisp(log)

    log(f"=== Bulk flashing {device.name} — press Stop to end ===")
    if device.instructions:
        log(device.instructions)

    unit = 0
    while not cancel.is_set():
        unit += 1
        log("")
        log(f"--- Device #{unit} ---")

        if device.method == "esptool":
            port = _wait_for_esp_port(cancel, log)
            if port is None:
                break
            # Give the OS a moment to finish setting up the port
            time.sleep(1.0)
            ok = _flash_esptool(device, fw, port, log, cancel)
        else:
            assert wchisp is not None
            if not _wait_for_wch_device(wchisp, cancel, log, device.instructions):
                break
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
            log(f"SUCCESS — device #{unit} flashed.")
        else:
            log(f"FAILED — device #{unit}. Check the connection and try again.")

        if device.method == "esptool":
            _wait_for_port_removal(port, cancel, log)  # type: ignore[arg-type]
        else:
            assert wchisp is not None
            _wait_for_wch_removal(wchisp, cancel, log)

    log("")
    log(
        f"=== Stopped. {stats.succeeded} flashed OK, "
        f"{stats.failed} failed, {stats.attempted} total. ==="
    )
