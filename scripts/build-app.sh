#!/bin/bash
# Build a local macOS Apple Silicon app; no publishing or installation.
set -euo pipefail
cd "$(dirname "$0")/.."
BUILD_PYTHON="${VINYLFLOW_BUILD_PYTHON:-.venv/bin/python}"
if [ ! -x "$BUILD_PYTHON" ]; then
    echo "Create .venv with Python 3.11+ and install requirements.txt and pyinstaller first." >&2
    exit 1
fi
"$BUILD_PYTHON" -c 'import platform, sys; assert sys.platform == "darwin" and platform.machine() == "arm64", "Use native ARM64 Python on macOS"; assert sys.version_info >= (3, 11)'
export PATH="$PWD/.venv/bin:$PATH"
# Keep PyInstaller's cache in the workspace, including in sandboxed builds.
export PYINSTALLER_CONFIG_DIR="$PWD/build/pyinstaller-cache"
"$BUILD_PYTHON" -m PyInstaller --noconfirm FlipsideFlow.spec
/usr/bin/file dist/FlipsideFlow.app/Contents/MacOS/FlipsideFlow
/usr/bin/lipo -verify_arch arm64 dist/FlipsideFlow.app/Contents/MacOS/FlipsideFlow
/usr/bin/codesign --verify --deep --strict dist/FlipsideFlow.app
echo "Built $PWD/dist/FlipsideFlow.app"
