#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ "$(uname -s)" = "Darwin" ]; then
  APP_STATE_DIR="${MEMOLENS_APP_STATE_DIR:-${HOME}/Library/Application Support/MemoLens}"
  RUNTIME_APP="${APP_STATE_DIR}/runtime/Electron.app"
  RUNTIME_BIN="${RUNTIME_APP}/Contents/MacOS/Electron"

  if [ ! -x "${RUNTIME_BIN}" ]; then
    echo "Prepared Electron runtime was not found at:"
    echo "  ${RUNTIME_BIN}"
    echo "Run ./Launch\\ MemoLens.command or bash ./scripts/prepare_desktop_runtime.sh first."
    exit 1
  fi

  cd "${PROJECT_ROOT}"
  exec open -na "${RUNTIME_APP}" --args "${PROJECT_ROOT}"
fi

cd "${PROJECT_ROOT}"
exec env -u ELECTRON_RUN_AS_NODE electron .
