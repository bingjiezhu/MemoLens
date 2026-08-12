#!/usr/bin/env bash
set -euo pipefail

if [ "$(uname -s)" != "Darwin" ]; then
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SOURCE_APP="${PROJECT_ROOT}/node_modules/electron/dist/Electron.app"
APP_STATE_DIR="${HOME}/Library/Application Support/MemoLens"
APP_STATE_DIR="${MEMOLENS_APP_STATE_DIR:-${APP_STATE_DIR}}"
RUNTIME_ROOT="${APP_STATE_DIR}/runtime"
TARGET_APP="${RUNTIME_ROOT}/Electron.app"

if [ ! -d "${SOURCE_APP}" ]; then
  ELECTRON_BIN="${PROJECT_ROOT}/node_modules/.bin/electron"
  if [ ! -x "${ELECTRON_BIN}" ]; then
    echo "Electron is not installed under node_modules. Run npm install first." >&2
    exit 1
  fi

  echo "Downloading the Electron runtime for this platform..."
  env -u ELECTRON_RUN_AS_NODE "${ELECTRON_BIN}" --version >/dev/null
fi

if [ ! -d "${SOURCE_APP}" ]; then
  echo "Electron.app is still missing after the runtime download." >&2
  exit 1
fi

mkdir -p "${RUNTIME_ROOT}"
rm -rf "${TARGET_APP}"
ditto "${SOURCE_APP}" "${TARGET_APP}"
xattr -cr "${TARGET_APP}" || true
codesign --force --deep --sign - "${TARGET_APP}" >/dev/null

if ! codesign --verify --deep --verbose=2 "${TARGET_APP}" >/dev/null 2>&1; then
  echo "Prepared Electron runtime did not pass codesign verification."
  exit 1
fi
