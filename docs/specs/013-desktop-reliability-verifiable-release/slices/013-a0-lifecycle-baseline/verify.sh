#!/usr/bin/env bash
set -euo pipefail

slice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${slice_dir}/../../../../.." && pwd)"
cd "${repo_root}"

npm run build:electron
node --experimental-strip-types --test \
  tests/backend_manager_health.test.mjs \
  tests/indexing_coordinator.test.mjs \
  tests/video_job_model.test.mjs
bash scripts/run_python.sh -m unittest discover \
  -s tests -p 'test_video_media.py' -k StartupRecoveryContractTests -v
bash scripts/run_python.sh -m unittest discover \
  -s tests -p 'test_sqlite_runtime.py' -v
npm run typecheck
