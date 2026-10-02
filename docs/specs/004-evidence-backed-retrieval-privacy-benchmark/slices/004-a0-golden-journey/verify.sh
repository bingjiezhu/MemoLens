#!/usr/bin/env bash
set -euo pipefail

slice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${slice_dir}/../../../../.." && pwd)"
cd "${repo_root}"

run_python_contract() {
  local pattern="$1"
  bash scripts/run_python.sh -m unittest discover -s tests -p "${pattern}" -v
}

run_python_contract 'test_media_import_service.py'
run_python_contract 'test_video_media.py'
node --experimental-strip-types --experimental-loader ./tests/ts-extension-loader.mjs \
  --test tests/video_api.test.mjs tests/video_workflow.test.mjs
