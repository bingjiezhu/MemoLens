#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -x "${PROJECT_ROOT}/.venv/bin/python3" ]; then
  PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "python3 is required but was not found." >&2
  exit 1
fi

exec "${PYTHON_BIN}" "$@"
