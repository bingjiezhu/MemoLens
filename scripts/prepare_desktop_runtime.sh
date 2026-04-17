#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

if [ ! -x ".venv/bin/python" ] || [ ! -d "node_modules" ]; then
  bash "${SCRIPT_DIR}/bootstrap_mac.sh"
fi

if [ "$(uname -s)" = "Darwin" ]; then
  case "$(uname -m)" in
    arm64)
      ROLLUP_NATIVE_PACKAGE="@rollup/rollup-darwin-arm64"
      ;;
    x86_64)
      ROLLUP_NATIVE_PACKAGE="@rollup/rollup-darwin-x64"
      ;;
    *)
      ROLLUP_NATIVE_PACKAGE=""
      ;;
  esac

  if [ -n "${ROLLUP_NATIVE_PACKAGE}" ] && ! node -e "require('${ROLLUP_NATIVE_PACKAGE}')" >/dev/null 2>&1; then
    npm install --no-save --no-package-lock "${ROLLUP_NATIVE_PACKAGE}"
  fi
fi

if [ ! -f "dist/index.html" ] || [ ! -f "electron-dist/electron/main.js" ]; then
  npm run build
fi

if [ "$(uname -s)" = "Darwin" ]; then
  bash "${SCRIPT_DIR}/prepare_macos_electron_runtime.sh"
fi
