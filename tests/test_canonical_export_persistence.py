from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

from backend.src.media.blueprint import BlueprintService
from backend.src.media.canonical_export_artifacts import usage_text_from_manifest
from backend.src.media.coverage import CoverageService
from backend.src.media.retrieval import MixedRetrievalService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.media_db import (
    CanonicalExportIntegrityError,
    CanonicalExportJobConflictError,
    CanonicalExportNativeNonceConflictError,
    CanonicalExportReceiptConflictError,
    MediaMigrationError,
    MediaRepository,
    SCHEMA_VERSION,
    V6_CHECKSUM,
    V7_CHECKSUM,
    V7_SCHEMA_STATEMENTS,
    V8_CHECKSUM,
    _V8_SCHEMA_OBJECTS,
    canonical_json,
    content_sha256,
)
from tests import test_timeline_lowering_integration as timeline_integration
from tests import test_timeline_persistence as timeline_persistence


def initialize_v7(root: Path) -> tuple[MediaRepository, Path, Path]:
    repository, library, db_path = timeline_persistence.initialize_v6(root)
    connection = repository._connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in V7_SCHEMA_STATEMENTS:
            connection.execute(statement)
        repository._migration(
            connection,
            7,
            "canonical_timeline_first_cut",
            V7_CHECKSUM,
        )
        connection.execute(
            "UPDATE database_meta SET schema_version=7,updated_at=? WHERE singleton=1",
            (timeline_persistence.utc_now_iso(),),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return repository, library, db_path


class CanonicalExportPersistenceTests(unittest.TestCase):
    _register_image = timeline_integration.TimelineLoweringIntegrationTests._register_image
    _register_video_span = timeline_integration.TimelineLoweringIntegrationTests._register_video_span
    _build_project = timeline_integration.TimelineLoweringIntegrationTests._build_project
    _materialize = timeline_integration.TimelineLoweringIntegrationTests._materialize

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-export-v8-")
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

    def test_renderer_source_binding_digest_known_vector_matches_core(self) -> None:
        vector = [
            {
                "media_kind": "image",
                "asset_source_id": "source_1",
                "asset_sha256": "a" * 64,
                "asset_id": "asset_1",
                "evidence_ref": "memolens://evidence/asset/asset_1",
                "clip_id": "clip_1",
                "coverage_proof_sha256": "b" * 64,
            },
            {
                "source_out_ms": 9000,
                "analysis_revision": 7,
                "clip_id": "clip_2",
                "asset_id": "asset_2",
                "asset_sha256": "c" * 64,
                "asset_source_id": "source_2",
                "coverage_proof_sha256": "d" * 64,
                "evidence_ref": "memolens://evidence/span/seg_2",
                "media_kind": "video",
                "span_id": "seg_2",
                "analysis_run_id": "run_2",
                "input_asset_sha256": "e" * 64,
                "source_in_ms": 1000,
            },
        ]
        self.assertEqual(
            content_sha256(vector),
            "d5b188d3115d68dc2d4e3489045e501a46cedaeab62065cb3147dcf495721959",
        )

    def test_command_admission_rejects_captured_database_swap(self) -> None:
        project_id, presented, envelope = self._prepared_request()
        swapped_presentation = deepcopy(presented["presentation"])
        swapped_presentation["database_uuid"] = "database-swapped"
        swapped = self.repository.build_canonical_export_envelope(
            presentation=swapped_presentation,
            presentation_sha256=content_sha256(swapped_presentation),
            main_native_user_gesture=True,
            runtime_epoch=str(envelope["runtime_epoch"]),
            native_nonce="nonce-database-swap",
            output_root_id=str(envelope["output_root_id"]),
            permission_fingerprint=str(envelope["permission_fingerprint"]),
            package_basename=str(envelope["package_basename"]),
        )
        with self.assertRaises(CanonicalExportJobConflictError) as raised:
            self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="database-swap",
                envelope=swapped,
            )
        self.assertEqual(str(raised.exception), "canonical_export_database_identity_stale")
        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_export_operations WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )

    def _prepared_request(self, *, nonce: str = "nonce-one"):
        project_id, _blueprint, _coverage, timeline_payload = self._build_project()
        self._materialize(project_id, timeline_payload, key="timeline-export")
        presented = self.repository.build_canonical_export_presentation(project_id)
        root = self.repository.register_user_export_root(self.output)
        envelope = self.repository.build_canonical_export_envelope(
            presentation=presented["presentation"],
            presentation_sha256=presented["presentation_sha256"],
            main_native_user_gesture=True,
            runtime_epoch="runtime-epoch-one",
            native_nonce=nonce,
            output_root_id=root["id"],
            permission_fingerprint=root["permission_fingerprint"],
            package_basename="memory-film",
        )
        return project_id, presented, envelope

    def _present_existing_project(
        self,
        project_id: str,
        *,
        key: str,
        nonce: str,
        package_basename: str,
    ):
        presented = self.repository.build_canonical_export_presentation(project_id)
        root = self.repository.register_user_export_root(self.output)
        envelope = self.repository.build_canonical_export_envelope(
            presentation=presented["presentation"],
            presentation_sha256=presented["presentation_sha256"],
            main_native_user_gesture=True,
            runtime_epoch=f"runtime-{key}",
            native_nonce=nonce,
            output_root_id=root["id"],
            permission_fingerprint=root["permission_fingerprint"],
            package_basename=package_basename,
        )
        command = self.repository.execute_canonical_export_command(
            authenticated_principal="main_native_user_gesture",
            project_id=project_id,
            idempotency_key=key,
            envelope=envelope,
        )
        return presented, command

    def _materialize_semantic_project(
        self,
        proposed: dict[str, object],
        *,
        key: str,
    ) -> tuple[str, dict[str, object]]:
        project = self.repository.create_project(
            f"Export {key}",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "canonical-export-test"},
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
            idempotency_key=f"blueprint-{key}",
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
            idempotency_key=f"coverage-{key}",
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
            key=f"timeline-{key}",
        )
        return project_id, blueprint

    def _advance_blueprint(self, project_id: str) -> dict[str, object]:
        current = self.repository.get_blueprint_head(project_id)
        assert current is not None
        semantic = deepcopy(current["blueprint"]["semantic"])
        semantic["script"]["blocks"][0]["text"] += " revised"
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": current["revision"],
                    "content_sha256": current["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic,
            },
            idempotency_key=f"blueprint-advance-{project_id}",
        )
        advanced = self.repository.get_blueprint_head(project_id)
        assert advanced is not None
        return advanced

    def _prepared_export(self, *, key: str = "export-one", nonce: str = "nonce-one"):
        project_id, presented, envelope = self._prepared_request(nonce=nonce)
        command = self.repository.execute_canonical_export_command(
            authenticated_principal="main_native_user_gesture",
            project_id=project_id,
            idempotency_key=key,
            envelope=envelope,
        )
        return project_id, presented, envelope, command

    def _advance_commit_pending(self, job_id: str) -> None:
        for status in ("rendering", "packaging"):
            self.repository.update_canonical_export_job(
                job_id=job_id,
                status=status,
                stage=status,
                progress={"rendering": 0.2, "packaging": 0.8}[status],
            )

    def test_interrupted_job_blocks_workspace_until_activation_recovery(self) -> None:
        project_id, _presented, _envelope, command = self._prepared_export(
            key="workspace-recovery-required",
            nonce="nonce-workspace-recovery",
        )
        self.repository.update_canonical_export_job(
            job_id=command.resource_id,
            status="interrupted",
            stage="interrupted",
            progress=0.0,
            error={
                "code": "canonical_export_published_outcome_unknown",
                "message": "Canonical export did not complete.",
                "retryable": True,
            },
            expected_status="requested",
        )

        workspace = self.blueprints.project_workspace(project_id).response["project"]
        self.assertEqual(
            workspace["canonical_export"],
            {
                "state": "blocked",
                "reason_code": "export_recovery_required",
                "latest_job": {
                    "job_id": command.resource_id,
                    "status": "interrupted",
                    "package_basename": "memory-film",
                },
            },
        )
        self.assertFalse(workspace["capabilities"]["canonical_export"])

        self._advance_blueprint(project_id)
        stale_workspace = self.blueprints.project_workspace(project_id).response[
            "project"
        ]
        self.assertEqual(
            stale_workspace["canonical_export"]["reason_code"],
            "export_recovery_required",
        )
        self.assertFalse(stale_workspace["capabilities"]["canonical_export"])

    def _completion_inputs(
        self,
        project_id: str,
        job_id: str,
        presented: dict[str, object],
        *,
        stage: bool = True,
        video_sha256: str = "a" * 64,
    ):
        binding = presented["presentation"]["timeline_binding"]
        occurrences = self.repository.build_canonical_usage_occurrences(
            project_id=project_id,
            timeline_revision=binding["revision"],
            timeline_revision_sha256=binding["revision_sha256"],
        )
        timeline_revision = self.repository.get_canonical_timeline_revision(
            project_id,
            binding["revision"],
        )
        assert timeline_revision is not None
        source_bindings = timeline_revision["source_bindings"]
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            snapshots = []
            for ordinal, source_binding in enumerate(source_bindings):
                file_size = connection.execute(
                    "SELECT file_size FROM assets WHERE id=? AND sha256=?",
                    (source_binding["asset_id"], source_binding["asset_sha256"]),
                ).fetchone()[0]
                snapshots.append(
                    {
                        "ordinal": ordinal,
                        "clip_id": source_binding["clip_id"],
                        "asset_source_id": source_binding["asset_source_id"],
                        "asset_id": source_binding["asset_id"],
                        "asset_sha256": source_binding["asset_sha256"],
                        "size_bytes": file_size,
                        "media_kind": source_binding["media_kind"],
                        "source_label": f"source-{ordinal}.bin",
                        "source_label_role": "locator_hint_only",
                    }
                )
        actual_read = {
            "object": "memolens.canonical_actual_read_manifest",
            "schema_version": "1",
            "timeline_content_sha256": binding["timeline_content_sha256"],
            "source_bindings": source_bindings,
            "source_bindings_sha256": binding["source_bindings_sha256"],
            "snapshots": snapshots,
        }
        dimensions = {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
        }[presented["presentation"]["aspect_ratio"]]
        runtime = {
            "object": "memolens.canonical_export_runtime_manifest",
            "schema_version": "1",
            "renderer": "memolens.canonical-export-artifacts/v1",
            "profile": "export-1080p",
            "timeline_content_sha256": binding["timeline_content_sha256"],
            "timeline_source_bindings_sha256": binding["source_bindings_sha256"],
            "width": dimensions[0],
            "height": dimensions[1],
            "fps": 30,
            "audio": "silent",
            "fit": "cover",
            "transitions": "hard_cut",
            "actual_read_manifest": actual_read,
            "actual_read_manifest_sha256": content_sha256(actual_read),
            "runtime_identity": {
                "executor": "memolens-test/v1",
                "evidence_ref": "memolens://evidence/asset/example",
            },
        }
        script = self.repository.build_canonical_export_script_projection(
            project_id=project_id,
            blueprint_binding=binding["blueprint_binding"],
        )
        video = {
            "video_sha256": video_sha256,
            "video_size_bytes": 1234,
            "video_duration_ms": presented["presentation"]["duration_ms"],
            "script_sha256": script["sha256"],
            "script_size_bytes": script["size_bytes"],
        }
        manifest = self.repository.build_canonical_export_package_manifest(
            job_id=job_id,
            occurrences=occurrences,
            runtime_manifest=runtime,
            **video,
        )
        package_manifest_sha256 = content_sha256(manifest)
        human_usage_sha256 = hashlib.sha256(
            usage_text_from_manifest(manifest).encode("utf-8")
        ).hexdigest()
        marker = {
            "object": "memolens.lightweight_export_package_completion",
            "schema_version": "1",
            "completion_state": "complete",
            "package_basename": manifest["package_basename"],
            "package_manifest_sha256": package_manifest_sha256,
            "required_file_sha256": {
                "video.mp4": video["video_sha256"],
                "script.txt": video["script_sha256"],
                "manifest.json": package_manifest_sha256,
                "使用清单.txt": human_usage_sha256,
            },
        }
        proof = {
            **video,
            "package_manifest": manifest,
            "package_manifest_sha256": package_manifest_sha256,
            "human_usage_sha256": human_usage_sha256,
            "completion_marker_sha256": content_sha256(marker),
        }
        if stage:
            attestation = self.repository.stage_canonical_export_commit(
                job_id=job_id,
                occurrences=occurrences,
                runtime_manifest=runtime,
                **video,
            )
            self.assertEqual(attestation["artifact_proof"], proof)
            proof = attestation["artifact_proof"]
        return proof, occurrences, runtime

    def _rewritten_revision_material(
        self,
        revision: dict[str, object],
        *,
        occurrences: list[dict[str, object]] | None = None,
        script: dict[str, object] | None = None,
        runtime_manifest: dict[str, object] | None = None,
    ) -> dict[str, object]:
        occurrence_values = deepcopy(
            revision["usage_occurrences"] if occurrences is None else occurrences
        )
        script_value = deepcopy(revision["script"] if script is None else script)
        runtime_value = deepcopy(
            revision["runtime_manifest"]
            if runtime_manifest is None
            else runtime_manifest
        )
        manifest = deepcopy(revision["package_manifest"])
        manifest["usage_occurrences"] = occurrence_values
        manifest["usage_sha256"] = content_sha256(occurrence_values)
        manifest["script"] = script_value
        manifest["runtime_manifest"] = runtime_value
        manifest["runtime_manifest_sha256"] = content_sha256(runtime_value)
        physical = self.repository._canonical_export_physical_artifact_bindings(
            manifest
        )
        material = self.repository._canonical_export_revision_material(
            project_id=revision["project_id"],
            revision=revision["revision"],
            job_id=revision["job_id"],
            operation_id=revision["operation_id"],
            timeline_binding=revision["timeline_binding"],
            output_binding=revision["output_binding"],
            video=revision["video"],
            script=script_value,
            package_manifest_sha256=physical["package_manifest_sha256"],
            usage_sha256=manifest["usage_sha256"],
            runtime_manifest_sha256=manifest["runtime_manifest_sha256"],
            human_usage_sha256=physical["human_usage_sha256"],
            completion_marker_sha256=physical["completion_marker_sha256"],
            completed_at=revision["completed_at"],
        )
        return {
            "occurrences": occurrence_values,
            "script": script_value,
            "runtime_manifest": runtime_value,
            "package_manifest": manifest,
            "package_manifest_sha256": physical["package_manifest_sha256"],
            "usage_sha256": manifest["usage_sha256"],
            "runtime_manifest_sha256": manifest["runtime_manifest_sha256"],
            "human_usage_sha256": physical["human_usage_sha256"],
            "completion_marker_sha256": physical["completion_marker_sha256"],
            "revision_sha256": content_sha256(material),
        }

    def _tamper_immutable_rows(
        self,
        statements: list[tuple[str, tuple[object, ...]]],
        *,
        trigger_names: tuple[str, ...],
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            trigger_sql = []
            for name in trigger_names:
                row = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()
                assert row is not None and isinstance(row[0], str)
                trigger_sql.append(row[0])
                connection.execute(f'DROP TRIGGER "{name}"')
            for statement, parameters in statements:
                connection.execute(statement, parameters)
            for statement in trigger_sql:
                connection.execute(statement)

    def test_current_schema_keeps_frozen_v6_through_v8_contracts(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V6_CHECKSUM,
            "cddb91190311db0f5593db6af5788d54f51c2d82385cad4ea6067da51461716e",
        )
        self.assertEqual(
            V7_CHECKSUM,
            "687918fbb4692d872b025467321d7c2509c64836b8bad7717eef46960783edf2",
        )
        self.assertEqual(
            V8_CHECKSUM,
            "6993c2ab8b74b59b39e275462ea7377f5fb35fa1a646b01c0c6ced3c5c89e33d",
        )
        self.assertEqual(len(_V8_SCHEMA_OBJECTS), 25)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT version,name,checksum FROM schema_migrations WHERE version=8"
                ).fetchone(),
                (8, "canonical_export_usage_ledger", V8_CHECKSUM),
            )
            for table in (
                "canonical_export_operations",
                "canonical_export_jobs",
                "canonical_export_revisions",
                "canonical_usage_occurrences",
                "canonical_export_receipts",
            ):
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                )
            self.assertIn(
                "native_nonce",
                {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(canonical_export_operations)"
                    )
                },
            )
            for table in ("canonical_export_jobs", "canonical_export_receipts"):
                columns = {
                    row[1]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                self.assertNotIn("native_nonce", columns)
                self.assertNotIn("runtime_epoch", columns)
            job_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(canonical_export_jobs)"
                )
            }
            self.assertTrue(
                {
                    "commit_attestation_json",
                    "commit_attestation_sha256",
                }.issubset(job_columns)
            )

    def test_user_export_registration_never_retypes_preview_authority(self) -> None:
        preview = self.repository.register_preview_root(self.output)
        with self.assertRaises(CanonicalExportJobConflictError) as raised:
            self.repository.register_user_export_root(self.output)
        self.assertEqual(
            raised.exception.code,
            "canonical_export_root_kind_conflict",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT id,kind,canonical_path FROM output_roots WHERE id=?",
                    (preview["id"],),
                ).fetchone(),
                (preview["id"], "app_preview", str(self.output)),
            )

    def test_v7_to_v8_backup_is_recoverable_and_collision_or_tamper_fails_closed(self) -> None:
        migration_root = self.root / "v7-migration"
        migration_root.mkdir()
        repository, library, db_path = initialize_v7(migration_root)
        try:
            repository.ensure_schema(library)
            backup_dir = db_path.parent / "migration-backups"
            manifest_path = next(backup_dir.glob("media-v7-to-v8-*.manifest.json"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            backup_path = backup_dir / manifest["backup_filename"]
            self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            self.assertEqual(
                hashlib.sha256(backup_path.read_bytes()).hexdigest(),
                manifest["backup_sha256"],
            )
            with closing(sqlite3.connect(backup_path)) as backup, backup:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(backup.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(
                    backup.execute("SELECT schema_version FROM database_meta").fetchone(),
                    (7,),
                )
                self.assertEqual(
                    backup.execute("SELECT MAX(version) FROM schema_migrations").fetchone(),
                    (7,),
                )
                for _object_type, name in _V8_SCHEMA_OBJECTS:
                    self.assertIsNone(
                        backup.execute(
                            "SELECT 1 FROM sqlite_schema WHERE name=?",
                            (name,),
                        ).fetchone()
                    )
            restored_path = migration_root / "restored-v7.db"
            restored_path.write_bytes(backup_path.read_bytes())
            restored = MediaRepository(restored_path)
            try:
                restored.ensure_schema(library)
            finally:
                restored.close()
            with closing(sqlite3.connect(restored_path)) as restored_connection, restored_connection:
                self.assertEqual(
                    restored_connection.execute(
                        "SELECT schema_version FROM database_meta"
                    ).fetchone(),
                    (SCHEMA_VERSION,),
                )
        finally:
            repository.close()

        collision_root = self.root / "v8-collision"
        collision_root.mkdir()
        repository, library, db_path = initialize_v7(collision_root)
        try:
            with closing(sqlite3.connect(db_path)) as connection, connection:
                connection.execute("CREATE TABLE canonical_export_jobs(fake TEXT)")
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v8_schema_name_collision:canonical_export_jobs:table",
            ):
                repository.ensure_schema(library)
            self.assertFalse((db_path.parent / "migration-backups").exists())
        finally:
            repository.close()

        physical_root = self.root / "v8-physical"
        physical_root.mkdir()
        repository, library, db_path = timeline_persistence.initialize_fresh(physical_root)
        repository.close()
        with closing(sqlite3.connect(db_path)) as connection, connection:
            connection.execute("DROP TRIGGER trg_canonical_export_receipts_no_delete")
        reopened = MediaRepository(db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v8_physical_schema_mismatch:trg_canonical_export_receipts_no_delete",
            ):
                reopened.ensure_schema(library)
        finally:
            reopened.close()

    def test_exact_presentation_native_replay_and_nonce_conflict(self) -> None:
        project_id, presented, envelope, command = self._prepared_export()
        replay = self.repository.execute_canonical_export_command(
            authenticated_principal="main_native_user_gesture",
            project_id=project_id,
            idempotency_key="export-one",
            envelope=envelope,
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, command.response)
        presentation = presented["presentation"]
        self.assertEqual(
            presentation["package_roles"],
            [
                "final_video",
                "script",
                "package_manifest",
                "human_usage_list",
                "completion_marker",
            ],
        )
        self.assertGreater(presentation["duration_ms"], 0)
        self.assertGreater(presentation["clip_count"], 0)
        self.assertNotIn("native_nonce", canonical_json(command.response))
        self.assertNotIn(str(self.output), canonical_json(command.response))

        other = dict(envelope)
        other["package_basename"] = "other-film"
        material = {key: value for key, value in other.items() if key != "request_sha256"}
        other["request_sha256"] = content_sha256(material)
        with self.assertRaises(CanonicalExportReceiptConflictError):
            self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="export-one",
                envelope=other,
            )
        with self.assertRaises(CanonicalExportNativeNonceConflictError):
            self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="export-two",
                envelope=envelope,
            )

    def test_concurrent_command_has_one_operation_and_faults_roll_back(self) -> None:
        project_id, presented, envelope = self._prepared_request(nonce="nonce-race")

        def submit(_index: int):
            return self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="export-race",
                envelope=envelope,
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(submit, range(16)))
        self.assertEqual(sum(not result.replayed for result in results), 1)
        self.assertEqual({result.resource_id for result in results}, {results[0].resource_id})
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "canonical_export_operations",
                        "canonical_export_jobs",
                        "canonical_export_receipts",
                    )
                ),
                (1, 1, 1),
            )

        root = self.repository.register_user_export_root(self.output)
        failed_envelope = self.repository.build_canonical_export_envelope(
            presentation=presented["presentation"],
            presentation_sha256=presented["presentation_sha256"],
            main_native_user_gesture=True,
            runtime_epoch="runtime-epoch-two",
            native_nonce="nonce-fault",
            output_root_id=root["id"],
            permission_fingerprint=root["permission_fingerprint"],
            package_basename="fault-film",
        )
        with self.assertRaisesRegex(RuntimeError, "injected-create-fault"):
            self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="export-fault",
                envelope=failed_envelope,
                _fault_injector=lambda _stage: (_ for _ in ()).throw(
                    RuntimeError("injected-create-fault")
                ),
            )
        self.assertEqual(self.repository._prepared_export_roots, {})
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_export_operations WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                1,
            )

        job_id = results[0].resource_id
        self._advance_commit_pending(job_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            job_id,
            presented,
        )
        with self.assertRaisesRegex(RuntimeError, "injected-complete-fault"):
            self.repository.complete_canonical_export_job(
                job_id=job_id,
                artifact_proof=proof,
                occurrences=occurrences,
                runtime_manifest=runtime,
                _fault_injector=lambda _stage: (_ for _ in ()).throw(
                    RuntimeError("injected-complete-fault")
                ),
            )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                (
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_export_revisions WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_usage_occurrences WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT status FROM canonical_export_jobs WHERE id=?",
                        (job_id,),
                    ).fetchone()[0],
                ),
                (0, 0, "commit_pending"),
            )

    def test_success_atomically_appends_revision_and_usage(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export()
        job_id = command.resource_id
        self._advance_commit_pending(job_id)
        proof, occurrences, runtime = self._completion_inputs(project_id, job_id, presented)
        revision = self.repository.complete_canonical_export_job(
            job_id=job_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        replay = self.repository.reconcile_canonical_export_commit_pending(
            job_id=job_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        self.assertEqual(replay, revision)
        self.assertEqual(revision["usage_occurrences"], occurrences)
        self.assertEqual(revision["package_manifest"]["usage_occurrences"], occurrences)
        self.assertEqual(revision["package_manifest"]["runtime_manifest"], runtime)
        ledger = self.repository.validate_canonical_export_ledger(project_id)
        self.assertEqual(
            (
                ledger["operation_count"],
                ledger["job_count"],
                ledger["revision_count"],
                ledger["usage_occurrence_count"],
            ),
            (1, 1, 1, len(occurrences)),
        )
        self.assertEqual(
            self.repository.get_canonical_export_job(job_id)["status"],
            "succeeded",
        )
        asset_ids = [str(item["asset_id"]) for item in occurrences]
        projected_usage = self.repository.canonical_usage_occurrences_for_search(
            asset_ids
        )
        self.assertEqual(
            sorted(
                [
                    {
                        key: item[key]
                        for key in item
                        if key not in {"project_id", "export_revision"}
                    }
                    for item in projected_usage
                ],
                key=lambda item: (str(item["asset_id"]), str(item["clip_id"])),
            ),
            sorted(
                occurrences,
                key=lambda item: (str(item["asset_id"]), str(item["clip_id"])),
            ),
        )
        self.assertEqual(
            {
                (item["project_id"], item["export_revision"])
                for item in projected_usage
            },
            {(project_id, 1)},
        )

    def test_material_search_projects_complete_derivative_facts_and_revision(self) -> None:
        output_sha256 = hashlib.sha256(b"same-canonical-output").hexdigest()
        project_id, presented, _envelope, command = self._prepared_export(
            key="derivative-fact-one",
            nonce="nonce-derivative-fact-one",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            video_sha256=output_sha256,
        )
        revision = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )

        first = self.repository.canonical_material_search_facts([])
        expected_fact = {
            "project_id": project_id,
            "export_revision": revision["revision"],
            "export_revision_sha256": revision["revision_sha256"],
            "job_id": revision["job_id"],
            "operation_id": revision["operation_id"],
            "output_role": "canonical_rendered_video",
            "video_sha256": output_sha256,
        }
        self.assertEqual(first["usage_occurrences"], [])
        self.assertEqual(first["derivative_facts"], [expected_fact])
        self.assertEqual(
            first["derivative_revision"],
            content_sha256(first["derivative_facts"]),
        )

        second_project, second_presented, _second_envelope, second_command = (
            self._prepared_export(
                key="derivative-fact-two",
                nonce="nonce-derivative-fact-two",
            )
        )
        self._advance_commit_pending(second_command.resource_id)
        second_proof, second_occurrences, second_runtime = self._completion_inputs(
            second_project,
            second_command.resource_id,
            second_presented,
            video_sha256=output_sha256,
        )
        self.repository.complete_canonical_export_job(
            job_id=second_command.resource_id,
            artifact_proof=second_proof,
            occurrences=second_occurrences,
            runtime_manifest=second_runtime,
        )
        second = self.repository.canonical_material_search_facts([])
        self.assertEqual(len(second["derivative_facts"]), 2)
        self.assertEqual(
            {fact["video_sha256"] for fact in second["derivative_facts"]},
            {output_sha256},
        )
        self.assertNotEqual(
            second["derivative_revision"],
            first["derivative_revision"],
        )

    def test_non_successful_export_states_are_audited_but_contribute_no_derivative(self) -> None:
        commit_project, commit_presented, _envelope, commit_command = (
            self._prepared_export(
                key="derivative-commit-pending",
                nonce="nonce-derivative-commit-pending",
            )
        )
        self._advance_commit_pending(commit_command.resource_id)
        self._completion_inputs(
            commit_project,
            commit_command.resource_id,
            commit_presented,
            video_sha256=hashlib.sha256(b"commit-pending-output").hexdigest(),
        )

        failed_project, _failed_presented, _failed_envelope, failed_command = (
            self._prepared_export(
                key="derivative-failed",
                nonce="nonce-derivative-failed",
            )
        )
        self.repository.update_canonical_export_job(
            job_id=failed_command.resource_id,
            status="failed",
            stage="failed",
            progress=0.0,
            error={
                "code": "render_failed",
                "message": "Render failed before commit.",
                "retryable": True,
            },
            expected_status="requested",
        )

        _cancelled_project, _cancelled_presented, _cancelled_envelope, cancelled = (
            self._prepared_export(
                key="derivative-cancelled",
                nonce="nonce-derivative-cancelled",
            )
        )
        self.repository.request_canonical_export_cancel(cancelled.resource_id)

        interrupted_project, _interrupted_presented, _interrupted_envelope, interrupted = (
            self._prepared_export(
                key="derivative-interrupted",
                nonce="nonce-derivative-interrupted",
            )
        )
        interrupted_jobs = self.repository.mark_canonical_export_jobs_interrupted(
            project_id=interrupted_project,
        )
        self.assertEqual(
            [job["id"] for job in interrupted_jobs],
            [interrupted.resource_id],
        )

        facts = self.repository.canonical_material_search_facts([])
        self.assertEqual(facts["derivative_facts"], [])
        self.assertEqual(facts["derivative_revision"], content_sha256([]))
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                {
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM canonical_export_jobs"
                    ).fetchall()
                },
                {"commit_pending", "failed", "cancelled", "interrupted"},
            )

    def test_successful_output_rename_copy_is_excluded_but_pending_output_is_not(self) -> None:
        successful_bytes = b"successful-canonical-output-bytes"
        successful_sha256 = hashlib.sha256(successful_bytes).hexdigest()
        project_id, presented, _envelope, command = self._prepared_export(
            key="derivative-search-success",
            nonce="nonce-derivative-search-success",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            video_sha256=successful_sha256,
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        renamed = self._register_image(
            "renamed-successful-output.jpg",
            successful_bytes,
        )
        copied_path = self.library / "copied-successful-output.jpg"
        copied_path.write_bytes(successful_bytes)
        copied_stat = copied_path.stat()
        copied = self.repository.upsert_asset_source(
            root_id=str(self.repository.register_library_root(self.library)["id"]),
            relative_path=copied_path.name,
            filename=copied_path.name,
            kind="image",
            sha256=successful_sha256,
            mime_type="image/jpeg",
            file_size=copied_stat.st_size,
            mtime_ns=copied_stat.st_mtime_ns,
            source_file_id=str(copied_stat.st_ino),
        )
        self.assertEqual(renamed["id"], copied["id"])

        pending_bytes = b"commit-pending-output-bytes"
        pending_sha256 = hashlib.sha256(pending_bytes).hexdigest()
        pending_project, pending_presented, _pending_envelope, pending_command = (
            self._prepared_export(
                key="derivative-search-pending",
                nonce="nonce-derivative-search-pending",
            )
        )
        self._advance_commit_pending(pending_command.resource_id)
        self._completion_inputs(
            pending_project,
            pending_command.resource_id,
            pending_presented,
            video_sha256=pending_sha256,
        )
        pending_asset = self._register_image(
            "pending-output-material.jpg",
            pending_bytes,
        )

        candidates, _heads = self.repository.mixed_candidates()
        by_id = {str(candidate["id"]): candidate for candidate in candidates}
        self.assertEqual(by_id[str(renamed["id"])]["asset_sha256"], successful_sha256)
        self.assertEqual(
            by_id[str(pending_asset["id"])]["asset_sha256"],
            pending_sha256,
        )
        retrieval = MixedRetrievalService(self.repository)
        excluded = retrieval.search(
            {
                "query": "copied successful output",
                "types": ["image"],
                "top_k": 100,
            }
        )
        excluded_ids = {
            str(item["id"])
            for item in [*excluded["results"], *excluded["audit_results"]]
        }
        self.assertNotIn(str(renamed["id"]), excluded_ids)
        self.assertRegex(excluded["derivative_revision"], r"^[0-9a-f]{64}$")

        retained = retrieval.search(
            {
                "query": "pending output material",
                "types": ["image"],
                "top_k": 100,
            }
        )
        self.assertIn(
            str(pending_asset["id"]),
            {str(item["id"]) for item in retained["results"]},
        )
        pending_match = next(
            item
            for item in retained["results"]
            if item["id"] == pending_asset["id"]
        )
        self.assertEqual(pending_match["asset_sha256"], pending_sha256)

    def test_duplicate_occurrences_and_video_half_open_interval_are_preserved(self) -> None:
        opening = self._register_image("video-opening.jpg", b"video-opening")
        span_ref = self._register_video_span()
        video_semantic = timeline_integration.semantic(
            "video-export",
            evidence_ref=f"memolens://evidence/asset/{opening['id']}",
        )
        video_semantic["material_hints"].append(
            {
                "hint_id": "ending_video_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact current video span.",
            }
        )
        video_project, _ = self._materialize_semantic_project(
            video_semantic,
            key="video",
        )
        presented, command = self._present_existing_project(
            video_project,
            key="export-video",
            nonce="nonce-video",
            package_basename="video-film",
        )
        video_occurrences = self.repository.build_canonical_usage_occurrences(
            project_id=video_project,
            timeline_revision=presented["presentation"]["timeline_binding"]["revision"],
            timeline_revision_sha256=presented["presentation"]["timeline_binding"][
                "revision_sha256"
            ],
        )
        video_occurrence = next(
            item for item in video_occurrences if item["media_kind"] == "video"
        )
        self.assertEqual(len(video_occurrences), 2)
        self.assertEqual([item["ordinal"] for item in video_occurrences], [0, 1])
        self.assertEqual(video_occurrence["source_start_ms"], 1000)
        self.assertEqual(
            video_occurrence["source_end_ms"] - video_occurrence["source_start_ms"],
            video_occurrence["timeline_end_ms"]
            - video_occurrence["timeline_start_ms"],
        )
        self.assertGreater(
            video_occurrence["source_end_ms"],
            video_occurrence["source_start_ms"],
        )
        timeline_revision = self.repository.get_canonical_timeline_revision(
            video_project
        )
        assert timeline_revision is not None
        duplicate_timeline = deepcopy(timeline_revision)
        duplicate_clip = deepcopy(duplicate_timeline["timeline"]["tracks"][0]["clips"][1])
        duplicate_clip["clip_id"] = "clip_duplicate_video"
        duplicate_binding = deepcopy(duplicate_timeline["source_bindings"][1])
        duplicate_binding["clip_id"] = duplicate_clip["clip_id"]
        duplicate_timeline["timeline"]["tracks"][0]["clips"].append(
            duplicate_clip
        )
        duplicate_timeline["source_bindings"].append(duplicate_binding)
        duplicate_occurrences = (
            self.repository._canonical_usage_occurrences_for_timeline(
                duplicate_timeline
            )
        )
        self.assertEqual(
            [item["asset_id"] for item in duplicate_occurrences[1:]],
            [video_occurrence["asset_id"], video_occurrence["asset_id"]],
        )
        self.assertEqual(
            [item["source_start_ms"] for item in duplicate_occurrences[1:]],
            [video_occurrence["source_start_ms"], video_occurrence["source_start_ms"]],
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            video_project,
            command.resource_id,
            presented,
        )
        stored = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        self.assertEqual(stored["usage_occurrences"], video_occurrences)

    def test_structural_split_then_delete_drives_exact_export_usage(self) -> None:
        opening = self._register_image(
            "structural-video-opening.jpg",
            b"structural-video-opening",
        )
        span_ref = self._register_video_span()
        video_semantic = timeline_integration.semantic(
            "structural-video-export",
            evidence_ref=f"memolens://evidence/asset/{opening['id']}",
        )
        video_semantic["material_hints"].append(
            {
                "hint_id": "structural_ending_video_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact current video span.",
            }
        )
        project_id, _blueprint = self._materialize_semantic_project(
            video_semantic,
            key="structural-video",
        )

        parent = self.timelines.read(project_id)
        parent_video = next(
            clip
            for clip in parent["timeline"]["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        )
        parent_source = next(
            binding
            for binding in parent["source_bindings"]
            if binding["clip_id"] == parent_video["clip_id"]
        )
        source_in_ms = int(parent_source["source_in_ms"])
        source_out_ms = int(parent_source["source_out_ms"])
        source_split_ms = source_in_ms + (source_out_ms - source_in_ms) // 2
        split_result = self.timelines.apply_structural_edit(
            project_id,
            {
                "expected_blueprint": parent["head"]["blueprint_binding"],
                "expected_coverage": parent["head"]["coverage_binding"],
                "expected_timeline_head": parent["head"],
                "structural_edit": {
                    "op": "split_clip",
                    "clip_id": parent_video["clip_id"],
                    "source_split_ms": source_split_ms,
                },
            },
            idempotency_key="structural-export-split",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(split_result.response_status, 201)

        split_state = self.timelines.read(project_id)
        self.assertEqual(split_state["head"]["revision"], 2)
        self.assertEqual(split_state["timeline"]["schema_version"], "2")
        split_children = [
            clip
            for clip in split_state["timeline"]["tracks"][0]["clips"]
            if clip["media_kind"] == "video"
        ]
        split_sources_by_clip = {
            binding["clip_id"]: binding
            for binding in split_state["source_bindings"]
        }
        split_child_sources = [
            split_sources_by_clip[child["clip_id"]] for child in split_children
        ]
        child_ids = [str(child["clip_id"]) for child in split_children]
        expected_intervals = [
            (source_in_ms, source_split_ms),
            (source_split_ms, source_out_ms),
        ]
        self.assertEqual(len(split_children), 2)
        self.assertEqual(len(set(child_ids)), 2)
        self.assertNotIn(str(parent_video["clip_id"]), child_ids)
        self.assertEqual(
            [
                (source["source_in_ms"], source["source_out_ms"])
                for source in split_child_sources
            ],
            expected_intervals,
        )
        shared_source_fields = (
            "evidence_ref",
            "asset_id",
            "asset_sha256",
            "asset_source_id",
            "coverage_proof_sha256",
            "media_kind",
            "span_id",
            "analysis_run_id",
            "analysis_revision",
            "input_asset_sha256",
        )
        for field in shared_source_fields:
            self.assertEqual(
                [source[field] for source in split_child_sources],
                [parent_source[field], parent_source[field]],
            )

        split_presented, split_command = self._present_existing_project(
            project_id,
            key="export-structural-split",
            nonce="nonce-structural-split",
            package_basename="structural-split",
        )
        self._advance_commit_pending(split_command.resource_id)
        split_proof, split_occurrences, split_runtime = self._completion_inputs(
            project_id,
            split_command.resource_id,
            split_presented,
        )
        split_export = self.repository.complete_canonical_export_job(
            job_id=split_command.resource_id,
            artifact_proof=split_proof,
            occurrences=split_occurrences,
            runtime_manifest=split_runtime,
        )
        split_usage_children = [
            occurrence
            for occurrence in split_export["usage_occurrences"]
            if occurrence["media_kind"] == "video"
        ]
        self.assertEqual(
            [occurrence["clip_id"] for occurrence in split_usage_children],
            child_ids,
        )
        self.assertEqual(
            [
                (occurrence["source_start_ms"], occurrence["source_end_ms"])
                for occurrence in split_usage_children
            ],
            expected_intervals,
        )
        split_actual_read = split_export["runtime_manifest"][
            "actual_read_manifest"
        ]
        split_actual_children = [
            binding
            for binding in split_actual_read["source_bindings"]
            if binding["clip_id"] in child_ids
        ]
        self.assertEqual(split_actual_children, split_child_sources)

        deleted_child_id = child_ids[0]
        delete_result = self.timelines.apply_structural_edit(
            project_id,
            {
                "expected_blueprint": split_state["head"]["blueprint_binding"],
                "expected_coverage": split_state["head"]["coverage_binding"],
                "expected_timeline_head": split_state["head"],
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": deleted_child_id,
                },
            },
            idempotency_key="structural-export-delete",
            expected_database_uuid=self.repository.database_uuid,
        )
        self.assertEqual(delete_result.response_status, 201)
        delete_state = self.timelines.read(project_id)
        self.assertEqual(delete_state["head"]["revision"], 3)
        self.assertNotIn(
            deleted_child_id,
            {
                str(clip["clip_id"])
                for clip in delete_state["timeline"]["tracks"][0]["clips"]
            },
        )
        self.assertNotIn(
            deleted_child_id,
            {
                str(binding["clip_id"])
                for binding in delete_state["source_bindings"]
            },
        )

        delete_presented, delete_command = self._present_existing_project(
            project_id,
            key="export-structural-delete",
            nonce="nonce-structural-delete",
            package_basename="structural-delete",
        )
        self._advance_commit_pending(delete_command.resource_id)
        delete_proof, delete_occurrences, delete_runtime = self._completion_inputs(
            project_id,
            delete_command.resource_id,
            delete_presented,
        )
        delete_export = self.repository.complete_canonical_export_job(
            job_id=delete_command.resource_id,
            artifact_proof=delete_proof,
            occurrences=delete_occurrences,
            runtime_manifest=delete_runtime,
        )
        self.assertEqual(delete_export["revision"], 2)
        self.assertNotIn(
            deleted_child_id,
            {
                str(occurrence["clip_id"])
                for occurrence in delete_export["usage_occurrences"]
            },
        )
        self.assertNotIn(
            deleted_child_id,
            {
                str(binding["clip_id"])
                for binding in delete_export["runtime_manifest"][
                    "actual_read_manifest"
                ]["source_bindings"]
            },
        )
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=2,
            ),
            delete_export["usage_occurrences"],
        )
        projected_video_usage = self.repository.canonical_usage_occurrences_for_search(
            [str(parent_video["asset_id"])]
        )
        self.assertEqual(
            {
                int(occurrence["export_revision"])
                for occurrence in projected_video_usage
                if occurrence["clip_id"] == deleted_child_id
            },
            {1},
        )
        self.assertEqual(
            self.repository.validate_canonical_export_ledger(project_id)[
                "usage_occurrence_count"
            ],
            len(split_export["usage_occurrences"])
            + len(delete_export["usage_occurrences"]),
        )

    def test_stage_rejects_stale_unselected_coverage_evidence_atomically(self) -> None:
        project_id, _blueprint, coverage, payload = self._build_project(
            include_unselected_alternative=True
        )
        self._materialize(project_id, payload, key="timeline-alternative")
        presented, command = self._present_existing_project(
            project_id,
            key="export-alternative",
            nonce="nonce-alternative",
            package_basename="alternative-film",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            stage=False,
        )
        timeline = self.repository.get_canonical_timeline_revision(project_id)
        assert timeline is not None
        selected_assets = {
            str(item["asset_id"]) for item in timeline["source_bindings"]
        }
        alternative_asset = next(
            str(item["proof"]["asset_id"])
            for item in coverage["plan"]["evidence_manifest"]
            if str(item["proof"]["asset_id"]) not in selected_assets
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            alternative_source = connection.execute(
                "SELECT id FROM asset_sources WHERE asset_id=? ORDER BY id LIMIT 1",
                (alternative_asset,),
            ).fetchone()[0]
        self.repository.mark_source_availability(alternative_source, "missing")
        with self.assertRaises(CanonicalExportJobConflictError) as raised:
            self.repository.stage_canonical_export_commit(
                job_id=command.resource_id,
                occurrences=occurrences,
                runtime_manifest=runtime,
                **{
                    key: proof[key]
                    for key in (
                        "video_sha256",
                        "video_size_bytes",
                        "video_duration_ms",
                        "script_sha256",
                        "script_size_bytes",
                    )
                },
            )
        self.assertEqual(
            raised.exception.code,
            "canonical_export_coverage_evidence_stale",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                (
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_export_revisions WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_usage_occurrences WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    connection.execute(
                        """SELECT status,commit_attestation_json
                             FROM canonical_export_jobs WHERE id=?""",
                        (command.resource_id,),
                    ).fetchone(),
                ),
                (0, 0, ("packaging", None)),
            )

    def test_attested_export_survives_blueprint_head_advance(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-history",
            nonce="nonce-history",
        )
        self._advance_commit_pending(command.resource_id)
        self.assertEqual(self.repository.active_job_count(), 1)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        stored = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        self._advance_blueprint(project_id)
        self.assertEqual(
            self.repository.get_canonical_export_revision(
                project_id=project_id,
                revision=stored["revision"],
            ),
            stored,
        )
        with self.assertRaises(CanonicalExportJobConflictError) as presentation_error:
            self.repository.build_canonical_export_presentation(project_id)
        self.assertEqual(
            presentation_error.exception.code,
            "canonical_export_blueprint_stale",
        )

        pending_project, pending_presented, _pending_envelope, pending = (
            self._prepared_export(
                key="export-pending-stale",
                nonce="nonce-pending-stale",
            )
        )
        self._advance_commit_pending(pending.resource_id)
        pending_proof, pending_occurrences, pending_runtime = self._completion_inputs(
            pending_project,
            pending.resource_id,
            pending_presented,
        )
        self._advance_blueprint(pending_project)
        pending_revision = self.repository.complete_canonical_export_job(
            job_id=pending.resource_id,
            artifact_proof=pending_proof,
            occurrences=pending_occurrences,
            runtime_manifest=pending_runtime,
        )
        self.assertEqual(pending_revision["revision"], 1)

    def test_complete_recomputes_runtime_script_and_physical_package_proofs(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-proof-oracle",
            nonce="nonce-proof-oracle",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        wrong_physical = {**proof, "human_usage_sha256": "f" * 64}
        with self.assertRaises(CanonicalExportJobConflictError) as physical_error:
            self.repository.complete_canonical_export_job(
                job_id=command.resource_id,
                artifact_proof=wrong_physical,
                occurrences=occurrences,
                runtime_manifest=runtime,
            )
        self.assertEqual(
            physical_error.exception.code,
            "canonical_export_commit_attestation_mismatch",
        )

        wrong_script = deepcopy(proof)
        wrong_script["script_sha256"] = "e" * 64
        wrong_script["script_size_bytes"] = 999
        wrong_script["package_manifest"]["script"] = {
            "sha256": wrong_script["script_sha256"],
            "size_bytes": wrong_script["script_size_bytes"],
        }
        wrong_script.update(
            self.repository._canonical_export_physical_artifact_bindings(
                wrong_script["package_manifest"]
            )
        )
        wrong_script.pop("completion_marker", None)
        with self.assertRaises(CanonicalExportJobConflictError) as script_error:
            self.repository.complete_canonical_export_job(
                job_id=command.resource_id,
                artifact_proof=wrong_script,
                occurrences=occurrences,
                runtime_manifest=runtime,
            )
        self.assertEqual(
            script_error.exception.code,
            "canonical_export_commit_attestation_mismatch",
        )

        wrong_runtime = deepcopy(runtime)
        wrong_runtime["fps"] = 24
        wrong_runtime_proof = deepcopy(proof)
        wrong_runtime_proof["package_manifest"]["runtime_manifest"] = wrong_runtime
        wrong_runtime_proof["package_manifest"]["runtime_manifest_sha256"] = (
            content_sha256(wrong_runtime)
        )
        wrong_runtime_proof.update(
            self.repository._canonical_export_physical_artifact_bindings(
                wrong_runtime_proof["package_manifest"]
            )
        )
        wrong_runtime_proof.pop("completion_marker", None)
        with self.assertRaises(CanonicalExportJobConflictError) as runtime_error:
            self.repository.complete_canonical_export_job(
                job_id=command.resource_id,
                artifact_proof=wrong_runtime_proof,
                occurrences=occurrences,
                runtime_manifest=wrong_runtime,
            )
        self.assertEqual(
            runtime_error.exception.code,
            "canonical_export_commit_attestation_mismatch",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                (
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_export_revisions WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    connection.execute(
                        "SELECT COUNT(*) FROM canonical_usage_occurrences WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                ),
                (0, 0),
            )

    def test_failed_job_has_zero_usage_and_completion_fault_rolls_back(self) -> None:
        project_id, _presented, _envelope, command = self._prepared_export()
        job_id = command.resource_id
        self.repository.update_canonical_export_job(
            job_id=job_id,
            status="failed",
            stage="failed",
            progress=0.2,
            error={"code": "renderer_failed", "message": "Renderer failed.", "retryable": False},
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_usage_occurrences WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )

    def test_interrupted_commit_reconciles_and_terminal_row_is_frozen(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-reconcile",
            nonce="nonce-reconcile",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        interrupted = self.repository.mark_canonical_export_jobs_interrupted(
            project_id=project_id
        )
        self.assertEqual(
            [(item["id"], item["status"]) for item in interrupted],
            [(command.resource_id, "interrupted")],
        )
        stored = self.repository.reconcile_canonical_export_commit_pending(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        self.assertEqual(stored["revision"], 1)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "terminal rows are immutable",
            ):
                connection.execute(
                    "UPDATE canonical_export_jobs SET progress=0.5 WHERE id=?",
                    (command.resource_id,),
                )

    def test_cold_audit_rederives_usage_from_historical_timeline(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-historical-usage",
            nonce="nonce-historical-usage",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        revision = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        rewritten_occurrences = deepcopy(revision["usage_occurrences"])
        rewritten_occurrences[0]["timeline_end_ms"] += 1
        rewritten = self._rewritten_revision_material(
            revision,
            occurrences=rewritten_occurrences,
        )
        self._tamper_immutable_rows(
            [
                (
                    """UPDATE canonical_usage_occurrences
                          SET timeline_end_ms=?,occurrence_sha256=?
                        WHERE project_id=? AND export_revision=? AND ordinal=0""",
                    (
                        rewritten_occurrences[0]["timeline_end_ms"],
                        content_sha256(rewritten_occurrences[0]),
                        project_id,
                        revision["revision"],
                    ),
                ),
                (
                    """UPDATE canonical_export_revisions
                          SET package_manifest_json=?,package_manifest_sha256=?,
                              usage_sha256=?,human_usage_sha256=?,
                              completion_marker_sha256=?,revision_sha256=?
                        WHERE project_id=? AND revision=?""",
                    (
                        canonical_json(rewritten["package_manifest"]),
                        rewritten["package_manifest_sha256"],
                        rewritten["usage_sha256"],
                        rewritten["human_usage_sha256"],
                        rewritten["completion_marker_sha256"],
                        rewritten["revision_sha256"],
                        project_id,
                        revision["revision"],
                    ),
                ),
            ],
            trigger_names=(
                "trg_canonical_usage_occurrences_no_update",
                "trg_canonical_export_revisions_no_update",
            ),
        )
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalExportIntegrityError,
                "canonical_export_historical_usage_mismatch",
            ):
                reopened.validate_canonical_export_ledger(project_id)
        finally:
            reopened.close()

    def test_cold_audit_rederives_script_from_historical_blueprint(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-historical-script",
            nonce="nonce-historical-script",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        revision = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        rewritten = self._rewritten_revision_material(
            revision,
            script={"sha256": "d" * 64, "size_bytes": 77},
        )
        self._tamper_immutable_rows(
            [
                (
                    """UPDATE canonical_export_revisions
                          SET script_sha256=?,script_size_bytes=?,package_manifest_json=?,
                              package_manifest_sha256=?,human_usage_sha256=?,
                              completion_marker_sha256=?,revision_sha256=?
                        WHERE project_id=? AND revision=?""",
                    (
                        rewritten["script"]["sha256"],
                        rewritten["script"]["size_bytes"],
                        canonical_json(rewritten["package_manifest"]),
                        rewritten["package_manifest_sha256"],
                        rewritten["human_usage_sha256"],
                        rewritten["completion_marker_sha256"],
                        rewritten["revision_sha256"],
                        project_id,
                        revision["revision"],
                    ),
                )
            ],
            trigger_names=("trg_canonical_export_revisions_no_update",),
        )
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalExportIntegrityError,
                "canonical_export_revision_attestation_mismatch",
            ):
                reopened.validate_canonical_export_ledger(project_id)
        finally:
            reopened.close()

    def test_cold_audit_revalidates_historical_runtime_contract(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="export-historical-runtime",
            nonce="nonce-historical-runtime",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        revision = self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        rewritten_runtime = deepcopy(revision["runtime_manifest"])
        rewritten_runtime["fps"] = 24
        rewritten = self._rewritten_revision_material(
            revision,
            runtime_manifest=rewritten_runtime,
        )
        self._tamper_immutable_rows(
            [
                (
                    """UPDATE canonical_export_revisions
                          SET runtime_manifest_json=?,runtime_manifest_sha256=?,
                              package_manifest_json=?,package_manifest_sha256=?,
                              human_usage_sha256=?,completion_marker_sha256=?,
                              revision_sha256=?
                        WHERE project_id=? AND revision=?""",
                    (
                        canonical_json(rewritten["runtime_manifest"]),
                        rewritten["runtime_manifest_sha256"],
                        canonical_json(rewritten["package_manifest"]),
                        rewritten["package_manifest_sha256"],
                        rewritten["human_usage_sha256"],
                        rewritten["completion_marker_sha256"],
                        rewritten["revision_sha256"],
                        project_id,
                        revision["revision"],
                    ),
                )
            ],
            trigger_names=("trg_canonical_export_revisions_no_update",),
        )
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalExportIntegrityError,
                "canonical_export_revision_attestation_mismatch",
            ):
                reopened.validate_canonical_export_ledger(project_id)
        finally:
            reopened.close()

    def test_prepared_root_is_atomic_consumed_and_rejections_leave_no_locator(self) -> None:
        project_id, presented, envelope = self._prepared_request(
            nonce="nonce-root-authority"
        )
        root_id = str(envelope["output_root_id"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM output_roots WHERE id=?",
                    (root_id,),
                ).fetchone()
            )
        command = self.repository.execute_canonical_export_command(
            authenticated_principal="main_native_user_gesture",
            project_id=project_id,
            idempotency_key="root-authority",
            envelope=envelope,
        )
        self.assertFalse(command.replayed)
        self.assertEqual(self.repository._prepared_export_roots, {})
        baseline_fd_count = len(os.listdir("/dev/fd"))
        for _index in range(8):
            self.repository.register_user_export_root(self.output)
            replay = self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="root-authority",
                envelope=envelope,
            )
            self.assertTrue(replay.replayed)
            self.assertEqual(self.repository._prepared_export_roots, {})
        self.assertLessEqual(len(os.listdir("/dev/fd")), baseline_fd_count + 1)

        pre_execute_path = self.root / "pre-execute-root"
        pre_execute_path.mkdir()
        rejected_paths = [pre_execute_path]
        pre_execute = self.repository.register_user_export_root(pre_execute_path)
        try:
            with self.assertRaisesRegex(
                ValueError,
                "canonical_export_package_basename_invalid",
            ):
                self.repository.build_canonical_export_envelope(
                    presentation=presented["presentation"],
                    presentation_sha256=presented["presentation_sha256"],
                    main_native_user_gesture=True,
                    runtime_epoch="runtime-pre-execute",
                    native_nonce="nonce-pre-execute",
                    output_root_id=pre_execute["id"],
                    permission_fingerprint=pre_execute[
                        "permission_fingerprint"
                    ],
                    package_basename="../invalid",
                )
        finally:
            self.assertTrue(
                self.repository.discard_prepared_user_export_root(
                    str(pre_execute["id"]),
                    permission_fingerprint=str(
                        pre_execute["permission_fingerprint"]
                    ),
                )
            )
        self.assertEqual(self.repository._prepared_export_roots, {})
        self.assertLessEqual(len(os.listdir("/dev/fd")), baseline_fd_count + 1)

        for name, key, nonce, expected_error in (
            (
                "idempotency-root",
                "root-authority",
                "nonce-idempotency-root",
                CanonicalExportReceiptConflictError,
            ),
            (
                "nonce-root",
                "root-nonce-conflict",
                "nonce-root-authority",
                CanonicalExportNativeNonceConflictError,
            ),
        ):
            rejected = self.root / name
            rejected.mkdir()
            rejected_paths.append(rejected)
            prepared = self.repository.register_user_export_root(rejected)
            rejected_envelope = self.repository.build_canonical_export_envelope(
                presentation=presented["presentation"],
                presentation_sha256=presented["presentation_sha256"],
                main_native_user_gesture=True,
                runtime_epoch=f"runtime-{name}",
                native_nonce=nonce,
                output_root_id=prepared["id"],
                permission_fingerprint=prepared["permission_fingerprint"],
                package_basename=name,
            )
            with self.assertRaises(expected_error):
                self.repository.execute_canonical_export_command(
                    authenticated_principal="main_native_user_gesture",
                    project_id=project_id,
                    idempotency_key=key,
                    envelope=rejected_envelope,
                )
            self.assertEqual(self.repository._prepared_export_roots, {})

        self._advance_blueprint(project_id)
        stale_path = self.root / "stale-root"
        stale_path.mkdir()
        rejected_paths.append(stale_path)
        stale_root = self.repository.register_user_export_root(stale_path)
        stale_envelope = self.repository.build_canonical_export_envelope(
            presentation=presented["presentation"],
            presentation_sha256=presented["presentation_sha256"],
            main_native_user_gesture=True,
            runtime_epoch="runtime-stale-root",
            native_nonce="nonce-stale-root",
            output_root_id=stale_root["id"],
            permission_fingerprint=stale_root["permission_fingerprint"],
            package_basename="stale-root",
        )
        with self.assertRaises(CanonicalExportJobConflictError):
            self.repository.execute_canonical_export_command(
                authenticated_principal="main_native_user_gesture",
                project_id=project_id,
                idempotency_key="root-stale",
                envelope=stale_envelope,
            )
        self.assertEqual(self.repository._prepared_export_roots, {})
        self.assertLessEqual(len(os.listdir("/dev/fd")), baseline_fd_count + 1)

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM output_roots WHERE kind='user_export'"
                ).fetchone(),
                (1,),
            )
            for rejected in rejected_paths:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM output_roots WHERE canonical_path=?",
                        (str(rejected),),
                    ).fetchone()
                )
            frozen_fingerprint = connection.execute(
                "SELECT permission_fingerprint FROM output_roots WHERE id=?",
                (root_id,),
            ).fetchone()[0]
        original_mode = stat.S_IMODE(self.output.stat().st_mode)
        try:
            os.chmod(self.output, original_mode ^ stat.S_IXOTH)
            with self.assertRaises(CanonicalExportJobConflictError):
                self.repository.register_user_export_root(self.output)
        finally:
            os.chmod(self.output, original_mode)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    "SELECT permission_fingerprint FROM output_roots WHERE id=?",
                    (root_id,),
                ).fetchone(),
                (frozen_fingerprint,),
            )

    def test_open_output_root_fd_rejects_validate_reopen_identity_race(self) -> None:
        _project_id, _presented, envelope, _command = self._prepared_export(
            key="root-open-race",
            nonce="nonce-root-open-race",
        )
        root_id = str(envelope["output_root_id"])
        root = self.repository.get_output_root(root_id)
        assert root is not None
        original = self.root / "export-original"
        self.output.rename(original)
        self.output.mkdir()
        try:
            with patch.object(
                self.repository,
                "validate_output_root",
                return_value=(root, self.output),
            ):
                with self.assertRaisesRegex(ValueError, "identity changed"):
                    self.repository.open_output_root_fd(root_id)
        finally:
            self.output.rmdir()
            original.rename(self.output)

    def test_native_selected_root_identity_rejects_replacement_without_side_effects(
        self,
    ) -> None:
        selected = self.root / "native-selected-root"
        selected.mkdir()
        with self.assertRaisesRegex(
            ValueError,
            "user_export_root_expected_identity_invalid",
        ):
            self.repository.register_user_export_root(
                selected,
                expected_device=selected.stat().st_dev,
            )
        with self.assertRaisesRegex(
            ValueError,
            "user_export_root_expected_identity_invalid",
        ):
            self.repository.register_user_export_root(
                selected,
                expected_inode=selected.stat().st_ino,
            )

        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        selection_fd = os.open(selected, flags)
        selection_identity = os.fstat(selection_fd)
        baseline_fd_count = len(os.listdir("/dev/fd"))
        renamed = self.root / "native-selected-root-away"
        selected.rename(renamed)
        selected.mkdir()
        try:
            with self.assertRaisesRegex(
                ValueError,
                "user_export_root_identity_changed",
            ):
                self.repository.register_user_export_root(
                    selected,
                    expected_device=selection_identity.st_dev,
                    expected_inode=selection_identity.st_ino,
                )
            self.assertEqual(self.repository._prepared_export_roots, {})
            self.assertLessEqual(len(os.listdir("/dev/fd")), baseline_fd_count)
            with closing(sqlite3.connect(self.db_path)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM output_roots"
                    ).fetchone(),
                    (0,),
                )
        finally:
            os.close(selection_fd)

    def test_commit_attestation_is_durable_once_and_authoritative(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="commit-attestation",
            nonce="nonce-commit-attestation",
        )
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
            stage=False,
        )
        with self.assertRaisesRegex(
            ValueError,
            "canonical_export_commit_requires_attestation",
        ):
            self.repository.update_canonical_export_job(
                job_id=command.resource_id,
                status="commit_pending",
                stage="commit_pending",
                progress=0.95,
            )
        stage_args = {
            key: proof[key]
            for key in (
                "video_sha256",
                "video_size_bytes",
                "video_duration_ms",
                "script_sha256",
                "script_size_bytes",
            )
        }
        with self.assertRaisesRegex(RuntimeError, "stage-fault"):
            self.repository.stage_canonical_export_commit(
                job_id=command.resource_id,
                occurrences=occurrences,
                runtime_manifest=runtime,
                _fault_injector=lambda _stage: (_ for _ in ()).throw(
                    RuntimeError("stage-fault")
                ),
                **stage_args,
            )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            self.assertEqual(
                connection.execute(
                    """SELECT status,commit_attestation_json
                         FROM canonical_export_jobs WHERE id=?""",
                    (command.resource_id,),
                ).fetchone(),
                ("packaging", None),
            )
        attestation = self.repository.stage_canonical_export_commit(
            job_id=command.resource_id,
            occurrences=occurrences,
            runtime_manifest=runtime,
            **stage_args,
        )
        self.assertEqual(self.repository.active_job_count(), 1)
        self.assertEqual(
            self.repository.get_canonical_export_commit_attestation(
                command.resource_id
            ),
            attestation,
        )
        self.assertEqual(attestation["artifact_proof"], proof)
        self.assertNotIn(str(self.output), canonical_json(attestation))
        self.assertNotIn("native_nonce", canonical_json(attestation))
        self.assertEqual(
            set(self.repository.get_canonical_export_job(command.resource_id)),
            {
                "id",
                "project_id",
                "operation_id",
                "presentation_sha256",
                "request_sha256",
                "timeline_binding",
                "output_binding",
                "status",
                "stage",
                "progress",
                "attempt",
                "cancel_requested",
                "error",
                "created_at",
                "started_at",
                "heartbeat_at",
                "finished_at",
                "result",
            },
        )
        self.assertEqual(
            self.repository.stage_canonical_export_commit(
                job_id=command.resource_id,
                occurrences=occurrences,
                runtime_manifest=runtime,
                **stage_args,
            ),
            attestation,
        )
        with self.assertRaises(CanonicalExportJobConflictError) as conflict:
            self.repository.stage_canonical_export_commit(
                job_id=command.resource_id,
                occurrences=occurrences,
                runtime_manifest=runtime,
                **{**stage_args, "video_sha256": "b" * 64},
            )
        self.assertEqual(
            conflict.exception.code,
            "canonical_export_commit_attestation_conflict",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    """UPDATE canonical_export_jobs
                          SET commit_attestation_sha256=? WHERE id=?""",
                    ("f" * 64, command.resource_id),
                )
        self.repository.mark_canonical_export_jobs_interrupted(
            project_id=project_id
        )
        self.assertEqual(self.repository.active_job_count(), 0)
        with self.assertRaises(CanonicalExportJobConflictError):
            self.repository.request_canonical_export_cancel(command.resource_id)
        revision = self.repository.reconcile_canonical_export_commit_pending(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        self.assertEqual(revision["job_id"], command.resource_id)

        orphan_project, orphan_presented, _orphan_envelope, orphan = (
            self._prepared_export(
                key="unattested-interrupted",
                nonce="nonce-unattested-interrupted",
            )
        )
        self._advance_commit_pending(orphan.resource_id)
        orphan_proof, orphan_occurrences, orphan_runtime = self._completion_inputs(
            orphan_project,
            orphan.resource_id,
            orphan_presented,
            stage=False,
        )
        self.repository.mark_canonical_export_jobs_interrupted(
            project_id=orphan_project
        )
        with self.assertRaises(CanonicalExportJobConflictError) as missing:
            self.repository.reconcile_canonical_export_commit_pending(
                job_id=orphan.resource_id,
                artifact_proof=orphan_proof,
                occurrences=orphan_occurrences,
                runtime_manifest=orphan_runtime,
            )
        self.assertEqual(
            missing.exception.code,
            "canonical_export_commit_attestation_missing",
        )

    def test_active_count_and_job_order_use_export_operation_sequence(self) -> None:
        project_id, _presented, _envelope, first = self._prepared_export(
            key="sequence-first",
            nonce="nonce-sequence-first",
        )
        with patch(
            "core.media_db.utc_now_iso",
            return_value="2000-01-01T00:00:00+00:00",
        ):
            _second_presented, second = self._present_existing_project(
                project_id,
                key="sequence-second",
                nonce="nonce-sequence-second",
                package_basename="sequence-second",
            )
        self.assertEqual(self.repository.active_job_count(), 2)
        self.assertEqual(
            self.repository.list_canonical_export_jobs(
                project_id=project_id,
                limit=1,
            )[0]["id"],
            second.resource_id,
        )
        self.repository.mark_canonical_export_jobs_interrupted(
            project_id=project_id
        )
        self.assertEqual(self.repository.active_job_count(), 0)
        self.assertEqual(
            [
                item["id"]
                for item in self.repository.list_canonical_export_recovery_jobs()
            ],
            [first.resource_id, second.resource_id],
        )
        self.repository.update_canonical_export_job(
            job_id=first.resource_id,
            status="cancelling",
            stage="cancelling",
            progress=0.1,
            expected_status="interrupted",
        )
        self.assertEqual(self.repository.active_job_count(), 1)
        self.repository.mark_canonical_export_jobs_interrupted(
            project_id=project_id
        )
        self.assertEqual(self.repository.active_job_count(), 0)

    def test_cold_audit_detects_commit_attestation_tamper(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export(
            key="attestation-tamper",
            nonce="nonce-attestation-tamper",
        )
        self._advance_commit_pending(command.resource_id)
        self._completion_inputs(project_id, command.resource_id, presented)
        self._tamper_immutable_rows(
            [
                (
                    """UPDATE canonical_export_jobs
                          SET commit_attestation_sha256=? WHERE id=?""",
                    ("f" * 64, command.resource_id),
                )
            ],
            trigger_names=("trg_canonical_export_jobs_attestation_once",),
        )
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                CanonicalExportIntegrityError,
                "canonical_export_commit_attestation_invalid",
            ):
                reopened.validate_canonical_export_ledger(project_id)
        finally:
            reopened.close()

    def test_cold_read_detects_occurrence_tamper(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export()
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id, command.resource_id, presented
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            connection.execute("DROP TRIGGER trg_canonical_usage_occurrences_no_update")
            connection.execute(
                "UPDATE canonical_usage_occurrences SET timeline_end_ms=timeline_end_ms+1"
            )
            connection.commit()
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaises((CanonicalExportIntegrityError, MediaMigrationError)):
                reopened.validate_canonical_export_ledger(project_id)
        finally:
            reopened.close()

    def test_usage_search_cold_audits_export_history_before_claiming_absence(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export()
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id, command.resource_id, presented
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        target_asset_id = str(occurrences[0]["asset_id"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            connection.execute("DROP TRIGGER trg_canonical_usage_occurrences_no_delete")
            connection.execute(
                "DELETE FROM canonical_usage_occurrences WHERE asset_id=?",
                (target_asset_id,),
            )
            connection.commit()
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaises(CanonicalExportIntegrityError):
                reopened.canonical_material_search_facts([])
            with self.assertRaises(CanonicalExportIntegrityError):
                reopened.canonical_usage_occurrences_for_search([target_asset_id])
        finally:
            reopened.close()

    def test_usage_search_detects_operation_and_occurrence_double_delete(self) -> None:
        project_id, presented, _envelope, command = self._prepared_export()
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id, command.resource_id, presented
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        target_asset_id = str(occurrences[0]["asset_id"])
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA recursive_triggers=OFF")
            connection.execute("DROP TRIGGER trg_canonical_usage_occurrences_no_delete")
            connection.execute("DROP TRIGGER trg_canonical_export_operations_no_delete")
            connection.execute(
                "DELETE FROM canonical_usage_occurrences WHERE asset_id=?",
                (target_asset_id,),
            )
            connection.execute(
                "DELETE FROM canonical_export_operations WHERE project_id=?",
                (project_id,),
            )
            connection.commit()
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        try:
            with self.assertRaises(CanonicalExportIntegrityError):
                reopened.canonical_usage_occurrences_for_search([target_asset_id])
        finally:
            reopened.close()

    def test_usage_search_detects_complete_five_table_erasure_after_triggers_restore(
        self,
    ) -> None:
        project_id, presented, _envelope, command = self._prepared_export()
        self._advance_commit_pending(command.resource_id)
        proof, occurrences, runtime = self._completion_inputs(
            project_id,
            command.resource_id,
            presented,
        )
        self.repository.complete_canonical_export_job(
            job_id=command.resource_id,
            artifact_proof=proof,
            occurrences=occurrences,
            runtime_manifest=runtime,
        )
        trigger_names = (
            "trg_canonical_export_receipts_no_delete",
            "trg_canonical_usage_occurrences_no_delete",
            "trg_canonical_export_revisions_no_delete",
            "trg_canonical_export_jobs_no_delete",
            "trg_canonical_export_operations_no_delete",
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "output_roots identity is immutable",
            ):
                connection.execute(
                    """UPDATE output_roots SET kind='app_preview'
                         WHERE kind='user_export'"""
                )
            trigger_sql = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()[0]
                for name in trigger_names
            }
            for name in trigger_names:
                connection.execute(f"DROP TRIGGER {name}")
            for table in (
                "canonical_export_receipts",
                "canonical_usage_occurrences",
                "canonical_export_revisions",
                "canonical_export_jobs",
                "canonical_export_operations",
            ):
                connection.execute(f"DELETE FROM {table} WHERE project_id=?", (project_id,))
            for name in trigger_names:
                connection.execute(str(trigger_sql[name]))
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM output_roots WHERE kind='user_export'"
                ).fetchone()[0],
                1,
            )

        with self.assertRaisesRegex(
            CanonicalExportIntegrityError,
            "canonical_export_output_root_orphaned",
        ):
            self.repository.canonical_material_search_facts([])


if __name__ == "__main__":
    unittest.main()
