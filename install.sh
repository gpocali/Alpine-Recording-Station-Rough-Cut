#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

echo "=================================================================="
echo "  Alpine Recording Station - Rough Cut Unix/macOS Installer"
echo "=================================================================="

PYTHON_BIN=""
for candidate in python3.12 python3.11 python3.13 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3 not found. Attempting automatic installation..."
    if command -v brew >/dev/null 2>&1; then
        brew install python@3.12 ffmpeg
        PYTHON_BIN="$(command -v python3)"
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip ffmpeg libsndfile1
        PYTHON_BIN="$(command -v python3)"
    else
        echo "ERROR: Please install Python 3.10+ and re-run ./install.sh"
        exit 1
    fi
fi

echo "Using Python interpreter: $PYTHON_BIN"
"$PYTHON_BIN" setup_env.py "$@"
