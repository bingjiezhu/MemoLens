from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image

from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService, CoverageServiceError
from backend.src.media.image_analysis import ImageAnalysisJobProcessor
from core.coverage_contract import coverage_plan_content_sha256
from core.db import ImageIndexRepository
from core.media_db import (
    CoverageHeadConflictError,
    CoverageIntegrityError,
    CoverageReceiptConflictError,
    MediaRepository,
    canonical_json,
    content_sha256,
    utc_now_iso,
)


IMAGE_RUNTIME_GENERATION = f"runtime_generation_{'c' * 64}"
IMAGE_PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}


def semantic(label: str = "one", *, evidence_ref: str | None = None) -> dict[str, object]:
    material_hints: list[dict[str, object]] = []
    if evidence_ref is not None:
        material_hints.append(
            {
                "hint_id": "opening_image",
                "evidence_ref": evidence_ref,
                "script_block_ids": ["opening"],
                "reason": "Use the selected local image for the opening.",
            }
        )
    return {
        "intent": {
            "goal": f"turn forgotten media into a short film {label}",
            "stance": "ordinary moments deserve attention",
            "audience": "individual creators",
            "platform": "short-video",
        },
        "script": {
            "blocks": [
                {"block_id": "opening", "text": f"These moments were waiting. {label}"},
                {"block_id": "ending", "text": "Now they have a place in the story."},
            ]
        },
        "direction": {
            "theme": "rediscovery",
            "narrative_arc": "forgotten to found",
            "emotion": "warm",
            "tone": "restrained",
            "pace": "measured",
        },
        "output": {"duration_target_ms": 8_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": material_hints,
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


class CoveragePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-coverage-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprint_service = BlueprintService(self.repository)
        self.coverage_service = CoverageService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _create_project(self, label: str = "one") -> tuple[str, dict[str, object]]:
        project = self.repository.create_project(
            f"Coverage {label}",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "coverage-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        return project_id, {
            "revision": 1,
            "content_sha256": str(brief["content_sha256"]),
        }

    def _commit_blueprint(
        self,
        project_id: str,
        legacy_ref: dict[str, object],
        proposed: dict[str, object],
        *,
        key: str,
        expected_head: dict[str, object] | None = None,
    ) -> dict[str, object]:
        self.blueprint_service.commit_proposal(
            project_id,
            {
                "expected_head": expected_head,
                "initial_legacy_brief": legacy_ref if expected_head is None else None,
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key=key,
        )
        head = self.repository.get_blueprint_head(project_id)
        assert head is not None
        return head

    @staticmethod
    def _blueprint_binding(head: dict[str, object]) -> dict[str, object]:
        return {
            "revision": head["revision"],
            "content_sha256": head["content_sha256"],
            "semantic_sha256": head["semantic_sha256"],
        }

    def _materialize(
        self,
        project_id: str,
        blueprint_head: dict[str, object],
        *,
        key: str,
        expected_plan_head: dict[str, object] | None = None,
    ):
        return self.coverage_service.materialize_baseline(
            project_id,
            {
                "expected_blueprint": self._blueprint_binding(blueprint_head),
                "expected_plan_head": expected_plan_head,
            },
            idempotency_key=key,
            expected_database_uuid=self.repository.database_uuid,
        )

    def _register_image(self, name: str = "opening.jpg") -> tuple[dict[str, object], str]:
        path = self.library / name
        Image.new("RGB", (24, 12), "purple").save(path)
        content = path.read_bytes()
        observed = path.stat()
        asset_sha256 = hashlib.sha256(content).hexdigest()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="image",
            sha256=asset_sha256,
            mime_type="image/jpeg",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        source_id = str(asset["asset_source_id"])
        self.repository.update_image_probe(asset_id, width=24, height=12)
        database_stat = os.stat(self.db_path, follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=IMAGE_RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=asset_id,
            source_id=source_id,
            analysis_profile=IMAGE_PROFILE,
            expected_head=None,
            enqueue_scope="coverage-persistence/image-analysis",
            idempotency_key=f"coverage-image-{asset_id}",
        )
        ImageAnalysisJobProcessor(
            self.repository,
            embedding_service=SimpleNamespace(
                backend="semantic_hash",
                semantic_vector_dimensions=8,
            ),
        ).run(
            str(job["id"]),
            runtime_generation=IMAGE_RUNTIME_GENERATION,
            expected_attempt=1,
            cancel_requested=lambda: False,
        )
        current = self.repository.get_current_image_analysis(asset_id)
        assert current is not None and current["projection"]["status"] == "current"
        return asset, source_id

    def test_materialize_replay_and_permanent_receipt_are_exact(self) -> None:
        project_id, legacy = self._create_project()
        blueprint = self._commit_blueprint(
            project_id, legacy, semantic(), key="blueprint-one"
        )

        first = self._materialize(project_id, blueprint, key="coverage-one")
        replay = self._materialize(project_id, blueprint, key="coverage-one")

        self.assertEqual(first.response_status, 201)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(replay.operation_id, first.operation_id)
        head = self.repository.get_coverage_head(project_id)
        assert head is not None
        self.assertEqual(head["revision"], 1)
        self.assertEqual(head["plan"]["blueprint_binding"], self._blueprint_binding(blueprint))
        workspace = self.coverage_service.read(project_id)
        self.assertEqual(workspace["freshness"], {"state": "current", "reasons": []})
        self.assertEqual(workspace["selected_revision"]["is_head"], True)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM coverage_plan_revisions WHERE project_id=?",
                    (project_id,),
                ).fetchone(),
                (1,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM coverage_plan_receipts WHERE project_id=?",
                    (project_id,),
                ).fetchone(),
                (1,),
            )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE coverage_plan_receipts SET response_status=200 WHERE project_id=?",
                    (project_id,),
                )

        changed_request = {
            "expected_blueprint": self._blueprint_binding(blueprint),
            "expected_plan_head": {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            },
        }
        with self.assertRaises(CoverageReceiptConflictError):
            self.coverage_service.materialize_baseline(
                project_id,
                changed_request,
                idempotency_key="coverage-one",
                expected_database_uuid=self.repository.database_uuid,
            )

    def test_project_workspace_reuses_one_blueprint_attestation_for_coverage(
        self,
    ) -> None:
        project_id, legacy = self._create_project("workspace-attestation")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("workspace-attestation"),
            key="blueprint-workspace-attestation",
        )
        self._materialize(
            project_id,
            blueprint,
            key="coverage-workspace-attestation",
        )

        with patch.object(
            MediaRepository,
            "_validate_blueprint_revision_chain",
            wraps=MediaRepository._validate_blueprint_revision_chain,
        ) as revision_audit:
            workspace = self.blueprint_service.project_workspace(project_id).response

        self.assertEqual(revision_audit.call_count, 1)
        self.assertEqual(workspace["project"]["coverage"]["state"], "current")
        self.assertEqual(
            workspace["project"]["coverage"]["head"]["revision"],
            1,
        )

    def test_blueprint_change_marks_old_plan_stale_then_new_revision_rebinds(self) -> None:
        project_id, legacy = self._create_project()
        first_blueprint = self._commit_blueprint(
            project_id, legacy, semantic("one"), key="blueprint-one"
        )
        self._materialize(project_id, first_blueprint, key="coverage-one")
        first_plan = self.repository.get_coverage_head(project_id)
        assert first_plan is not None
        second_blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("two"),
            key="blueprint-two",
            expected_head={
                "revision": first_blueprint["revision"],
                "content_sha256": first_blueprint["content_sha256"],
            },
        )

        stale = self.coverage_service.read(project_id)
        self.assertEqual(stale["freshness"]["state"], "stale_blueprint")
        with self.assertRaises(CoverageServiceError) as raised:
            self._materialize(project_id, first_blueprint, key="stale-blueprint")
        self.assertEqual(raised.exception.code, "blueprint_head_conflict")

        second = self._materialize(
            project_id,
            second_blueprint,
            key="coverage-two",
            expected_plan_head={
                "revision": first_plan["revision"],
                "content_sha256": first_plan["content_sha256"],
            },
        )
        self.assertEqual(second.response["result"]["result_head"]["revision"], 2)
        current = self.coverage_service.read(project_id)
        self.assertEqual(current["freshness"]["state"], "current")
        exact_first = self.coverage_service.read(project_id, revision=1)
        self.assertFalse(exact_first["selected_revision"]["is_head"])
        self.assertEqual(exact_first["freshness"]["state"], "stale_blueprint")

    def test_evidence_availability_change_is_visible_not_silently_selected(self) -> None:
        asset, source_id = self._register_image()
        evidence_ref = f"memolens://evidence/asset/{asset['id']}"
        project_id, legacy = self._create_project("evidence")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("evidence", evidence_ref=evidence_ref),
            key="blueprint-evidence",
        )
        self._materialize(project_id, blueprint, key="coverage-evidence")
        current = self.coverage_service.read(project_id)
        self.assertEqual(current["freshness"]["state"], "current")
        self.assertEqual(
            len(current["coverage_plan"]["beats"][0]["selected_assignments"]),
            1,
        )

        self.repository.mark_source_availability(source_id, "missing")
        stale = self.coverage_service.read(project_id)
        self.assertEqual(stale["freshness"]["state"], "stale_evidence")
        self.assertEqual(stale["freshness"]["reasons"], ["evidence_head_changed"])

    def test_whole_video_asset_is_gap_while_current_exact_span_is_selected(self) -> None:
        content = b"coverage-video"
        asset_sha256 = hashlib.sha256(content).hexdigest()
        name = "opening.mp4"
        path = self.library / name
        path.write_bytes(content)
        observed = path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="video",
            sha256=asset_sha256,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        whole_video_ref = f"memolens://evidence/asset/{asset_id}"

        whole_project, whole_legacy = self._create_project("whole-video")
        whole_blueprint = self._commit_blueprint(
            whole_project,
            whole_legacy,
            semantic("whole-video", evidence_ref=whole_video_ref),
            key="blueprint-whole-video",
        )
        self._materialize(whole_project, whole_blueprint, key="coverage-whole-video")
        whole_plan = self.repository.get_coverage_head(whole_project)
        assert whole_plan is not None
        first_beat = whole_plan["plan"]["beats"][0]
        self.assertEqual(first_beat["selected_assignments"], [])
        self.assertEqual(first_beat["gap"]["code"], "evidence_unresolved")

        now = utc_now_iso()
        analysis_run_id = f"arun_{'a' * 32}"
        span_id = f"seg_{asset_id.removeprefix('asset_')}_1_0"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,analysis_profile_json,
                     input_asset_sha256,status,transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','coverage-test','{}',?,'succeeded',
                          'available','ready',?)""",
                (analysis_run_id, asset_id, asset_sha256, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,boundary_reason,
                     semantic_json,combined_text,visual_status,transcript_status,created_at)
                   VALUES(?,?,?,0,0,10000,'coverage-test','{}','','ready','available',?)""",
                (span_id, asset_id, analysis_run_id, now),
            )
            connection.execute(
                "INSERT INTO asset_analysis_heads(asset_id,analysis_run_id,updated_at) VALUES(?,?,?)",
                (asset_id, analysis_run_id, now),
            )

        span_project, span_legacy = self._create_project("current-span")
        span_blueprint = self._commit_blueprint(
            span_project,
            span_legacy,
            semantic(
                "current-span",
                evidence_ref=f"memolens://evidence/span/{span_id}",
            ),
            key="blueprint-current-span",
        )
        self._materialize(span_project, span_blueprint, key="coverage-current-span")
        span_plan = self.repository.get_coverage_head(span_project)
        assert span_plan is not None
        span_first_beat = span_plan["plan"]["beats"][0]
        self.assertEqual(len(span_first_beat["selected_assignments"]), 1)
        self.assertIsNone(span_first_beat["gap"])

    def test_failure_after_revision_insert_rolls_back_operation_head_and_receipt(self) -> None:
        project_id, legacy = self._create_project()
        blueprint = self._commit_blueprint(
            project_id, legacy, semantic(), key="blueprint-fault"
        )
        original = self.repository.append_coverage_revision

        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected_after_revision")

        with patch.object(
            self.repository, "append_coverage_revision", side_effect=fail_after
        ), self.assertRaisesRegex(RuntimeError, "injected_after_revision"):
            self._materialize(project_id, blueprint, key="coverage-fault")

        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in (
                "coverage_plan_operations",
                "coverage_plan_revisions",
                "coverage_plan_heads",
                "coverage_plan_receipts",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone(),
                    (0,),
                )

    def test_verified_transaction_rejects_callback_ddl_commit_and_temp_shadow(self) -> None:
        project_id, legacy = self._create_project("verified-boundary")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("verified-boundary"),
            key="blueprint-verified-boundary",
        )

        def ddl_attack(connection, **_kwargs):
            connection.execute("CREATE TABLE coverage_unexpected_side_effect(id TEXT)")
            raise AssertionError("DDL authorizer did not reject the callback")

        with patch.object(
            self.coverage_service,
            "_materialize_mutation",
            side_effect=ddl_attack,
        ), self.assertRaises(CoverageIntegrityError):
            self._materialize(project_id, blueprint, key="coverage-ddl-attack")

        def commit_attack(connection, **_kwargs):
            connection.commit()
            raise AssertionError("transaction-control guard did not reject commit")

        with patch.object(
            self.coverage_service,
            "_materialize_mutation",
            side_effect=commit_attack,
        ), self.assertRaises(CoverageIntegrityError):
            self._materialize(project_id, blueprint, key="coverage-commit-attack")

        anchor, _identity = self.repository._blueprint_anchor_connection()
        anchor.execute("CREATE TEMP TABLE assets(id TEXT PRIMARY KEY)")
        with self.assertRaises(CoverageIntegrityError):
            self._materialize(project_id, blueprint, key="coverage-temp-attack")

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name='coverage_unexpected_side_effect'"
                ).fetchone()
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "coverage_plan_operations",
                        "coverage_plan_revisions",
                        "coverage_plan_heads",
                        "coverage_plan_receipts",
                    )
                ),
                (0, 0, 0, 0),
            )
        finally:
            connection.close()

    def test_same_uuid_database_path_replacement_is_rejected(self) -> None:
        project_id, legacy = self._create_project("path-replacement")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("path-replacement"),
            key="blueprint-path-replacement",
        )
        clone_path = self.root / "same-uuid-clone.db"
        source = sqlite3.connect(self.db_path)
        clone = sqlite3.connect(clone_path)
        try:
            source.backup(clone)
        finally:
            clone.close()
            source.close()
        os.replace(clone_path, self.db_path)

        with self.assertRaises(CoverageIntegrityError):
            self._materialize(project_id, blueprint, key="coverage-path-replacement")

    def test_persisted_plan_tamper_fails_closed_and_poisons_repository(self) -> None:
        project_id, legacy = self._create_project()
        blueprint = self._commit_blueprint(
            project_id, legacy, semantic(), key="blueprint-tamper"
        )
        self._materialize(project_id, blueprint, key="coverage-tamper")

        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_coverage_revisions_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_coverage_revisions_no_update")
            connection.execute(
                "UPDATE coverage_plan_revisions SET plan_json='{}' WHERE project_id=?",
                (project_id,),
            )
            connection.execute(trigger)
            connection.commit()
        with self.assertRaises(CoverageIntegrityError):
            self.repository.get_coverage_head(project_id)

    def test_persisted_receipt_tamper_fails_closed(self) -> None:
        receipt_project, receipt_legacy = self._create_project("receipt")
        receipt_blueprint = self._commit_blueprint(
            receipt_project,
            receipt_legacy,
            semantic("receipt"),
            key="blueprint-receipt",
        )
        self._materialize(receipt_project, receipt_blueprint, key="coverage-receipt")
        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_coverage_receipts_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_coverage_receipts_no_update")
            connection.execute(
                "UPDATE coverage_plan_receipts SET response_json='{}' WHERE project_id=?",
                (receipt_project,),
            )
            connection.execute(trigger)
            connection.commit()
        with self.assertRaises(CoverageIntegrityError):
            self._materialize(
                receipt_project, receipt_blueprint, key="coverage-receipt"
            )

    def test_receipt_digest_binds_idempotency_key_for_cold_validation(self) -> None:
        project_id, legacy = self._create_project("receipt-key")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("receipt-key"),
            key="blueprint-receipt-key",
        )
        self._materialize(project_id, blueprint, key="coverage-original-key")

        connection = sqlite3.connect(self.db_path)
        try:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_coverage_receipts_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_coverage_receipts_no_update")
            connection.execute(
                "UPDATE coverage_plan_receipts SET idempotency_key=? WHERE project_id=?",
                ("coverage-forged-key", project_id),
            )
            connection.execute(trigger)
            connection.commit()
        finally:
            connection.close()

        cold_repository = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(CoverageIntegrityError, "coverage_receipt_invalid"):
                cold_repository.get_coverage_head(project_id)
        finally:
            cold_repository.close()

    def test_cold_validation_rejects_coherent_but_non_derived_plan_forgery(self) -> None:
        asset, _source_id = self._register_image("derived.jpg")
        evidence_ref = f"memolens://evidence/asset/{asset['id']}"
        project_id, legacy = self._create_project("exact-derivation")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("exact-derivation", evidence_ref=evidence_ref),
            key="blueprint-exact-derivation",
        )
        self._materialize(project_id, blueprint, key="coverage-exact-derivation")

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            revision_row = connection.execute(
                "SELECT * FROM coverage_plan_revisions WHERE project_id=?",
                (project_id,),
            ).fetchone()
            operation_row = connection.execute(
                "SELECT * FROM coverage_plan_operations WHERE project_id=?",
                (project_id,),
            ).fetchone()
            receipt_row = connection.execute(
                "SELECT * FROM coverage_plan_receipts WHERE project_id=?",
                (project_id,),
            ).fetchone()
            assert revision_row is not None
            assert operation_row is not None
            assert receipt_row is not None

            forged_plan = json.loads(revision_row["plan_json"])
            assignment = forged_plan["beats"][0]["selected_assignments"][0]
            assignment["reason_sha256"] = "f" * 64
            forged_content_sha256 = coverage_plan_content_sha256(forged_plan)

            forged_result = json.loads(operation_row["result_json"])
            forged_result["result_head"]["content_sha256"] = forged_content_sha256
            forged_response = json.loads(receipt_row["response_json"])
            forged_response["result"] = forged_result
            serialized_response = canonical_json(forged_response)
            response_sha256 = hashlib.sha256(serialized_response.encode()).hexdigest()
            receipt_material = MediaRepository._coverage_receipt_material(
                database_uuid=receipt_row["database_uuid"],
                authenticated_principal=receipt_row["authenticated_principal"],
                project_id=receipt_row["project_id"],
                command_type=receipt_row["command_type"],
                command_version=receipt_row["command_version"],
                idempotency_key=receipt_row["idempotency_key"],
                request_sha256=receipt_row["request_sha256"],
                operation_id=receipt_row["operation_id"],
                response_status=receipt_row["response_status"],
                response_sha256=response_sha256,
                resource_id=receipt_row["resource_id"],
                created_at=receipt_row["created_at"],
            )

            trigger_names = (
                "trg_coverage_operations_no_update",
                "trg_coverage_revisions_no_update",
                "trg_coverage_heads_forward_only",
                "trg_coverage_receipts_no_update",
            )
            triggers = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                for name in trigger_names
            }
            connection.execute("PRAGMA foreign_keys=OFF")
            for name in trigger_names:
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute(
                """UPDATE coverage_plan_revisions
                      SET plan_json=?,content_sha256=? WHERE project_id=?""",
                (canonical_json(forged_plan), forged_content_sha256, project_id),
            )
            connection.execute(
                """UPDATE coverage_plan_operations
                      SET result_content_sha256=?,result_json=? WHERE project_id=?""",
                (forged_content_sha256, canonical_json(forged_result), project_id),
            )
            connection.execute(
                "UPDATE coverage_plan_heads SET content_sha256=? WHERE project_id=?",
                (forged_content_sha256, project_id),
            )
            connection.execute(
                """UPDATE coverage_plan_receipts
                      SET response_json=?,response_sha256=?,receipt_sha256=?
                    WHERE project_id=?""",
                (
                    serialized_response,
                    response_sha256,
                    content_sha256(receipt_material),
                    project_id,
                ),
            )
            for sql in triggers.values():
                connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

        cold_repository = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CoverageIntegrityError,
                "coverage_baseline_derivation_invalid",
            ):
                cold_repository.get_coverage_head(project_id)
        finally:
            cold_repository.close()


    def test_historical_revision_tamper_blocks_current_head(self) -> None:
        project_id, legacy = self._create_project("historical-tamper")
        first_blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("historical-one"),
            key="blueprint-historical-one",
        )
        self._materialize(project_id, first_blueprint, key="coverage-historical-one")
        first_plan = self.repository.get_coverage_head(project_id)
        assert first_plan is not None
        second_blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("historical-two"),
            key="blueprint-historical-two",
            expected_head={
                "revision": first_blueprint["revision"],
                "content_sha256": first_blueprint["content_sha256"],
            },
        )
        self._materialize(
            project_id,
            second_blueprint,
            key="coverage-historical-two",
            expected_plan_head={
                "revision": first_plan["revision"],
                "content_sha256": first_plan["content_sha256"],
            },
        )

        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_coverage_revisions_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_coverage_revisions_no_update")
            connection.execute(
                """UPDATE coverage_plan_revisions SET plan_json='{}'
                    WHERE project_id=? AND revision=1""",
                (project_id,),
            )
            connection.execute(trigger)
            connection.commit()

        with self.assertRaises(CoverageIntegrityError):
            self.repository.get_coverage_head(project_id)
        with self.assertRaises(CoverageIntegrityError):
            self.coverage_service.read(project_id)

    def test_trusted_prefix_rejects_coherent_history_rollback(self) -> None:
        project_id, legacy = self._create_project("trusted-rollback")
        first_blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("trusted-rollback-one"),
            key="blueprint-trusted-rollback-one",
        )
        self._materialize(
            project_id,
            first_blueprint,
            key="coverage-trusted-rollback-one",
        )
        first = self.repository.get_coverage_head(project_id)
        assert first is not None
        second_blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("trusted-rollback-two"),
            key="blueprint-trusted-rollback-two",
            expected_head={
                "revision": first_blueprint["revision"],
                "content_sha256": first_blueprint["content_sha256"],
            },
        )
        self._materialize(
            project_id,
            second_blueprint,
            key="coverage-trusted-rollback-two",
            expected_plan_head={
                "revision": first["revision"],
                "content_sha256": first["content_sha256"],
            },
        )

        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            trigger_names = (
                "trg_coverage_operations_no_delete",
                "trg_coverage_revisions_no_delete",
                "trg_coverage_receipts_no_delete",
                "trg_coverage_heads_forward_only",
            )
            triggers = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (name,)
                ).fetchone()[0]
                for name in trigger_names
            }
            for name in trigger_names:
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute(
                "DELETE FROM coverage_plan_receipts WHERE project_id=? AND operation_id<>?",
                (project_id, first["operation_id"]),
            )
            connection.execute(
                """UPDATE coverage_plan_heads
                      SET revision=?,content_sha256=?,blueprint_revision=?,
                          blueprint_content_sha256=?,blueprint_semantic_sha256=?,
                          operation_id=?,updated_at=? WHERE project_id=?""",
                (
                    first["revision"],
                    first["content_sha256"],
                    first["blueprint_revision"],
                    first["blueprint_content_sha256"],
                    first["blueprint_semantic_sha256"],
                    first["operation_id"],
                    first["updated_at"],
                    project_id,
                ),
            )
            connection.execute(
                "DELETE FROM coverage_plan_revisions WHERE project_id=? AND revision>1",
                (project_id,),
            )
            connection.execute(
                "DELETE FROM coverage_plan_operations WHERE project_id=? AND sequence>1",
                (project_id,),
            )
            for sql in triggers.values():
                connection.execute(sql)
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(
            CoverageIntegrityError,
            "coverage_trusted_prefix_regressed",
        ):
            self.repository.get_coverage_head(project_id)

    def test_concurrent_first_head_has_one_winner_and_no_partial_rows(self) -> None:
        project_id, legacy = self._create_project()
        blueprint = self._commit_blueprint(
            project_id, legacy, semantic(), key="blueprint-race"
        )

        def contender(index: int) -> str:
            try:
                result = self._materialize(
                    project_id, blueprint, key=f"coverage-race-{index}"
                )
                return "won" if not result.replayed else "replayed"
            except CoverageHeadConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(contender, range(24)))
        self.assertEqual(outcomes.count("won"), 1)
        self.assertEqual(outcomes.count("conflict"), 23)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "coverage_plan_operations",
                        "coverage_plan_revisions",
                        "coverage_plan_heads",
                        "coverage_plan_receipts",
                    )
                ),
                (1, 1, 1, 1),
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_cas_has_exactly_one_winner_across_100_rounds(self) -> None:
        project_id, legacy = self._create_project("cas-100")
        blueprint = self._commit_blueprint(
            project_id,
            legacy,
            semantic("cas-100"),
            key="blueprint-cas-100",
        )
        winning_request: tuple[str, dict[str, object] | None] | None = None

        def contender(
            key: str,
            expected_head: dict[str, object] | None,
        ) -> tuple[str, str]:
            try:
                result = self._materialize(
                    project_id,
                    blueprint,
                    key=key,
                    expected_plan_head=expected_head,
                )
                return key, "won" if not result.replayed else "replayed"
            except CoverageHeadConflictError:
                return key, "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            for round_index in range(100):
                current = self.repository.get_coverage_head(project_id)
                expected_head = (
                    None
                    if current is None
                    else {
                        "revision": current["revision"],
                        "content_sha256": current["content_sha256"],
                    }
                )
                keys = (
                    f"coverage-cas-{round_index}-left",
                    f"coverage-cas-{round_index}-right",
                )
                outcomes = list(
                    pool.map(
                        lambda key: contender(key, expected_head),
                        keys,
                    )
                )
                self.assertEqual(
                    sorted(status for _key, status in outcomes),
                    ["conflict", "won"],
                )
                winner = next(key for key, status in outcomes if status == "won")
                winning_request = (winner, expected_head)

        final_head = self.repository.get_coverage_head(project_id)
        assert final_head is not None
        self.assertEqual(final_head["revision"], 100)
        assert winning_request is not None
        replay = self._materialize(
            project_id,
            blueprint,
            key=winning_request[0],
            expected_plan_head=winning_request[1],
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response["result"]["result_head"]["revision"], 100)

        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "coverage_plan_operations",
                        "coverage_plan_revisions",
                        "coverage_plan_receipts",
                    )
                ),
                (100, 100, 100),
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        finally:
            connection.close()


class CoverageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-coverage-api-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        project = self.repository.create_project(
            "Coverage API",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "coverage-api-test"},
        )
        self.project_id = str(project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        blueprint_service = BlueprintService(self.repository)
        blueprint_service.commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": semantic("api"),
            },
            idempotency_key="api-blueprint",
        )
        self.blueprint = self.repository.get_blueprint_head(self.project_id)
        assert self.blueprint is not None
        app = Flask("coverage-api-test")
        app.config.update(
            TESTING=True,
            DESKTOP_SESSION_TOKEN="coverage-test-token",
        )
        app.extensions["media_repository"] = self.repository
        app.extensions["blueprint_service"] = blueprint_service
        app.extensions["coverage_service"] = CoverageService(self.repository)
        app.register_blueprint(api_blueprint)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _payload(self, expected_plan_head=None) -> dict[str, object]:
        return {
            "expected_blueprint": {
                "revision": self.blueprint["revision"],
                "content_sha256": self.blueprint["content_sha256"],
                "semantic_sha256": self.blueprint["semantic_sha256"],
            },
            "expected_plan_head": expected_plan_head,
        }

    def _post(self, payload: object, *, key: str = "api-coverage", token: bool = True):
        headers = {"Idempotency-Key": key}
        if token:
            headers["X-MemoLens-Desktop-Token"] = "coverage-test-token"
        return self.client.post(
            f"/v1/creative/projects/{self.project_id}/coverage/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": self.repository.database_uuid,
            },
            json=payload,
            headers=headers,
        )

    def test_post_requires_desktop_database_binding_and_strict_json(self) -> None:
        denied = self._post(self._payload(), token=False)
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.get_json()["code"], "desktop_auth_required")

        missing_binding = self.client.post(
            f"/v1/creative/projects/{self.project_id}/coverage/materialize",
            json=self._payload(),
            headers={
                "X-MemoLens-Desktop-Token": "coverage-test-token",
                "Idempotency-Key": "missing-db",
            },
        )
        self.assertEqual(missing_binding.status_code, 409)
        self.assertEqual(missing_binding.get_json()["code"], "database_binding_required")

        unsupported = self._payload()
        unsupported["raw_sql"] = "SELECT *"
        invalid = self._post(unsupported, key="unsupported")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "invalid_coverage_command")

        scalar = self.client.post(
            f"/v1/creative/projects/{self.project_id}/coverage/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": self.repository.database_uuid,
            },
            data="[]",
            content_type="application/json",
            headers={
                "X-MemoLens-Desktop-Token": "coverage-test-token",
                "Idempotency-Key": "scalar",
            },
        )
        self.assertEqual(scalar.status_code, 400)

    def test_post_replay_and_public_read_surface(self) -> None:
        first = self._post(self._payload())
        replay = self._post(self._payload())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.headers["Idempotency-Replayed"], "false")
        self.assertEqual(replay.status_code, 201)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.get_json(), first.get_json())

        read = self.client.get(
            f"/v1/creative/projects/{self.project_id}/coverage"
        )
        self.assertEqual(read.status_code, 200)
        workspace = read.get_json()
        self.assertEqual(workspace["freshness"]["state"], "current")
        self.assertEqual(workspace["coverage_plan"]["revision"], 1)

        project = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string={"db_path": str(self.db_path)},
        )
        self.assertEqual(project.status_code, 200)
        project_workspace = project.get_json()["project"]
        self.assertEqual(project_workspace["coverage"]["state"], "current")
        self.assertEqual(project_workspace["coverage"]["beat_count"], 2)
        self.assertEqual(project_workspace["coverage"]["gap_count"], 2)
        self.assertEqual(
            project_workspace["executable"]["reason_code"],
            "coverage_not_lowerable",
        )
        self.assertTrue(
            project_workspace["capabilities"]["coverage_plan_materialization"]
        )

        exact = self.client.get(
            f"/v1/creative/projects/{self.project_id}/coverage",
            query_string={"revision": "1"},
        )
        self.assertEqual(exact.status_code, 200)
        missing = self.client.get(
            f"/v1/creative/projects/{self.project_id}/coverage",
            query_string={"revision": "2"},
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["code"], "coverage_not_found")

    def test_database_uuid_and_head_conflicts_are_explicit(self) -> None:
        wrong_database = self.client.post(
            f"/v1/creative/projects/{self.project_id}/coverage/materialize",
            query_string={
                "db_path": str(self.db_path),
                "expected_database_uuid": "00000000-0000-4000-8000-000000000000",
            },
            json=self._payload(),
            headers={
                "X-MemoLens-Desktop-Token": "coverage-test-token",
                "Idempotency-Key": "wrong-database",
            },
        )
        self.assertEqual(wrong_database.status_code, 409)
        self.assertEqual(wrong_database.get_json()["code"], "database_identity_changed")

        self.assertEqual(self._post(self._payload(), key="winner").status_code, 201)
        stale = self._post(self._payload(), key="stale-head")
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["code"], "coverage_head_conflict")
        self.assertEqual(stale.get_json()["details"]["current"]["revision"], 1)

    def test_blueprint_corruption_maps_to_stable_coverage_integrity_error(self) -> None:
        connection = sqlite3.connect(self.db_path)
        try:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_blueprint_revisions_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_blueprint_revisions_no_update")
            connection.execute(
                "UPDATE creative_blueprint_revisions SET blueprint_json='{}' WHERE project_id=?",
                (self.project_id,),
            )
            connection.execute(trigger)
            connection.commit()
        finally:
            connection.close()

        response = self.client.get(
            f"/v1/creative/projects/{self.project_id}/coverage"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "coverage_integrity_error")

    def test_project_workspace_maps_coverage_corruption_to_coverage_error(self) -> None:
        self.assertEqual(self._post(self._payload()).status_code, 201)
        connection = sqlite3.connect(self.db_path)
        try:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='trg_coverage_revisions_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_coverage_revisions_no_update")
            connection.execute(
                "UPDATE coverage_plan_revisions SET plan_json='{}' WHERE project_id=?",
                (self.project_id,),
            )
            connection.execute(trigger)
            connection.commit()
        finally:
            connection.close()

        response = self.client.get(
            f"/v1/creative/projects/{self.project_id}",
            query_string={"db_path": str(self.db_path)},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "coverage_integrity_error")


if __name__ == "__main__":
    unittest.main()
