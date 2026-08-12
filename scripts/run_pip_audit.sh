#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${PROJECT_ROOT}"

if bash "${SCRIPT_DIR}/run_python.sh" -c "import pip_audit" >/dev/null 2>&1; then
  exec bash "${SCRIPT_DIR}/run_python.sh" -m pip_audit
fi

if command -v pip-audit >/dev/null 2>&1; then
  exec pip-audit
fi

if command -v uvx >/dev/null 2>&1; then
  SITE_PACKAGES="$(
    bash "${SCRIPT_DIR}/run_python.sh" -c \
      'import sysconfig; print(sysconfig.get_paths()["purelib"])'
  )"
  exec uvx --python 3.12 "pip-audit>=2.10,<3" --path "${SITE_PACKAGES}"
fi

echo "pip-audit is required; install requirements-dev.txt or uv." >&2
exit 1
