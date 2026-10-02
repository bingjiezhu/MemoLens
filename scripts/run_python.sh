#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SQLITE_RUNTIME_CHECKER="${PROJECT_ROOT}/scripts/check_sqlite_runtime.py"

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

if [ -n "${MEMOLENS_PYTHON:-}" ]; then
  if ! command -v "${MEMOLENS_PYTHON}" >/dev/null 2>&1; then
    echo "MEMOLENS_PYTHON is not executable: ${MEMOLENS_PYTHON}" >&2
    exit 1
  fi
  PYTHON_BIN="$(command -v "${MEMOLENS_PYTHON}")"
elif [ -x "${PROJECT_ROOT}/.venv/bin/python3" ]; then
  PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python3"
elif [ -x "${PROJECT_ROOT}/.venv/bin/python" ]; then
  PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "python3 is required but was not found." >&2
  exit 1
fi

if ! run_isolated_python "${PYTHON_BIN}" "${SQLITE_RUNTIME_CHECKER}" --quiet; then
  run_isolated_python "${PYTHON_BIN}" "${SQLITE_RUNTIME_CHECKER}" --json >&2 || true
  echo "MemoLens blocked this command because its Python runtime is unsafe for writable WAL tests." >&2
  echo "Run npm run setup:mac or set MEMOLENS_PYTHON to a safe managed environment." >&2
  exit 78
fi

MEMOLENS_ISOLATED_TARGET_RUNNER='
import runpy
import sys

project_root = sys.argv[1]
arguments = sys.argv[2:]
sys.path.insert(0, project_root)

if not arguments:
    raise SystemExit("MemoLens run_python requires a module, command, or script.")

target = arguments[0]
if target == "-m":
    if len(arguments) < 2:
        raise SystemExit("Argument expected for -m.")
    module_name = arguments[1]
    sys.argv = [module_name, *arguments[2:]]
    runpy.run_module(module_name, run_name="__main__", alter_sys=True)
elif target == "-c":
    if len(arguments) < 2:
        raise SystemExit("Argument expected for -c.")
    sys.argv = ["-c", *arguments[2:]]
    namespace = {
        "__name__": "__main__",
        "__package__": None,
        "__spec__": None,
    }
    exec(compile(arguments[1], "<string>", "exec"), namespace, namespace)
elif target.startswith("-"):
    raise SystemExit(f"Unsupported isolated Python option: {target}")
else:
    sys.argv = [target, *arguments[1:]]
    runpy.run_path(target, run_name="__main__")
'

exec env \
  -u PYTHONPATH \
  -u PYTHONHOME \
  -u PYTHONINSPECT \
  -u PYTHONSTARTUP \
  -u PYTHONUSERBASE \
  "${PYTHON_BIN}" \
  -I \
  -c "${MEMOLENS_ISOLATED_TARGET_RUNNER}" \
  "${PROJECT_ROOT}" \
  "$@"
