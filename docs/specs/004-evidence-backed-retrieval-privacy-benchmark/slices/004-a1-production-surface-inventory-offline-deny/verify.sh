#!/usr/bin/env bash
set -euo pipefail

slice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${slice_dir}/../../../../.." && pwd)"
cd "${repo_root}"

run_python_file() {
  local test_path="$1"
  if [[ ! -f "${test_path}" ]]; then
    echo "A1 gate is missing required Python test: ${test_path}" >&2
    return 1
  fi
  bash scripts/run_python.sh -m unittest discover \
    -s "$(dirname "${test_path}")" \
    -p "$(basename "${test_path}")" \
    -v
}

# The compiled Electron modules are the production modules consumed by the
# Node oracles; renderer type-check/build also proves the local API boundary.
npm run build:renderer
npm run build:electron

# Compile the Photon sources consumed by its exact TypeScript oracles.
(
  cd photon-bot
  npm run typecheck
)

# Execute every hash-bound oracle by its exact selector.  The runner requires
# exactly one unqualified pass per selector (zero skips, failures, cancellations,
# todos, expected failures, and unexpected successes).
bash scripts/run_python.sh scripts/run_production_oracles.py

# These journeys close the setup/runtime network gate but are not negative
# oracles for one individual production action.
run_python_file tests/test_setup_offline_policy.py
run_python_file tests/test_offline_production_journey.py
run_python_file tests/test_photo_atlas_read_purity.py
run_python_file .agents/plugins/plugins/memolens/tests/test_deepseek_harness.py

node --experimental-strip-types --experimental-loader ./tests/ts-extension-loader.mjs \
  --test tests/local_api_base.test.mjs tests/timeline_preview_model.test.mjs
MEMOLENS_NETWORK_PROFILE=offline node --input-type=module <<'JS'
import {
  getElectronNetworkPolicyObservation,
  resetElectronNetworkPolicyCountersForTests,
} from "./electron-dist/electron/networkPolicy.js";

resetElectronNetworkPolicyCountersForTests();
process.stdout.write(`${JSON.stringify({
  gate: "ML-004-A1/ML-007-A1",
  journey: "electron-offline-policy-scope",
  observation: getElectronNetworkPolicyObservation(),
})}\n`);
JS

.venv/bin/python - <<'PY'
from __future__ import annotations

import json
from collections import Counter

from core.production_surface_inventory import (
    high_impact_oracle_gaps,
    load_production_surface_inventory,
)

inventory = load_production_surface_inventory()
gaps = high_impact_oracle_gaps(inventory)
if gaps:
    print(
        json.dumps(
            {
                "gate": "ML-004-A1/ML-007-A1",
                "status": "failed",
                "reason": "high_impact_oracle_gaps",
                "gaps": gaps,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    raise SystemExit(1)
counts = Counter(row["surface"] for row in inventory["actions"])
print(
    json.dumps(
        {
            "gate": "ML-004-A1/ML-007-A1",
            "status": "passed",
            "inventory_sha256": inventory["inventory_sha256"],
            "action_count": len(inventory["actions"]),
            "negative_oracle_count": len(inventory["negative_oracles"]),
            "surface_counts": dict(sorted(counts.items())),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
