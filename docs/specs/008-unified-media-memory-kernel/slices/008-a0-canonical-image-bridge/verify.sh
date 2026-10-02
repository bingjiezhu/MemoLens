#!/usr/bin/env bash
set -euo pipefail

slice_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${slice_dir}/../../../../.." && pwd)"
cd "${repo_root}"

run_python_pattern() {
  local pattern="$1"
  bash scripts/run_python.sh -m unittest discover -s tests -p "${pattern}" -v
}

run_python_file() {
  local test_path="$1"
  if [[ ! -f "${test_path}" ]]; then
    echo "A0.1 gate is missing required Python test: ${test_path}" >&2
    return 1
  fi
  bash scripts/run_python.sh -m unittest discover \
    -s "$(dirname "${test_path}")" \
    -p "$(basename "${test_path}")" \
    -v
}

# Closed contracts, exact current V17 migration/immutability, durable jobs,
# database/source admission, atomic publication, projection, alias and Atlas.
run_python_pattern 'test_image*.py'

# Production compatibility/backfill and the callers around the image bridge.
run_python_file tests/test_media_import_service.py
run_python_file tests/test_runtime_generation.py
run_python_file tests/test_legacy_image_backfill.py
run_python_file tests/test_legacy_image_index_adapter.py
run_python_file tests/test_legacy_route_authority.py
run_python_file tests/test_photo_atlas_read_purity.py
run_python_file tests/test_backend_regressions.py
run_python_file tests/test_index_status_read_cutover.py
run_python_file tests/test_sqlite_setup.py

# The Codex plugin is a production consumer.  This required focused gate must
# reject legacy-only, stale, wrong-database and tampered image evidence.
run_python_file .agents/plugins/plugins/memolens/tests/test_canonical_image_authority.py
node --experimental-strip-types --test tests/index_status_model.test.mjs

# A0.1 adds filesystem reads/writes and mutation surfaces.  Reuse the exact
# inventory/oracle/offline gate rather than maintaining a weaker second list.
bash docs/specs/004-evidence-backed-retrieval-privacy-benchmark/slices/004-a1-production-surface-inventory-offline-deny/verify.sh

ruff check \
  core/db.py \
  core/image_analysis_contract.py \
  core/image_analysis_job_contract.py \
  core/image_analysis_persistence.py \
  core/image_analysis_schema.py \
  core/image_projection_renderer.py \
  core/image_read_cutover_schema.py \
  core/media_db.py \
  core/provider_egress_contract.py \
  core/provider_egress_persistence.py \
  core/provider_egress_schema.py \
  core/photo_atlas.py \
  backend/src/api/routes.py \
  backend/src/media/image_analysis.py \
  backend/src/media/legacy_image_backfill.py \
  backend/src/media/provider_egress.py \
  backend/src/retrieval/retrieval.py \
  tests/test_image_analysis_contract.py \
  tests/test_image_analysis_job_contract.py \
  tests/test_image_analysis_persistence.py \
  tests/test_image_analysis_runner.py \
  tests/test_image_atlas_consumer.py \
  tests/test_image_attempt_lifecycle.py \
  tests/test_image_bridge_migration.py \
  tests/test_image_bridge_fault_matrix.py \
  tests/test_image_checkpoint_process_matrix.py \
  tests/test_image_import_enqueue.py \
  tests/test_image_mixed_consumer.py \
  tests/test_image_projection_alias_snapshot_schema.py \
  tests/test_image_projection_materialization.py \
  tests/test_image_projection_outcomes.py \
  tests/test_image_projection_projector.py \
  tests/test_image_projection_renderer.py \
  tests/test_image_projection_schema_guards.py \
  tests/test_image_projection_verified_read.py \
  tests/test_image_read_cutover.py \
  tests/test_image_read_cutover_migration.py \
  tests/test_image_provider_egress_service.py \
  tests/test_index_status_read_cutover.py \
  tests/test_legacy_image_backfill.py \
  tests/test_legacy_image_index_adapter.py \
  tests/test_legacy_route_authority.py \
  tests/test_photo_atlas_read_purity.py \
  tests/test_provider_egress_contract.py \
  tests/test_provider_egress_migration.py \
  tests/test_provider_egress_persistence.py \
  tests/test_provider_send_permit.py \
  tests/test_retrieval_read_cutover.py \
  .agents/plugins/plugins/memolens/scripts/memolens_blueprint_authority.py \
  .agents/plugins/plugins/memolens/scripts/memolens_image_authority.py \
  .agents/plugins/plugins/memolens/scripts/memolens_photo_store.py \
  .agents/plugins/plugins/memolens/tests/test_b1_agent_receipts.py \
  .agents/plugins/plugins/memolens/tests/test_blueprint_authority.py \
  .agents/plugins/plugins/memolens/tests/test_canonical_image_authority.py

git diff --check

.venv/bin/python - <<'PY'
from __future__ import annotations

import json

from core.production_surface_inventory import (
    high_impact_oracle_gaps,
    load_production_surface_inventory,
)

inventory = load_production_surface_inventory()
gaps = high_impact_oracle_gaps(inventory)
if gaps:
    raise SystemExit(f"high-impact production actions lack exact oracles: {gaps}")
print(
    json.dumps(
        {
            "gate": "ML-008-A0.1",
            "status": "focused_implementation_gate_passed",
            "promotion": "blocked_until_documented_residuals_are_closed",
            "inventory_sha256": inventory["inventory_sha256"],
            "action_count": len(inventory["actions"]),
            "negative_oracle_count": len(inventory["negative_oracles"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)
PY
