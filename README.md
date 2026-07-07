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

## Usage

1. Pick a device with the arrow keys.
2. Press `f` to fetch the latest firmware release from GitHub (cached in
   `~/.fri3d-bulk-flasher/`). Starting without firmware fetches it automatically.
3. Press `s` to start bulk flashing, then just keep plugging devices in:
   - **Badge**: plug in over USB → it flashes → unplug → plug in the next one.
   - **Communicator / DJ Addon**: hold the BOOT button while plugging in USB
     (enters the CH32 bootloader) → it flashes → unplug → next one.
4. The sidebar tracks OK / failed / total counts. Press `s` again (or Stop) to end.

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
- The flasher waits for the device to be **unplugged** before arming for the
  next one, so it never flashes the same unit twice.
