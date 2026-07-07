#!/bin/sh
# Build a standalone binary for the current platform with PyInstaller.
# Usage: ./build-binary.sh  ->  dist/fri3d-bulk-flasher-<version>[.exe]
set -e

VERSION="$(python - <<'PY'
import tomllib
from pathlib import Path
data = tomllib.loads(Path('pyproject.toml').read_text(encoding='utf-8'))
print(data['project']['version'])
PY
)"
BIN_NAME="fri3d-bulk-flasher-${VERSION}"

uv sync --group build 2>/dev/null || uv pip install -e . "pyinstaller>=6.10"
uv run pyinstaller \
    --onefile \
    --name "$BIN_NAME" \
    --collect-submodules textual \
    --collect-submodules esptool \
    --collect-data esptool \
    --copy-metadata esptool \
    --collect-submodules serial \
    --clean --noconfirm \
    run.py
