from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from backend.src.media.usage_projection import (
    project_asset_usage,
    project_candidate_usage,
)
from core.blueprint_contract import (
    BLUEPRINT_RESIDUAL_SCHEMA_SHA256,
    BLUEPRINT_SCHEMA_SHA256,
    blueprint_residual_proofs,
    validate_blueprint_semantic,
)
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, content_sha256
from core.residual_identity_contract import (
    build_residual_binding,
    source_binding_sha256,
)
from tests import test_canonical_export_persistence as export_fixture
from tests import test_timeline_lowering_integration as timeline_fixture
from tests.test_coverage_persistence import semantic


class A3CanonicalResidualChainTests(unittest.TestCase):
    _register_image = timeline_fixture.TimelineLoweringIntegrationTests._register_image
    _register_video_spans = (
        timeline_fixture.TimelineLoweringIntegrationTests._register_video_spans
    )
    _materialize = timeline_fixture.TimelineLoweringIntegrationTests._materialize
    _materialize_semantic_project = (
        export_fixture.CanonicalExportPersistenceTests._materialize_semantic_project
    )
    _present_existing_project = (
        export_fixture.CanonicalExportPersistenceTests._present_existing_project
    )
    _advance_commit_pending = (
        export_fixture.CanonicalExportPersistenceTests._advance_commit_pending
    )
    _completion_inputs = (
        export_fixture.CanonicalExportPersistenceTests._completion_inputs
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-a3-canonical-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.output = self.root / "export"
        self.output.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _successful_partial_parent_export(
        self,
    ) -> tuple[str, str, str, dict[str, object]]:
        span_ref = self._register_video_spans(((0, 60_000),))[0]
        parent_segment_id = span_ref.rsplit("/", 1)[1]
        asset_id = f"asset_{parent_segment_id.split('_')[1]}"
        self.repository.update_asset_probe(
            asset_id,
            {
                "duration_ms": 60_000,
                "width": 1920,
                "height": 1080,
                "rotation_degrees": 0,
                "codec": {"video": "fixture"},
                "captured_at": None,
            },
        )
        ending = self._register_image("a3-ending.jpg", b"a3-ending")
        proposed = semantic("a3-parent", evidence_ref=span_ref)
        proposed["material_hints"][0]["reason"] = "Use part of the exact parent span."
        proposed["material_hints"].append(
            {
                "hint_id": "a3_ending",
                "evidence_ref": f"memolens://evidence/asset/{ending['id']}",
                "script_block_ids": ["ending"],
                "reason": "Close on one exact image.",
            }
        )
        project_id, _blueprint = self._materialize_semantic_project(
            proposed,
            key="a3-parent",
        )
        presented, command = self._present_existing_project(
            project_id,
            key="a3-parent-export",
            nonce="a3-parent-nonce",
            package_basename="a3-parent-film",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            video_sha256="e" * 64,
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        current_candidates, _analysis_heads = self.repository.mixed_candidates()
        current_asset_ids = sorted(
            {str(candidate["asset_id"]) for candidate in current_candidates}
        )
        facts = self.repository.canonical_material_search_facts(current_asset_ids)
        usage_occurrences = facts["usage_occurrences"]
        self.assertIsInstance(usage_occurrences, list)
        self.assertTrue(
            any(item.get("asset_id") != asset_id for item in usage_occurrences),
            "fixture must prove Usage revision over more than the residual asset",
        )
        asset_usage = project_asset_usage(
            asset_id=asset_id,
            media_kind="video",
            duration_ms=60_000,
            occurrences=usage_occurrences,
        )
        parent_usage = project_candidate_usage(
            {
                "id": parent_segment_id,
                "asset_id": asset_id,
                "result_type": "video_segment",
                "start_ms": 0,
                "end_ms": 60_000,
            },
            asset_usage,
        )
        residuals = parent_usage["residual_intervals"]
        self.assertIsInstance(residuals, list)
        self.assertTrue(parent_usage["used_intervals"])
        self.assertTrue(residuals)
        source_in_ms, source_out_ms = residuals[0]
        with self.repository.transaction() as connection:
            row = connection.execute(
                """SELECT segment.analysis_run_id,run.revision,
                          run.input_asset_sha256,asset.sha256,
                          source.id AS asset_source_id,source.library_root_id,
                          source.observed_size,source.observed_mtime_ns,
                          source.source_file_id
                     FROM video_segments segment
                     JOIN analysis_runs run ON run.id=segment.analysis_run_id
                     JOIN assets asset ON asset.id=segment.asset_id
                     JOIN asset_sources source ON source.asset_id=asset.id
                    WHERE segment.id=?
                    ORDER BY source.is_preferred DESC,source.id ASC LIMIT 1""",
                (parent_segment_id,),
            ).fetchone()
        assert row is not None
        source_digest = source_binding_sha256(
            {
                "asset_source_id": str(row["asset_source_id"]),
                "asset_id": asset_id,
                "asset_sha256": str(row["sha256"]),
                "library_root_id": str(row["library_root_id"]),
                "observed_size": row["observed_size"],
                "observed_mtime_ns": row["observed_mtime_ns"],
                "source_file_id": row["source_file_id"],
            }
        )
        binding = build_residual_binding(
            parent_segment_id=parent_segment_id,
            asset_id=asset_id,
            asset_sha256=str(row["sha256"]),
            asset_source_id=str(row["asset_source_id"]),
            source_binding_sha256=source_digest,
            analysis_run_id=str(row["analysis_run_id"]),
            analysis_revision=int(row["revision"]),
            input_asset_sha256=str(row["input_asset_sha256"]),
            parent_start_ms=0,
            parent_end_ms=60_000,
            source_in_ms=int(source_in_ms),
            source_out_ms=int(source_out_ms),
            usage_revision=content_sha256(usage_occurrences),
            parent_usage_projection_sha256=content_sha256(parent_usage),
        )
        return project_id, asset_id, parent_segment_id, binding

    def _materialize_residual_project(
        self,
        binding: dict[str, object],
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        residual_ref = f"memolens://evidence/span/{binding['residual_id']}"
        ending = self._register_image("a3-residual-ending.jpg", b"a3-residual-ending")
        proposed = semantic("a3-residual", evidence_ref=residual_ref)
        proposed["material_hints"][0] = {
            **proposed["material_hints"][0],
            "hint_id": "a3_residual",
            "reason": "Use only the exact residual remainder.",
            "proof": {
                "kind": "residual_span",
                "residual_binding": deepcopy(binding),
            },
        }
        proposed["material_hints"].append(
            {
                "hint_id": "a3_residual_ending",
                "evidence_ref": f"memolens://evidence/asset/{ending['id']}",
                "script_block_ids": ["ending"],
                "reason": "Close on one exact image.",
            }
        )
        project = self.repository.create_project(
            "A3 residual chain",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "a3-canonical-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key="a3-residual-blueprint",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            field: blueprint[field]
            for field in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: blueprint_binding[field]
                    for field in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": None,
            },
            idempotency_key="a3-residual-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        self._materialize(
            project_id,
            {
                "expected_blueprint": blueprint_binding,
                "expected_coverage": {
                    field: coverage[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    )
                },
                "expected_timeline_head": None,
            },
            key="a3-residual-timeline",
        )
        return project_id, blueprint, coverage

    def test_real_usage_residual_flows_blueprint_coverage_timeline_without_rseg_row(
        self,
    ) -> None:
        _usage_project, _asset_id, parent_segment_id, binding = (
            self._successful_partial_parent_export()
        )
        project_id, blueprint, coverage = self._materialize_residual_project(binding)

        document = blueprint["blueprint"]
        self.assertEqual(document["schema_sha256"], BLUEPRINT_RESIDUAL_SCHEMA_SHA256)
        residual_ref = f"memolens://evidence/span/{binding['residual_id']}"
        self.assertEqual(
            blueprint_residual_proofs(document["semantic"]),
            {
                residual_ref: {
                    "kind": "residual_span",
                    "residual_binding": binding,
                }
            },
        )
        residual_evidence = next(
            row
            for row in coverage["plan"]["evidence_manifest"]
            if row["evidence_ref"] == residual_ref
        )
        self.assertEqual(
            residual_evidence["proof"],
            {"kind": "residual_span", "residual_binding": binding},
        )

        workspace = self.timelines.read(project_id)
        clip = workspace["timeline"]["tracks"][0]["clips"][0]
        source = workspace["source_bindings"][0]
        for row in (clip, source):
            self.assertEqual(row["evidence_ref"], residual_ref)
            self.assertEqual(row["residual_id"], binding["residual_id"])
            self.assertEqual(row["parent_segment_id"], parent_segment_id)
            self.assertEqual(row["span_id"], parent_segment_id)
            self.assertEqual(row["residual_binding"], binding)
            self.assertGreaterEqual(row["source_in_ms"], binding["source_in_ms"])
            self.assertLessEqual(row["source_out_ms"], binding["source_out_ms"])
        self.assertEqual(source["asset_source_id"], binding["asset_source_id"])
        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM video_segments WHERE id=?",
                    (binding["residual_id"],),
                ).fetchone()[0],
                0,
            )

    def test_residual_schema_is_closed_and_old_profile_remains_distinct(self) -> None:
        self.assertNotEqual(BLUEPRINT_SCHEMA_SHA256, BLUEPRINT_RESIDUAL_SCHEMA_SHA256)
        _project, _asset, _parent, binding = self._successful_partial_parent_export()
        residual_ref = f"memolens://evidence/span/{binding['residual_id']}"
        proposed = semantic("a3-contract", evidence_ref=residual_ref)
        self.assertIn("residual_proof_missing", {
            row["code"] for row in validate_blueprint_semantic(proposed)
        })
        proposed["material_hints"][0]["proof"] = {
            "kind": "residual_span",
            "residual_binding": binding,
            "path": "/forbidden",
        }
        self.assertIn("unknown_field", {
            row["code"] for row in validate_blueprint_semantic(proposed)
        })

    def test_residual_proof_tamper_and_source_replacement_fail_closed(self) -> None:
        _project, asset_id, _parent, binding = self._successful_partial_parent_export()
        asset_scoped_occurrences = self.repository.canonical_material_search_facts(
            [asset_id]
        )["usage_occurrences"]
        asset_scoped_revision = content_sha256(asset_scoped_occurrences)
        self.assertNotEqual(asset_scoped_revision, binding["usage_revision"])
        scoped_binding = build_residual_binding(
            **{
                field: (
                    asset_scoped_revision
                    if field == "usage_revision"
                    else binding[field]
                )
                for field in (
                    "parent_segment_id",
                    "asset_id",
                    "asset_sha256",
                    "asset_source_id",
                    "source_binding_sha256",
                    "analysis_run_id",
                    "analysis_revision",
                    "input_asset_sha256",
                    "parent_start_ms",
                    "parent_end_ms",
                    "source_in_ms",
                    "source_out_ms",
                    "usage_revision",
                    "parent_usage_projection_sha256",
                )
            }
        )
        scoped_ref = f"memolens://evidence/span/{scoped_binding['residual_id']}"
        with self.repository.transaction() as connection:
            self.assertEqual(
                self.repository.resolve_media_evidence_proofs_in_transaction(
                    connection,
                    [scoped_ref],
                    image_assets_only=True,
                    residual_proofs={
                        scoped_ref: {
                            "kind": "residual_span",
                            "residual_binding": scoped_binding,
                        }
                    },
                ),
                [{"evidence_ref": scoped_ref, "proof": None}],
            )
        forged = deepcopy(binding)
        forged["source_out_ms"] = int(forged["source_out_ms"]) - 1
        residual_ref = f"memolens://evidence/span/{binding['residual_id']}"
        proposed = semantic("a3-tamper", evidence_ref=residual_ref)
        proposed["material_hints"][0]["proof"] = {
            "kind": "residual_span",
            "residual_binding": forged,
        }
        self.assertIn("invalid_residual_proof", {
            row["code"] for row in validate_blueprint_semantic(proposed)
        })

        project_id, _blueprint, _coverage = self._materialize_residual_project(binding)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET observed_mtime_ns=observed_mtime_ns+1 WHERE id=?",
                (binding["asset_source_id"],),
            )
        workspace = self.timelines.read(project_id)
        self.assertEqual(workspace["freshness"]["state"], "stale_source_binding")


if __name__ == "__main__":
    unittest.main()
