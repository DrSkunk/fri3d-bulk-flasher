"""Device definitions for the Fri3d Camp 2026 hardware."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CACHE_DIR = Path.home() / ".fri3d-bulk-flasher"
FIRMWARE_DIR = CACHE_DIR / "firmware"
TOOLS_DIR = CACHE_DIR / "tools"


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    repo: str
    asset_pattern: str
    method: str  # "esptool" or "wchisp"
    chip: str
    flash_offset: int = 0
    baud: int = 2_000_000
    instructions: str = ""

    @property
    def firmware_dir(self) -> Path:
        return FIRMWARE_DIR / self.id


DEVICES: list[Device] = [
    Device(
        id="badge2026",
        name="Fri3d Badge 2026",
        repo="Fri3dCamp/badge_firmware_MicroPythonOS",
        asset_pattern="full_2026_firmware_for_2026_badge.bin",
        method="esptool",
        chip="esp32s3",
        flash_offset=0x0,
        baud=921600,
        instructions="Connect the badge over USB. It should enumerate as a serial port automatically.",
    ),
    Device(
        id="communicator2026",
        name="Communicator 2026",
        repo="Fri3dCamp/communicator_2026",
        asset_pattern="firmware.bin",
        method="wchisp",
        chip="CH32X035",
        instructions="Hold the BOOT button while plugging in USB to enter the bootloader.",
    ),
    Device(
        id="dj2026",
        name="DJ Addon 2026",
        repo="Fri3dCamp/dj_2026",
        asset_pattern="firmware.bin",
        method="wchisp",
        chip="CH32X035",
        instructions="Hold the BOOT button while plugging in USB to enter the bootloader.",
    ),
]


def device_by_id(device_id: str) -> Device:
    for d in DEVICES:
        if d.id == device_id:
            return d
    raise KeyError(device_id)
