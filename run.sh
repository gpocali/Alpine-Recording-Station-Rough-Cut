#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    echo "Virtual environment not found. Running ./install.sh first..."
    bash ./install.sh
fi

URL="http://127.0.0.1:${PORT}"
echo "Starting Alpine Recording Station Rough Cut at ${URL} ..."

if [ "${NO_BROWSER:-0}" != "1" ]; then
    (
        sleep 2
        if command -v open >/dev/null 2>&1; then
            open "$URL"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$URL"
        fi
    ) &
fi

exec "$VENV_PY" -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
