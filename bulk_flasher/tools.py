"""Locate or install the external flashing tools (wchisp)."""

from __future__ import annotations

import json
import platform
import shutil
import ssl
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

import certifi

from .devices import TOOLS_DIR

LogFn = Callable[[str], None]
USER_AGENT = "fri3d-bulk-flasher"

WCHISP_REPO = "ch32-rs/wchisp"


def _wchisp_exe_name() -> str:
    return "wchisp.exe" if sys.platform == "win32" else "wchisp"


def find_wchisp() -> Path | None:
    """Return the path to a usable wchisp binary, or None."""
    on_path = shutil.which("wchisp")
    if on_path:
        return Path(on_path)
    cached = TOOLS_DIR / _wchisp_exe_name()
    if cached.exists():
        return cached
    return None


def _wchisp_asset_suffix() -> str:
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "darwin":
        return "macos-arm64" if arm else "macos-x64"
    if sys.platform == "win32":
        return "win-x64"
    # linux
    return "linux-aarch64" if arm else "linux-x64"


def download_wchisp(log: LogFn) -> Path:
    """Download the latest wchisp release binary for this platform."""
    url = f"https://api.github.com/repos/{WCHISP_REPO}/releases/latest"
    log("Querying wchisp latest release...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=30, context=ssl_context) as resp:
        release = json.loads(resp.read().decode())

    suffix = _wchisp_asset_suffix()
    asset = None
    for a in release.get("assets", []):
        if suffix in a["name"]:
            asset = a
            break
    if asset is None:
        raise RuntimeError(f"No wchisp build for platform '{suffix}' found")

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    archive = TOOLS_DIR / asset["name"]
    log(f"Downloading {asset['name']}...")
    req = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": USER_AGENT}
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=60, context=ssl_context) as resp, open(archive, "wb") as out:
        shutil.copyfileobj(resp, out)

    exe_name = _wchisp_exe_name()
    dest = TOOLS_DIR / exe_name
    log(f"Extracting {exe_name}...")
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            member = next(m for m in zf.namelist() if m.endswith(exe_name))
            with zf.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    else:
        with tarfile.open(archive) as tf:
            member = next(m for m in tf.getmembers() if m.name.endswith(exe_name))
            src = tf.extractfile(member)
            assert src is not None
            with open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    archive.unlink(missing_ok=True)

    if sys.platform != "win32":
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log(f"wchisp installed at {dest}")
    return dest


def ensure_wchisp(log: LogFn) -> Path:
    path = find_wchisp()
    if path:
        return path
    log("wchisp not found — downloading prebuilt binary from GitHub...")
    return download_wchisp(log)


def esptool_cmd() -> list[str]:
    """Command prefix to invoke esptool.

    In a frozen (PyInstaller) build there is no Python interpreter to call
    `-m esptool` on, so we re-invoke our own executable with a dispatch flag
    handled in app.main().
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--esptool"]
    return [sys.executable, "-m", "esptool"]
