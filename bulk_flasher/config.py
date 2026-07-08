"""User configuration loaded from ~/.fri3d-bulk-flasher/config.toml."""

from __future__ import annotations

from dataclasses import dataclass

from .devices import CACHE_DIR

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

CONFIG_PATH = CACHE_DIR / "config.toml"
DEFAULT_MAX_PARALLEL = 10
MAX_PARALLEL_LIMIT = 16
BAUD_MIN = 9600
BAUD_MAX = 3_000_000

_DEFAULT_TOML = f"""\
# Fri3d bulk flasher configuration.

# How many badges may be flashed at the same time (1-{MAX_PARALLEL_LIMIT}).
# Only applies to esptool devices (the badge); the CH32 devices are always
# flashed one at a time because wchisp cannot target a specific unit.
max_parallel = {DEFAULT_MAX_PARALLEL}

# Serial baud rate for badge flashing. Uncomment to override the device
# default (921600). Lower it (e.g. 460800 or 115200) if flashing is
# unreliable through a hub, or raise it if your adapters can take it.
#baud = 921600
"""


@dataclass(frozen=True)
class Config:
    max_parallel: int = DEFAULT_MAX_PARALLEL
    baud: int | None = None  # None = use the device's default


def load_config() -> Config:
    """Read config.toml, creating it with defaults on first run.

    Never raises: an unreadable or invalid file falls back to defaults so the
    app always starts.
    """
    if not CONFIG_PATH.exists():
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(_DEFAULT_TOML)
        except OSError:
            pass
        return Config()
    try:
        data = tomllib.loads(CONFIG_PATH.read_text())
        max_parallel = int(data.get("max_parallel", DEFAULT_MAX_PARALLEL))
        baud = data.get("baud")
        baud = None if baud is None else int(baud)
    except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
        return Config()
    if baud is not None:
        baud = max(BAUD_MIN, min(BAUD_MAX, baud))
    return Config(
        max_parallel=max(1, min(MAX_PARALLEL_LIMIT, max_parallel)),
        baud=baud,
    )
