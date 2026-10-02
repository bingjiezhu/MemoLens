#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

normalize_network_profile() {
  local value="${MEMOLENS_NETWORK_PROFILE-online}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}" | tr '[:upper:]' '[:lower:]'
}

NETWORK_PROFILE="$(normalize_network_profile)"
case "${NETWORK_PROFILE}" in
  online)
    OFFLINE_NETWORKING="no"
    ;;
  offline)
    OFFLINE_NETWORKING="yes"
    ;;
  *)
    echo "MemoLens network profile is invalid; desktop preparation networking is disabled." >&2
    exit 78
    ;;
esac

cd "${PROJECT_ROOT}"

SQLITE_RUNTIME_CHECKER="${PROJECT_ROOT}/scripts/check_sqlite_runtime.py"
VENV_PYTHON="${PROJECT_ROOT}/.venv/bin/python"

run_isolated_python() {
  local candidate="$1"
  shift
  env \
    -u PYTHONPATH \
    -u PYTHONHOME \
    -u PYTHONINSPECT \
    -u PYTHONSTARTUP \
    -u PYTHONUSERBASE \
    "${candidate}" -I "$@"
}

if [ -L "${PROJECT_ROOT}/.venv" ] \
  || [ ! -x "${VENV_PYTHON}" ] \
  || ! run_isolated_python "${VENV_PYTHON}" "${SQLITE_RUNTIME_CHECKER}" --quiet; then
  if [ "${OFFLINE_NETWORKING}" = "yes" ]; then
    echo "MemoLens offline launch needs an already prepared, admitted project .venv." >&2
    echo "Desktop preparation stopped before bootstrap, pip, npm, or any download." >&2
    exit 78
  fi
  bash "${SCRIPT_DIR}/bootstrap_mac.sh"
elif [ "${OFFLINE_NETWORKING}" = "no" ] && [ ! -d "node_modules" ]; then
  bash "${SCRIPT_DIR}/bootstrap_mac.sh"
fi

if [ "${OFFLINE_NETWORKING}" = "no" ] && [ "$(uname -s)" = "Darwin" ]; then
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

if [ "${OFFLINE_NETWORKING}" = "yes" ]; then
  if [ ! -f "dist/index.html" ] \
    || [ ! -f "electron-dist/electron/main.js" ] \
    || [ ! -f "electron-dist/electron/preload.cjs" ]; then
    echo "MemoLens offline launch needs an already prepared renderer and Electron build." >&2
    echo "Desktop preparation stopped before npm or any download." >&2
    exit 78
  fi
else
  npm run build
fi

if [ ! -f "dist/index.html" ] \
  || [ ! -f "electron-dist/electron/main.js" ] \
  || [ ! -f "electron-dist/electron/preload.cjs" ]; then
  echo "MemoLens desktop build is incomplete." >&2
  exit 1
fi

if ! grep -q "Content-Security-Policy" "dist/index.html"; then
  echo "MemoLens renderer build is missing its Content Security Policy." >&2
  exit 1
fi

if [ "$(uname -s)" = "Darwin" ]; then
  bash "${SCRIPT_DIR}/prepare_macos_electron_runtime.sh"
fi
