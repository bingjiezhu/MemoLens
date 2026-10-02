from __future__ import annotations

from contextlib import closing
import copy
import gc
import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
import warnings
from unittest.mock import patch

from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.image_analysis_schema import V14_SCHEMA_OBJECTS
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import (
    AgentCapabilityError,
    CanonicalTimelineIntegrityError,
    MediaMigrationError,
    MediaRepository,
    SCHEMA_VERSION,
    V9_SCHEMA_STATEMENTS,
    V11_SCHEMA_STATEMENTS,
    V12_CHECKSUM,
    V12_SCHEMA_STATEMENTS,
    V13_CHECKSUM,
    V13_SCHEMA_STATEMENTS,
    canonical_json,
    new_id,
    utc_now_iso,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17
from core.timeline_lowering_contract import canonical_timeline_content_sha256
from core.timeline_restore_contract import restore_canonical_timeline

try:
    from tests import test_timeline_lowering_integration as timeline_fixture
except ImportError:  # pragma: no cover - unittest discovery from tests/
    import test_timeline_lowering_integration as timeline_fixture


_CAPABILITY_COLUMNS = (
    "id,database_uuid,runtime_authority_epoch,project_id,paired_subject_id,"
    "claimed_client_label,CAST(actions_json AS BLOB),secret_sha256,issued_at,"
    "expires_at,max_operations,pairing_receipt_id,facts_sha256"
)
_OPERATION_COLUMNS = (
    "id,project_id,sequence,parent_operation_id,command_type,command_version,"
    "effect_class,expected_blueprint_revision,expected_blueprint_content_sha256,"
    "expected_blueprint_semantic_sha256,expected_blueprint_operation_id,"
    "expected_coverage_revision,expected_coverage_content_sha256,"
    "expected_coverage_evidence_manifest_sha256,expected_coverage_operation_id,"
    "CAST(expected_timeline_head_json AS BLOB),request_sha256,result_revision,"
    "result_revision_sha256,result_timeline_content_sha256,"
    "CAST(result_json AS BLOB),created_at"
)
_RECEIPT_COLUMNS = (
    "id,database_uuid,authenticated_principal,capability_id,paired_subject_id,"
    "claimed_client_label,project_id,command_type,command_version,idempotency_key,"
    "CAST(request_json AS BLOB),request_sha256,CAST(actor_json AS BLOB),"
    "CAST(origin_json AS BLOB),operation_id,response_status,"
    "CAST(response_json AS BLOB),response_sha256,receipt_sha256,resource_type,"
    "resource_id,created_at,receipt_schema_version,request_capture"
)
_EVENT_COLUMNS = (
    "id,capability_id,project_id,sequence,parent_event_id,parent_event_sha256,"
    "event_schema_version,event_type,action,operation_id,agent_command_receipt_id,"
    "agent_project_command_receipt_id,reason_code,presentation_sha256,"
    "native_gesture_nonce,event_sha256,created_at"
)


class V13TimelineRestoreCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v13-restore-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self._pair_sequence = 0

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
        self.assertEqual(
            [warning for warning in caught if warning.category is ResourceWarning],
            [],
        )

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        return timeline_fixture.TimelineLoweringIntegrationTests._build_project(self)

    def _materialize_edit(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        project_id, _blueprint, _coverage, payload = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            payload,
            idempotency_key="v13-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        first = self.timelines.read(project_id)
        first_head = first["head"]
        first_timeline = first["timeline"]
        assert isinstance(first_head, dict) and isinstance(first_timeline, dict)
        clip_id = str(first_timeline["tracks"][0]["clips"][0]["clip_id"])
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": payload["expected_blueprint"],
                "expected_coverage": payload["expected_coverage"],
                "expected_timeline_head": first_head,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1_375,
                },
            },
            idempotency_key="v13-edit",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.repository.get_canonical_timeline_head(project_id)
        target = self.repository.get_canonical_timeline_revision(project_id, 1)
        assert current is not None and target is not None
        return project_id, current, target

    def _pair(
        self,
        project_id: str,
        *,
        actions: list[str],
        max_operations: int = 4,
    ) -> tuple[dict[str, object], str]:
        self._pair_sequence += 1
        suffix = str(self._pair_sequence)
        secret_sha256 = hashlib.sha256(f"secret-{suffix}".encode()).hexdigest()
        arguments = {
            "pairing_id": f"pair_v13_{suffix}",
            "runtime_authority_epoch": "epoch_v13_restore",
            "project_id": project_id,
            "paired_subject_id": f"agent_v13_{suffix}",
            "claimed_client_label": f"V13 local editor {suffix}",
            "actions": actions,
            "ttl_seconds": 900,
            "max_operations": max_operations,
            "pairing_request_sha256": hashlib.sha256(
                f"pairing-{suffix}".encode()
            ).hexdigest(),
        }
        envelope = self.repository.build_agent_pairing_presentation(**arguments)
        issued = self.repository.issue_agent_project_capability(
            capability_id=f"cap_v13_{suffix}",
            secret_sha256=secret_sha256,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce=f"gesture_v13_{suffix}",
            **arguments,
        )
        return issued.capability, secret_sha256

    @staticmethod
    def _restore_request(
        repository: MediaRepository,
        project_id: str,
        current: dict[str, object],
        target: dict[str, object],
    ) -> dict[str, object]:
        return {
            "object": "memolens.timeline_restore_revision_command",
            "schema_version": "1",
            "project_id": project_id,
            "expected_database_uuid": repository.database_uuid,
            "expected_blueprint": copy.deepcopy(current["blueprint_binding"]),
            "expected_coverage": copy.deepcopy(current["coverage_binding"]),
            "expected_timeline_head": (
                repository._canonical_timeline_head_projection(current)
            ),
            "restore_from": repository._canonical_timeline_head_projection(target),
        }

    def _restore_mutation(
        self,
        *,
        project_id: str,
        request: dict[str, object],
        request_sha256: str,
    ):
        def mutation(
            connection: sqlite3.Connection,
        ) -> tuple[dict[str, object], int, str, str]:
            current = self.repository.get_canonical_timeline_head_in_transaction(
                connection,
                project_id,
            )
            restore_from = request["restore_from"]
            assert isinstance(current, dict) and isinstance(restore_from, dict)
            target = self.repository.get_canonical_timeline_revision_in_transaction(
                connection,
                project_id,
                int(restore_from["revision"]),
            )
            if (
                target is None
                or self.repository._canonical_timeline_head_projection(current)
                != request["expected_timeline_head"]
                or self.repository._canonical_timeline_head_projection(target)
                != restore_from
            ):
                raise CanonicalTimelineIntegrityError(
                    "canonical_timeline_restore_test_precondition_invalid"
                )
            restored = restore_canonical_timeline(
                current_timeline=current["timeline"],
                current_source_bindings=current["source_bindings"],
                restore_timeline=target["timeline"],
                restore_source_bindings=target["source_bindings"],
            )
            operation_id = new_id("timeline_op")
            next_revision = int(current["revision"]) + 1
            now = utc_now_iso()
            revision = self.repository.append_canonical_timeline_revision(
                connection,
                project_id=project_id,
                revision=next_revision,
                parent_revision=int(current["revision"]),
                parent_revision_sha256=str(current["revision_sha256"]),
                timeline=restored["timeline"],
                source_bindings=restored["source_bindings"],
                blueprint_binding=request["expected_blueprint"],
                coverage_binding=request["expected_coverage"],
                operation_id=operation_id,
                created_at=now,
            )
            result_head = self.repository._canonical_timeline_head_projection(revision)
            result = {
                "kind": "revision_restored",
                "result_head": result_head,
                "restore_from": copy.deepcopy(restore_from),
            }
            self.repository.append_canonical_timeline_operation(
                connection,
                operation_id=operation_id,
                project_id=project_id,
                command_type="timeline.restore_revision",
                command_version="1",
                expected_blueprint=request["expected_blueprint"],
                expected_coverage=request["expected_coverage"],
                expected_timeline_head=request["expected_timeline_head"],
                request_sha256=request_sha256,
                result_revision=next_revision,
                result_revision_sha256=str(revision["revision_sha256"]),
                result_timeline_content_sha256=(
                    canonical_timeline_content_sha256(restored["timeline"])
                ),
                result=result,
                created_at=now,
            )
            self.repository.cas_canonical_timeline_head(
                connection,
                project_id=project_id,
                expected_head=request["expected_timeline_head"],
                revision=next_revision,
            )
            response = {
                "object": "canonical_timeline.command_result",
                "schema_version": "1",
                "project_id": project_id,
                "command_type": "timeline.restore_revision",
                "operation_id": operation_id,
                "result": result,
            }
            return response, 201, operation_id, f"{project_id}:{next_revision}"

        return mutation

    def _downgrade_to_exact_v12(self) -> None:
        downgrade_current_v18_to_exact_v17(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            for object_type, name in reversed(V17_SCHEMA_OBJECTS):
                connection.execute(
                    f'DROP {object_type.upper()} IF EXISTS "{name}"'
                )
            connection.execute(
                MediaRepository._expected_v14_physical_schema()[
                    ("trigger", "trg_image_generations_activate_clean_only")
                ]
            )
            for object_type, name in reversed(V16_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            for object_type, name in reversed(V14_SCHEMA_OBJECTS):
                connection.execute(f'DROP {object_type.upper()} IF EXISTS "{name}"')
            connection.execute(
                "DELETE FROM schema_migrations WHERE version IN (14,15,16,17)"
            )
            for name in (
                "trg_agent_capabilities_no_update",
                "trg_agent_capabilities_no_delete",
                "trg_canonical_timeline_operations_no_update",
                "trg_canonical_timeline_operations_no_delete",
                "trg_agent_project_receipts_no_update",
                "trg_agent_project_receipts_no_delete",
                "trg_agent_capability_events_no_update",
                "trg_agent_capability_events_no_delete",
            ):
                connection.execute(f"DROP TRIGGER {name}")
            for name in (
                "idx_agent_capabilities_project_expiry",
                "idx_canonical_timeline_operations_project_sequence",
                "idx_agent_project_receipts_operation",
                "idx_agent_capability_events_sequence",
                "idx_agent_capability_events_native_gesture",
            ):
                connection.execute(f"DROP INDEX {name}")
            for table in (
                "agent_project_capability_events",
                "agent_project_command_receipts",
                "canonical_timeline_operations",
                "agent_project_capabilities",
            ):
                connection.execute(f"ALTER TABLE {table} RENAME TO {table}_v13_fixture")
            for statement in (
                V11_SCHEMA_STATEMENTS[0],
                V9_SCHEMA_STATEMENTS[0],
                V12_SCHEMA_STATEMENTS[0],
                V12_SCHEMA_STATEMENTS[1],
            ):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO agent_project_capabilities "
                "SELECT * FROM agent_project_capabilities_v13_fixture"
            )
            connection.execute(
                "INSERT INTO canonical_timeline_operations "
                "SELECT * FROM canonical_timeline_operations_v13_fixture"
            )
            connection.execute(
                "INSERT INTO agent_project_command_receipts "
                "SELECT * FROM agent_project_command_receipts_v13_fixture"
            )
            connection.execute(
                "INSERT INTO agent_project_capability_events "
                "SELECT * FROM agent_project_capability_events_v13_fixture"
            )
            for table in (
                "agent_project_capability_events_v13_fixture",
                "agent_project_command_receipts_v13_fixture",
                "canonical_timeline_operations_v13_fixture",
                "agent_project_capabilities_v13_fixture",
            ):
                connection.execute(f"DROP TABLE {table}")
            for statement in (
                V11_SCHEMA_STATEMENTS[3],
                V11_SCHEMA_STATEMENTS[7],
                V11_SCHEMA_STATEMENTS[8],
                V9_SCHEMA_STATEMENTS[2],
                V9_SCHEMA_STATEMENTS[4],
                V9_SCHEMA_STATEMENTS[5],
                *V12_SCHEMA_STATEMENTS[2:],
            ):
                connection.execute(statement)
            connection.execute("DELETE FROM schema_migrations WHERE version=13")
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=12 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
            connection.commit()

    def _rebuilt_table_snapshot(self) -> dict[str, tuple[tuple[object, ...], ...]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {columns} FROM {table} ORDER BY rowid"
                    )
                )
                for table, columns in (
                    ("agent_project_capabilities", _CAPABILITY_COLUMNS),
                    ("canonical_timeline_operations", _OPERATION_COLUMNS),
                    ("agent_project_command_receipts", _RECEIPT_COLUMNS),
                    ("agent_project_capability_events", _EVENT_COLUMNS),
                )
            }

    def test_fresh_v13_manifest_and_closed_restore_action(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V12_CHECKSUM,
            "3cb7ae9007333c0cc65bf2f420cb31c50b2c9e2aefc4c857481805ff0722da66",
        )
        self.assertEqual(
            V13_CHECKSUM,
            "c28e18ceebf0c0fe5ca116917068b857db800aacff8ee05513dfeaa6c3dc6f27",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name,checksum FROM schema_migrations WHERE version=13"
                ).fetchone(),
                ("paired_agent_canonical_timeline_restore", V13_CHECKSUM),
            )
            capability_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='agent_project_capabilities'"
            ).fetchone()[0]
            operation_sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='canonical_timeline_operations'"
            ).fetchone()[0]
            self.assertIn("timeline.restore_revision", capability_sql)
            self.assertIn("timeline.restore_revision", operation_sql)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        for _attempt in range(2):
            self.repository.ensure_schema(self.library)

    def test_paired_restore_replays_exact_k_to_n_plus_one_and_edit_only_denies(self) -> None:
        project_id, current, target = self._materialize_edit()
        request = self._restore_request(self.repository, project_id, current, target)
        request_sha256 = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        edit_capability, edit_secret = self._pair(
            project_id,
            actions=["timeline.apply_edit"],
        )
        mutation_called = False

        def forbidden_mutation(_connection: sqlite3.Connection):
            nonlocal mutation_called
            mutation_called = True
            raise AssertionError("edit-only capability reached restore mutation")

        with self.assertRaises(AgentCapabilityError) as denied:
            self.repository.execute_paired_canonical_timeline_command(
                capability_id=str(edit_capability["id"]),
                runtime_authority_epoch="epoch_v13_restore",
                presented_secret_sha256=edit_secret,
                project_id=project_id,
                command_type="timeline.restore_revision",
                command_version="1",
                idempotency_key="v13-edit-only-restore",
                request_value=request,
                request_sha256=request_sha256,
                mutation=forbidden_mutation,
            )
        self.assertEqual(denied.exception.code, "agent_pairing_scope_denied")
        self.assertFalse(mutation_called)

        restore_capability, restore_secret = self._pair(
            project_id,
            actions=["timeline.restore_revision"],
            max_operations=1,
        )
        first = self.repository.execute_paired_canonical_timeline_command(
            capability_id=str(restore_capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=restore_secret,
            project_id=project_id,
            command_type="timeline.restore_revision",
            command_version="1",
            idempotency_key="v13-restore-1",
            request_value=request,
            request_sha256=request_sha256,
            mutation=self._restore_mutation(
                project_id=project_id,
                request=request,
                request_sha256=request_sha256,
            ),
        )
        replay = self.repository.execute_paired_canonical_timeline_command(
            capability_id=str(restore_capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=restore_secret,
            project_id=project_id,
            command_type="timeline.restore_revision",
            command_version="1",
            idempotency_key="v13-restore-1",
            request_value=request,
            request_sha256=request_sha256,
            mutation=forbidden_mutation,
        )
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertFalse(mutation_called)
        self.assertEqual(first.response["result"]["kind"], "revision_restored")
        self.assertEqual(first.response["result"]["restore_from"], request["restore_from"])

        head = self.repository.get_canonical_timeline_head(project_id)
        assert head is not None
        self.assertEqual(head["revision"], 3)
        self.assertEqual(head["timeline"]["revision"], 3)
        self.assertEqual(head["timeline"]["parent"]["revision"], 2)
        expected_timeline = copy.deepcopy(target["timeline"])
        expected_timeline["revision"] = 3
        expected_timeline["parent"] = {
            "revision": 2,
            "content_sha256": current["timeline_content_sha256"],
        }
        self.assertEqual(head["timeline"], expected_timeline)
        self.assertEqual(head["source_bindings"], target["source_bindings"])
        state = self.repository.get_agent_project_capability(
            str(restore_capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
        )
        assert state is not None
        self.assertEqual(state["operations_used"], 1)
        self.assertEqual(state["status"], "exhausted")
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT command_type,receipt_schema_version,request_capture "
                        "FROM agent_project_command_receipts WHERE operation_id=?",
                        (first.operation_id,),
                    ).fetchone()
                ),
                ("timeline.restore_revision", "2", "exact_canonical_json"),
            )
            self.assertEqual(
                tuple(
                    connection.execute(
                        "SELECT action,event_schema_version FROM "
                        "agent_project_capability_events WHERE operation_id=?",
                        (first.operation_id,),
                    ).fetchone()
                ),
                ("timeline.restore_revision", "2"),
            )
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        reopened.ensure_schema(self.library)
        self.repository = reopened
        self.assertEqual(
            self.repository.get_canonical_timeline_head(project_id)["revision"],
            3,
        )

    def test_populated_v12_migrates_with_exact_history_and_verified_backup(self) -> None:
        project_id, current, _target = self._materialize_edit()
        capability, secret = self._pair(
            project_id,
            actions=["timeline.apply_edit"],
        )
        clip_id = str(current["timeline"]["tracks"][0]["clips"][0]["clip_id"])
        paired_edit = self.timelines.apply_paired_edit(
            project_id,
            {
                "expected_blueprint": current["blueprint_binding"],
                "expected_coverage": current["coverage_binding"],
                "expected_timeline_head": (
                    self.repository._canonical_timeline_head_projection(current)
                ),
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1_625,
                },
            },
            idempotency_key="v13-populated-paired-edit",
            expected_database_uuid=self.repository.database_uuid,
            paired_capability_id=str(capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=secret,
        )
        self.assertFalse(paired_edit.replayed)
        current = self.repository.get_canonical_timeline_head(project_id)
        assert current is not None
        expected = self._rebuilt_table_snapshot()
        self.repository.close()
        self._downgrade_to_exact_v12()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            MediaRepository._verify_v9_physical_schema(connection)
            MediaRepository._verify_v11_unreplaced_physical_schema(connection)
            MediaRepository._verify_v12_physical_schema(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                12,
            )

        migrated = MediaRepository(self.db_path)
        migrated.ensure_schema(self.library)
        self.repository = migrated
        self.assertEqual(self._rebuilt_table_snapshot(), expected)
        self.assertEqual(
            self.repository.get_canonical_timeline_head(project_id)["revision"],
            current["revision"],
        )
        backup_dir = self.db_path.parent / "migration-backups"
        artifacts = list(backup_dir.glob("media-v12-to-v13-*"))
        self.assertEqual(len(artifacts), 2)
        manifest_path = next(backup_dir.glob("media-v12-to-v13-*.manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        backup_path = backup_dir / manifest["backup_filename"]
        self.assertEqual(manifest["from_schema_version"], 12)
        self.assertEqual(manifest["to_schema_version"], 13)
        self.assertEqual(stat.S_IMODE(backup_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
        self.assertEqual(
            hashlib.sha256(backup_path.read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(backup_path)) as backup:
            self.assertEqual(
                backup.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (12,),
            )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT name,checksum FROM schema_migrations WHERE version=12"
                ).fetchone(),
                (
                    "paired_agent_project_command_receipt_convergence",
                    V12_CHECKSUM,
                ),
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v13_collision_and_copy_fault_roll_back_exact_v12(self) -> None:
        self.repository.close()
        self._downgrade_to_exact_v12()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "CREATE TABLE agent_project_capabilities_v12(attacker TEXT)"
            )
            connection.commit()
        collided = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v13_schema_name_collision:agent_project_capabilities_v12:table",
            ):
                collided.ensure_schema(self.library)
        finally:
            collided.close()
        self.assertEqual(
            list((self.db_path.parent / "migration-backups").glob("*")),
            [],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TABLE agent_project_capabilities_v12")
            connection.commit()

        faulted = MediaRepository(self.db_path)
        try:
            with patch.object(
                MediaRepository,
                "_assert_v13_projection_equal",
                side_effect=MediaMigrationError("injected_v13_copy_fault"),
            ):
                with self.assertRaisesRegex(
                    MediaMigrationError,
                    "injected_v13_copy_fault",
                ):
                    faulted.ensure_schema(self.library)
        finally:
            faulted.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone()[0],
                12,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=13"
                ).fetchone()[0],
                0,
            )
            for name in (
                "agent_project_capabilities_v12",
                "canonical_timeline_operations_v12",
                "agent_project_command_receipts_v12",
                "agent_project_capability_events_v12",
            ):
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema WHERE name=?",
                        (name,),
                    ).fetchone()
                )
            MediaRepository._verify_v9_physical_schema(connection)
            MediaRepository._verify_v11_unreplaced_physical_schema(connection)
            MediaRepository._verify_v12_physical_schema(connection)
        artifacts = list(
            (self.db_path.parent / "migration-backups").glob(
                "media-v12-to-v13-*"
            )
        )
        self.assertEqual(len(artifacts), 2)
        recovered = MediaRepository(self.db_path)
        recovered.ensure_schema(self.library)
        self.repository = recovered

    def test_future_v21_schema_fails_before_v13_partial_write(self) -> None:
        self.repository.close()
        self._downgrade_to_exact_v12()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "UPDATE database_meta SET schema_version=21 WHERE singleton=1"
            )
            connection.commit()
        future = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(MediaMigrationError, "future_schema_version:21"):
                future.ensure_schema(self.library)
        finally:
            future.close()
        self.assertEqual(
            list((self.db_path.parent / "migration-backups").glob("*")),
            [],
        )

    def test_v12_tamper_fails_before_backup_or_v13_partial_write(self) -> None:
        project_id, current, _target = self._materialize_edit()
        capability, secret = self._pair(
            project_id,
            actions=["timeline.apply_edit"],
        )
        clip_id = str(current["timeline"]["tracks"][0]["clips"][0]["clip_id"])
        saved = self.timelines.apply_paired_edit(
            project_id,
            {
                "expected_blueprint": current["blueprint_binding"],
                "expected_coverage": current["coverage_binding"],
                "expected_timeline_head": (
                    self.repository._canonical_timeline_head_projection(current)
                ),
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip_id,
                    "duration_ms": 1_725,
                },
            },
            idempotency_key="v13-tamper-paired-edit",
            expected_database_uuid=self.repository.database_uuid,
            paired_capability_id=str(capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=secret,
        )
        self.repository.close()
        self._downgrade_to_exact_v12()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("DROP TRIGGER trg_agent_project_receipts_no_update")
            connection.execute(
                "UPDATE agent_project_command_receipts SET response_json='{}' "
                "WHERE operation_id=?",
                (saved.operation_id,),
            )
            connection.execute(V12_SCHEMA_STATEMENTS[5])
            connection.commit()
        rejected = MediaRepository(self.db_path)
        try:
            with self.assertRaisesRegex(
                MediaMigrationError,
                "v12_agent_authority_audit_failed",
            ):
                rejected.ensure_schema(self.library)
        finally:
            rejected.close()
        self.assertEqual(
            list((self.db_path.parent / "migration-backups").glob("*")),
            [],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (12,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=13"
                ).fetchone(),
                (0,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT response_json FROM agent_project_command_receipts "
                    "WHERE operation_id=?",
                    (saved.operation_id,),
                ).fetchone(),
                ("{}",),
            )

    def test_cold_audit_rejects_tampered_restore_target(self) -> None:
        project_id, current, target = self._materialize_edit()
        capability, secret = self._pair(
            project_id,
            actions=["timeline.restore_revision"],
        )
        request = self._restore_request(self.repository, project_id, current, target)
        request_sha256 = hashlib.sha256(canonical_json(request).encode()).hexdigest()
        saved = self.repository.execute_paired_canonical_timeline_command(
            capability_id=str(capability["id"]),
            runtime_authority_epoch="epoch_v13_restore",
            presented_secret_sha256=secret,
            project_id=project_id,
            command_type="timeline.restore_revision",
            command_version="1",
            idempotency_key="v13-tamper-restore-target",
            request_value=request,
            request_sha256=request_sha256,
            mutation=self._restore_mutation(
                project_id=project_id,
                request=request,
                request_sha256=request_sha256,
            ),
        )
        tampered_result = copy.deepcopy(saved.response["result"])
        tampered_result["restore_from"] = copy.deepcopy(
            request["expected_timeline_head"]
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                "UPDATE canonical_timeline_operations SET result_json=? WHERE id=?",
                (canonical_json(tampered_result), saved.operation_id),
            )
            connection.execute(V13_SCHEMA_STATEMENTS[11])
            connection.commit()
        self.repository.close()
        reopened = MediaRepository(self.db_path)
        self.repository = reopened
        with self.assertRaisesRegex(
            MediaMigrationError,
                "v18_structural_edit_authority_audit_failed",
        ):
            reopened.ensure_schema(self.library)


if __name__ == "__main__":
    unittest.main()
