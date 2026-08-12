#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

if ! command -v npm >/dev/null 2>&1; then
  echo "npm is required but was not found."
  exit 1
fi

PYTHON_BIN="${MEMOLENS_PYTHON:-}"
if [ -n "${PYTHON_BIN}" ]; then
  PYTHON_CANDIDATES=("${PYTHON_BIN}")
else
  PYTHON_CANDIDATES=(python3.13 python3.12 python3.11 python3.10 python3)
fi

PYTHON_BIN=""
for candidate in "${PYTHON_CANDIDATES[@]}"; do
  if command -v "${candidate}" >/dev/null 2>&1 && "${candidate}" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))'; then
    PYTHON_BIN="$(command -v "${candidate}")"
    break
  fi
done

if [ -z "${PYTHON_BIN}" ]; then
  echo "MemoLens requires Python 3.10 or newer (3.11 recommended)." >&2
  echo "Install a current Python, or set MEMOLENS_PYTHON to its executable." >&2
  exit 1
fi

NODE_SUPPORTED="$(node -p 'const [major, minor] = process.versions.node.split(".").map(Number); (major > 22 || (major === 22 && minor >= 12)) ? "yes" : "no"')"
if [ "${NODE_SUPPORTED}" != "yes" ]; then
  echo "MemoLens requires Node.js 22.12 or newer." >&2
  exit 1
fi

if [ -d ".venv" ] && ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
  echo "Rebuilding .venv with ${PYTHON_BIN} because the existing environment uses an older Python."
  "${PYTHON_BIN}" -m venv --clear .venv
elif [ ! -d ".venv" ]; then
  "${PYTHON_BIN}" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
npm install

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
