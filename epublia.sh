#!/usr/bin/env sh
# Usage: ./epublia.sh firstborn
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT" || exit 1
if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
elif [ -x "$ROOT/.venv/Scripts/python.exe" ]; then PY="$ROOT/.venv/Scripts/python.exe"
else PY=python3; fi
exec "$PY" -m epublia "$@"
