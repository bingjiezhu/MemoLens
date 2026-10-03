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

if [ -L "${PROJECT_ROOT}/.venv" ]; then
  echo "MemoLens refuses to use .venv when it is a symbolic link." >&2
  echo "Replace it with a real project-local directory, then run setup again." >&2
  exit 1
fi

NETWORK_PROFILE="$(normalize_network_profile)"
case "${NETWORK_PROFILE}" in
  online)
    ;;
  offline)
    echo "MemoLens setup cannot provision dependencies while MEMOLENS_NETWORK_PROFILE=offline." >&2
    echo "No brew, pip, npm, or Electron download was attempted. Prepare once while online, then launch offline." >&2
    exit 78
    ;;
  *)
    echo "MemoLens network profile is invalid; setup networking is disabled." >&2
    exit 78
    ;;
esac

cd "${PROJECT_ROOT}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required but was not found."
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing FFmpeg for video understanding, previews, and exports..."
    brew install ffmpeg
  else
    echo "MemoLens video workflows require FFmpeg 6 or newer." >&2
    echo "Install Homebrew from https://brew.sh, then run: brew install ffmpeg" >&2
    exit 1
  fi
fi

FFMPEG_MAJOR="$(ffmpeg -version | awk 'NR == 1 { print $3 }' | sed -E 's/^[^0-9]*([0-9]+).*/\1/')"
if ! [[ "${FFMPEG_MAJOR}" =~ ^[0-9]+$ ]] || [ "${FFMPEG_MAJOR}" -lt 6 ]; then
  echo "MemoLens requires FFmpeg 6 or newer; found: $(ffmpeg -version | head -n 1)" >&2
  exit 1
fi

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

python_is_supported() {
  local candidate="$1"
  run_isolated_python \
    "${candidate}" \
    -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' \
    >/dev/null 2>&1
}

sqlite_runtime_is_safe() {
  local candidate="$1"
  run_isolated_python \
    "${candidate}" \
    "${SQLITE_RUNTIME_CHECKER}" \
    --quiet \
    >/dev/null 2>&1
}

resolve_python() {
  local candidate="$1"
  command -v "${candidate}" 2>/dev/null || true
}

explain_unsafe_runtime() {
  local candidate="$1"
  run_isolated_python "${candidate}" "${SQLITE_RUNTIME_CHECKER}" --json >&2 || true
}

PYTHON_BIN=""
VENV_REBUILD_REQUIRED="no"
REQUESTED_PYTHON="${MEMOLENS_PYTHON:-}"

if [ -n "${REQUESTED_PYTHON}" ]; then
  PYTHON_BIN="$(resolve_python "${REQUESTED_PYTHON}")"
  if [ -z "${PYTHON_BIN}" ] || ! python_is_supported "${PYTHON_BIN}"; then
    echo "MEMOLENS_PYTHON must name an executable Python 3.10 or newer: ${REQUESTED_PYTHON}" >&2
    exit 1
  fi
  if ! sqlite_runtime_is_safe "${PYTHON_BIN}"; then
    echo "MEMOLENS_PYTHON uses an SQLite runtime that MemoLens cannot use for writable WAL access." >&2
    explain_unsafe_runtime "${PYTHON_BIN}"
    echo "Choose a Python runtime admitted by the shared MemoLens SQLite policy." >&2
    exit 1
  fi
fi

if [ ! -x "${VENV_PYTHON}" ] || ! python_is_supported "${VENV_PYTHON}"; then
  VENV_REBUILD_REQUIRED="yes"
elif ! sqlite_runtime_is_safe "${VENV_PYTHON}"; then
  echo "Rebuilding .venv because its SQLite runtime is unsafe for concurrent WAL writes."
  explain_unsafe_runtime "${VENV_PYTHON}"
  VENV_REBUILD_REQUIRED="yes"
fi

if [ "${VENV_REBUILD_REQUIRED}" = "yes" ] && [ -z "${PYTHON_BIN}" ]; then
  PYTHON_CANDIDATES=(python3.14 python3.13 python3.12 python3.11 python3.10 python3)
  for candidate in "${PYTHON_CANDIDATES[@]}"; do
    resolved_candidate="$(resolve_python "${candidate}")"
    if [ -n "${resolved_candidate}" ] \
      && python_is_supported "${resolved_candidate}" \
      && sqlite_runtime_is_safe "${resolved_candidate}"; then
      PYTHON_BIN="${resolved_candidate}"
      break
    fi
  done
fi

if [ "${VENV_REBUILD_REQUIRED}" = "yes" ] && [ -z "${PYTHON_BIN}" ]; then
  echo "MemoLens could not find a safe Python 3.10+ runtime for its writable WAL database." >&2
  echo "Install a runtime admitted by the shared policy, or set MEMOLENS_PYTHON to that executable." >&2
  exit 1
fi

NODE_SUPPORTED="$(node -p 'const [major, minor] = process.versions.node.split(".").map(Number); (major > 22 || (major === 22 && minor >= 12)) ? "yes" : "no"')"
if [ "${NODE_SUPPORTED}" != "yes" ]; then
  echo "MemoLens requires Node.js 22.12 or newer." >&2
  exit 1
fi

if [ "${VENV_REBUILD_REQUIRED}" = "yes" ] && [ -d ".venv" ]; then
  echo "Building the managed .venv with ${PYTHON_BIN}."
  run_isolated_python "${PYTHON_BIN}" -m venv --clear .venv
elif [ "${VENV_REBUILD_REQUIRED}" = "yes" ]; then
  run_isolated_python "${PYTHON_BIN}" -m venv .venv
fi

source .venv/bin/activate
if ! run_isolated_python "${VENV_PYTHON}" "${SQLITE_RUNTIME_CHECKER}" --quiet; then
  run_isolated_python "${VENV_PYTHON}" "${SQLITE_RUNTIME_CHECKER}" --json >&2 || true
  echo "The managed .venv failed MemoLens SQLite runtime admission." >&2
  exit 1
fi
run_isolated_python "${VENV_PYTHON}" -m pip install --upgrade pip
run_isolated_python "${VENV_PYTHON}" -m pip install -r requirements.txt
npm ci

npm run build

bash "${SCRIPT_DIR}/prepare_macos_electron_runtime.sh"

cat <<'EOF'

MemoLens desktop setup is ready.

Next step:
  ./Launch\ MemoLens.command

The Electron shell will now try to auto-start the local backend by using:
  .venv/bin/python

If you want to install the optional legacy local model stack later:
  source .venv/bin/activate
  pip install -r requirements-local-models.txt

EOF
