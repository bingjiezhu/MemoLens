#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
APP_STATE_DIR="${HOME}/Library/Application Support/MemoLens"
RUNTIME_ROOT="${APP_STATE_DIR}/runtime"
exec "${RUNTIME_ROOT}/Electron.app/Contents/MacOS/Electron" "${PROJECT_ROOT}" --remote-debugging-port=9223 "$@"
