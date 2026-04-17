#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

bash ./scripts/prepare_desktop_runtime.sh

exec bash ./scripts/run_desktop_runtime.sh
