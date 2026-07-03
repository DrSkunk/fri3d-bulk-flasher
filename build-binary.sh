#!/bin/sh
# Build a standalone binary for the current platform with PyInstaller.
# Usage: ./build-binary.sh  ->  dist/fri3d-bulk-flasher[.exe]
set -e
uv sync --group build 2>/dev/null || uv pip install -e . "pyinstaller>=6.10"
uv run pyinstaller \
    --onefile \
    --name fri3d-bulk-flasher \
    --collect-submodules textual \
    --collect-submodules esptool \
    --collect-data esptool \
    --copy-metadata esptool \
    --collect-submodules serial \
    --clean --noconfirm \
    run.py
