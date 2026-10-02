#!/usr/bin/env bash
set -euo pipefail

slice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${slice_dir}/../../../../.." && pwd)"
cd "${repo_root}"

run_python_contract() {
  local pattern="$1"
  bash scripts/run_python.sh -m unittest discover -s tests -p "${pattern}" -v
}

run_python_contract 'test_backend_regressions.py'
run_python_contract 'test_video_hardening.py'
run_python_contract 'test_strict_http_json.py'
run_python_contract 'test_b1_backend_protocol.py'
bash scripts/run_python.sh -m unittest discover \
  -s .agents/plugins/plugins/memolens/tests -p 'test_media_wiki.py' -v
npm run build:electron
node --experimental-strip-types --test \
  tests/backend_manager_health.test.mjs \
  tests/agent_authority_coordinator.test.mjs \
  tests/artifact_integrity.test.mjs
