"""Fetch and cache firmware release assets from GitHub."""

from __future__ import annotations

import fnmatch
import json
import shutil
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .devices import Device

USER_AGENT = "fri3d-bulk-flasher"
LogFn = Callable[[str], None]


class FetchCancelled(Exception):
    """Raised when a firmware download is cancelled."""


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


@dataclass
class FirmwareInfo:
    tag: str
    asset_name: str
    path: Path
    size: int

    @property
    def label(self) -> str:
        return f"{self.tag} ({self.asset_name})"


def _meta_path(device: Device) -> Path:
    return device.firmware_dir / "meta.json"


def get_cached(device: Device) -> FirmwareInfo | None:
    """Return the firmware currently in the local cache, if any."""
    meta_file = _meta_path(device)
    if not meta_file.exists():
        return None
    try:
        meta = json.loads(meta_file.read_text())
        path = device.firmware_dir / meta["asset_name"]
        if not path.exists():
            return None
        return FirmwareInfo(
            tag=meta["tag"],
            asset_name=meta["asset_name"],
            path=path,
            size=path.stat().st_size,
        )
    except (json.JSONDecodeError, KeyError):
        return None


def _api_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_latest(
    device: Device, log: LogFn, cancel: threading.Event | None = None
) -> FirmwareInfo:
    """Download the latest matching release asset for the device.

    Raises FetchCancelled when `cancel` is set mid-download.
    """

    def check_cancel() -> None:
        if cancel is not None and cancel.is_set():
            raise FetchCancelled

    url = f"https://api.github.com/repos/{device.repo}/releases/latest"
    log(f"Querying {device.repo} for latest release...")
    release = _api_get(url)
    check_cancel()
    tag = release.get("tag_name", "unknown")

    asset = None
    for a in release.get("assets", []):
        if fnmatch.fnmatch(a["name"], device.asset_pattern):
            asset = a
            break
    if asset is None:
        raise RuntimeError(
            f"No asset matching '{device.asset_pattern}' in release {tag} of {device.repo}"
        )

    cached = get_cached(device)
    if cached and cached.tag == tag and cached.asset_name == asset["name"]:
        if cached.size == asset["size"]:
            log(f"Already up to date: {tag} ({asset['name']})")
            return cached

    device.firmware_dir.mkdir(parents=True, exist_ok=True)
    dest = device.firmware_dir / asset["name"]
    tmp = dest.with_suffix(dest.suffix + ".part")

    log(f"Downloading {asset['name']} ({format_size(asset['size'])}) from release {tag}...")

    req = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": USER_AGENT}
    )
    downloaded = 0
    next_report = 0.1
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as out:
            while True:
                check_cancel()
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if asset["size"] and downloaded / asset["size"] >= next_report:
                    log(f"  ... {downloaded / asset['size'] * 100:.0f}%")
                    next_report += 0.1
    except FetchCancelled:
        tmp.unlink(missing_ok=True)
        raise

    shutil.move(str(tmp), str(dest))
    _meta_path(device).write_text(
        json.dumps({"tag": tag, "asset_name": asset["name"], "size": asset["size"]})
    )
    log(f"Saved to {dest}")
    return FirmwareInfo(tag=tag, asset_name=asset["name"], path=dest, size=asset["size"])
