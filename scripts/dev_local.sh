#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

BACKEND_HOST="${MEMOLENS_BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${MEMOLENS_BACKEND_PORT:-5519}"
FRONTEND_HOST="${MEMOLENS_FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${MEMOLENS_FRONTEND_PORT:-5173}"
PYTHON_BIN="${MEMOLENS_PYTHON:-}"

if [ -z "${PYTHON_BIN}" ]; then
  if [ -x ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

backend_pid=""

backend_is_memolens() {
  local health_url="http://${BACKEND_HOST}:${BACKEND_PORT}/healthz"
  "${PYTHON_BIN}" - "${health_url}" <<'PY'
import json
import sys
from urllib.request import urlopen

try:
    with urlopen(sys.argv[1], timeout=1.5) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1)

valid = (
    isinstance(payload, dict)
    and payload.get("status") == "ok"
    and payload.get("service") == "memolens-backend"
    and payload.get("api_version") == "1"
)
raise SystemExit(0 if valid else 1)
PY
}

cleanup() {
  if [ -n "${backend_pid}" ] && kill -0 "${backend_pid}" >/dev/null 2>&1; then
    kill "${backend_pid}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

if backend_is_memolens; then
  echo "MemoLens local service is already running at http://${BACKEND_HOST}:${BACKEND_PORT}"
else
  echo "Starting MemoLens local service at http://${BACKEND_HOST}:${BACKEND_PORT}"
  MEMOLENS_BACKEND_HOST="${BACKEND_HOST}" \
  MEMOLENS_BACKEND_PORT="${BACKEND_PORT}" \
  MEMOLENS_FRONTEND_PORT="${FRONTEND_PORT}" \
  MEMOLENS_BACKEND_DEBUG="${MEMOLENS_BACKEND_DEBUG:-0}" \
  PYTHONUNBUFFERED=1 \
    "${PYTHON_BIN}" backend/app.py &
  backend_pid="$!"

  for _ in $(seq 1 40); do
    if backend_is_memolens; then
      break
    fi
    sleep 0.25
  done

  if ! backend_is_memolens; then
    echo "MemoLens local service did not become healthy." >&2
    exit 1
  fi
fi

echo "Starting MemoLens UI at http://${FRONTEND_HOST}:${FRONTEND_PORT}"
exec npm run dev -- --host "${FRONTEND_HOST}" --port "${FRONTEND_PORT}"
