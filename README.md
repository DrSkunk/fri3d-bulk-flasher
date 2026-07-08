# Fri3d Bulk Flasher

Terminal UI for bulk-flashing Fri3d Camp 2026 hardware on macOS, Windows and Linux.

| Device | Chip | Tool | Firmware source |
|---|---|---|---|
| Fri3d Badge 2026 | ESP32-S3 | esptool | [badge_firmware_MicroPythonOS](https://github.com/Fri3dCamp/badge_firmware_MicroPythonOS/releases) (`full_2026_firmware_for_2026_badge.bin`, flashed at `0x0`) |
| Communicator 2026 | CH32X035 | [wchisp](https://github.com/ch32-rs/wchisp) | [communicator_2026](https://github.com/Fri3dCamp/communicator_2026/releases) (`firmware.bin`) |
| DJ Addon 2026 | CH32X035 | [wchisp](https://github.com/ch32-rs/wchisp) | [dj_2026](https://github.com/Fri3dCamp/dj_2026/releases) (`firmware.bin`) |

## Standalone binaries

Prebuilt single-file binaries (no Python needed) are produced by the GitHub
Actions workflow in `.github/workflows/build.yml` for:

- macOS arm64 / x64
- Linux x64 / arm64
- Windows x64 / arm64

Push the repo to GitHub and every push to `main` builds all six as artifacts;
pushing a `v*` tag attaches them to a GitHub Release.

Build one locally for your current platform:

```sh
./build-binary.sh          # -> dist/fri3d-bulk-flasher-<version>
```

(Windows: run the `uv run pyinstaller ...` command from the script in
PowerShell — output is `dist\\fri3d-bulk-flasher-<version>.exe`.)

esptool is bundled inside the binary; wchisp is still downloaded on first use.

## Install & run from source

With [uv](https://docs.astral.sh/uv/) (recommended):

```sh
uv tool install .        # or: uvx --from . fri3d-bulk-flasher
fri3d-bulk-flasher
```

Or with pip:

```sh
pip install .
fri3d-bulk-flasher
```

Or straight from the source tree:

```sh
uv run fri3d-bulk-flasher
```

## Development

Everything goes through [uv](https://docs.astral.sh/uv/) — it creates the
virtualenv and installs dependencies automatically on first run:

```sh
uv run fri3d-bulk-flasher   # run the app; code edits apply on next run
uv sync                     # (re)install deps after changing pyproject.toml
```

Debugging a Textual TUI (the app owns the terminal, so `print()` is useless):

```sh
uv run textual console               # in a second terminal
uv run textual run --dev bulk_flasher.app:BulkFlasherApp
```

`textual run --dev` connects to the console for live log output and enables
CSS hot-reloading (the `textual-dev` tools are in the `dev` dependency group,
installed by `uv sync`/`uv run` automatically).

There is no USB hardware in CI, so exercise flashing logic against fakes: the
port poller and flash call (`_esp_candidate_ports`, `_flash_esptool_slot` in
`bulk_flasher/flasher.py`) are module-level and easy to monkeypatch.

## Usage

1. Pick a device with the arrow keys.
2. Press `f` to fetch the latest firmware release from GitHub (cached in
   `~/.fri3d-bulk-flasher/`). Starting without firmware fetches it automatically.
3. Press `s` to start bulk flashing, then just keep plugging devices in:
   - **Badge**: plug in over USB → it flashes → unplug → plug in the next one.
     Badges flash **in parallel**: every newly plugged-in badge claims a free
     slot (up to 10 by default) and flashes immediately, so you can keep a
     whole USB hub busy at once.
   - **Communicator / DJ Addon**: hold the BOOT button while plugging in USB
     (enters the CH32 bootloader) → it flashes → unplug → next one (wchisp can
     only talk to one bootloader device at a time, so these stay sequential).
4. The status grid shows one card per slot — port, progress bar, and
   success/failure — and adapts its columns to the terminal width. The sidebar
   tracks OK / failed / total counts. Press `s` again (or Stop) to end.

### Configuration

Settings live in `~/.fri3d-bulk-flasher/config.toml` (created on first run):

```toml
# How many badges may be flashed at the same time (1-16).
max_parallel = 10

# Serial baud rate for badge flashing (default: device default, 921600).
# Lower it (e.g. 460800 or 115200) if flashing is unreliable through a hub.
#baud = 921600
```

`esptool` is installed as a Python dependency. `wchisp` is found on your `PATH`,
or automatically downloaded from the [wchisp releases](https://github.com/ch32-rs/wchisp/releases)
on first use.

## Platform notes

- **Linux**: give yourself access to the USB devices —
  serial: `sudo usermod -aG dialout $USER` (log out/in), and for wchisp add a
  udev rule for the WCH bootloader (VID `4348`/`1a86`), e.g.
  `SUBSYSTEM=="usb", ATTR{idVendor}=="4348", MODE="0666"` in
  `/etc/udev/rules.d/50-wchisp.rules`, then `sudo udevadm control --reload`.
- **Windows**: the CH32 bootloader needs the WinUSB driver; if wchisp can't see
  the device, install it with [Zadig](https://zadig.akeo.ie/).
- **macOS**: works out of the box.

## Notes

- The badge is flashed with the full 16 MB image, so every flash produces an
  identical, factory-fresh device (no erase step needed). At 921600 baud a badge
  takes a few minutes.
- The flasher keeps a port claimed until the device is **unplugged**, so it
  never flashes the same unit twice — even with many badges connected at once.
