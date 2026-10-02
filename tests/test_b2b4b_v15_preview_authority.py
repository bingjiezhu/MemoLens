from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend.src.agent_authority import PAIRING_ACTIONS as BACKEND_PAIRING_ACTIONS
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.image_read_cutover_schema import V17_SCHEMA_OBJECTS
from core.media_db import (
    MediaMigrationError,
    MediaRepository,
    SCHEMA_VERSION,
    V13_SCHEMA_STATEMENTS,
    V15_CHECKSUM,
    canonical_json,
)
from core.provider_egress_schema import V16_SCHEMA_OBJECTS
from tests.v18_migration_test_support import downgrade_current_v18_to_exact_v17

try:
    from tests import test_timeline_lowering_integration as timeline_fixture
except ImportError:  # pragma: no cover - unittest discovery from tests/
    import test_timeline_lowering_integration as timeline_fixture


PLUGIN_SCRIPTS = (
    Path(__file__).resolve().parents[1]
    / ".agents"
    / "plugins"
    / "plugins"
    / "memolens"
    / "scripts"
)
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

import memolens_agent_client as plugin_client  # noqa: E402
import memolens_agent_credentials as plugin_credentials  # noqa: E402
import memolens_agent_receipts as plugin_receipts  # noqa: E402
import memolens_blueprint_authority as plugin_authority  # noqa: E402


_CAPABILITY_PROJECTION = (
    "id,database_uuid,runtime_authority_epoch,project_id,paired_subject_id,"
    "claimed_client_label,CAST(actions_json AS BLOB),secret_sha256,issued_at,"
    "expires_at,max_operations,pairing_receipt_id,facts_sha256"
)
_PAIRING_RECEIPT_PROJECTION = (
    "id,database_uuid,runtime_authority_epoch,pairing_id,capability_id,project_id,"
    "pairing_request_sha256,observed_revision,observed_content_sha256,"
    "CAST(presentation_json AS BLOB),presentation_sha256,native_gesture_nonce,"
    "receipt_sha256,created_at"
)
_COMMAND_RECEIPT_PROJECTION = (
    "id,database_uuid,authenticated_principal,capability_id,paired_subject_id,"
    "claimed_client_label,project_id,command_type,command_version,idempotency_key,"
    "CAST(request_json AS BLOB),request_sha256,CAST(actor_json AS BLOB),"
    "CAST(origin_json AS BLOB),operation_id,response_status,"
    "CAST(response_json AS BLOB),response_sha256,receipt_sha256,resource_type,"
    "resource_id,created_at,receipt_schema_version,request_capture"
)
_EVENT_PROJECTION = (
    "id,capability_id,project_id,sequence,parent_event_id,parent_event_sha256,"
    "event_schema_version,event_type,action,operation_id,agent_command_receipt_id,"
    "agent_project_command_receipt_id,reason_code,presentation_sha256,"
    "native_gesture_nonce,event_sha256,created_at"
)


class V15PreviewAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v15-preview-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository: MediaRepository | None = None
        self._open_repository()
        self._pair_sequence = 0

    def tearDown(self) -> None:
        if self.repository is not None:
            self.repository.close()
        self.temporary.cleanup()

    def _open_repository(self) -> None:
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)

    def _close_repository(self) -> None:
        assert self.repository is not None
        self.repository.close()
        self.repository = None

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

    def _materialize_project(self) -> tuple[str, dict[str, object]]:
        assert self.repository is not None
        project_id, _blueprint, _coverage, payload = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            payload,
            idempotency_key=f"v15-materialize-{project_id}",
            expected_database_uuid=self.repository.database_uuid,
        )
        return project_id, payload

    def _pair(
        self,
        project_id: str,
        *,
        actions: list[str],
    ) -> tuple[dict[str, object], str, dict[str, object]]:
        assert self.repository is not None
        self._pair_sequence += 1
        suffix = str(self._pair_sequence)
        secret = hashlib.sha256(f"v15-secret-{suffix}".encode()).hexdigest()
        arguments = {
            "pairing_id": f"pair_v15_{suffix}",
            "runtime_authority_epoch": "epoch_v15_preview",
            "project_id": project_id,
            "paired_subject_id": f"agent_v15_{suffix}",
            "claimed_client_label": f"MemoLens preview host {suffix}",
            "actions": actions,
            "ttl_seconds": 900,
            "max_operations": 4,
            "pairing_request_sha256": hashlib.sha256(
                f"v15-pairing-{suffix}".encode()
            ).hexdigest(),
        }
        envelope = self.repository.build_agent_pairing_presentation(**arguments)
        issued = self.repository.issue_agent_project_capability(
            capability_id=f"cap_v15_{suffix}",
            secret_sha256=secret,
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce=f"gesture_v15_{suffix}",
            **arguments,
        )
        return issued.capability, secret, envelope

    @staticmethod
    def _table_snapshot(db_path: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
        projections = {
            "agent_project_capabilities": _CAPABILITY_PROJECTION,
            "agent_pairing_confirmation_receipts": _PAIRING_RECEIPT_PROJECTION,
            "agent_project_command_receipts": _COMMAND_RECEIPT_PROJECTION,
            "agent_project_capability_events": _EVENT_PROJECTION,
        }
        with closing(sqlite3.connect(db_path)) as connection:
            return {
                table: tuple(
                    tuple(row)
                    for row in connection.execute(
                        f"SELECT {projection} FROM {table} ORDER BY rowid"
                    )
                )
                for table, projection in projections.items()
            }

    @staticmethod
    def _assert_exact_v14(db_path: Path) -> None:
        with closing(sqlite3.connect(db_path)) as connection:
            connection.row_factory = sqlite3.Row
            assert MediaRepository._schema_preflight(connection) == 14
            MediaRepository._verify_v13_physical_schema(connection)
            MediaRepository._verify_v14_physical_schema(connection)
            plugin_authority.validate_exact_v14_schema_manifest(connection)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    @classmethod
    def _downgrade_to_exact_v14(cls, db_path: Path) -> None:
        downgrade_current_v18_to_exact_v17(db_path)
        with closing(sqlite3.connect(db_path)) as connection:
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
            connection.execute("DELETE FROM schema_migrations WHERE version IN (16,17)")
            for name in (
                "trg_agent_capabilities_no_update",
                "trg_agent_capabilities_no_delete",
            ):
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute("DROP INDEX idx_agent_capabilities_project_expiry")
            connection.execute(
                "ALTER TABLE agent_project_capabilities "
                "RENAME TO agent_project_capabilities_v15_fixture"
            )
            connection.execute(V13_SCHEMA_STATEMENTS[0])
            connection.execute(
                """INSERT INTO agent_project_capabilities(
                     id,database_uuid,runtime_authority_epoch,project_id,
                     paired_subject_id,claimed_client_label,actions_json,
                     secret_sha256,issued_at,expires_at,max_operations,
                     pairing_receipt_id,facts_sha256)
                   SELECT id,database_uuid,runtime_authority_epoch,project_id,
                          paired_subject_id,claimed_client_label,actions_json,
                          secret_sha256,issued_at,expires_at,max_operations,
                          pairing_receipt_id,facts_sha256
                     FROM agent_project_capabilities_v15_fixture ORDER BY rowid"""
            )
            connection.execute("DROP TABLE agent_project_capabilities_v15_fixture")
            for statement in (
                V13_SCHEMA_STATEMENTS[4],
                V13_SCHEMA_STATEMENTS[9],
                V13_SCHEMA_STATEMENTS[10],
            ):
                connection.execute(statement)
            connection.execute("DELETE FROM schema_migrations WHERE version=15")
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None and guard[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=14 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
            connection.commit()
        cls._assert_exact_v14(db_path)

    def test_fresh_v15_preview_is_paired_read_scope_not_a_write_command(self) -> None:
        assert self.repository is not None
        self.assertEqual(SCHEMA_VERSION, 20)
        self.assertEqual(
            V15_CHECKSUM,
            "d143a791612e9fb072560b2ddbfa72e2ab4b4222bfd416bb393cbd67a879e5c1",
        )
        self.assertEqual(plugin_authority.CURRENT_MANAGED_SCHEMA_VERSION, 20)
        project_id, _payload = self._materialize_project()
        capability, secret, envelope = self._pair(
            project_id,
            actions=["timeline.preview_media"],
        )
        presentation = envelope["presentation"]
        self.assertEqual(presentation["actions"], ["timeline.preview_media"])
        self.assertIn("observed_timeline_head", presentation)
        self.assertEqual(capability["actions"], ["timeline.preview_media"])

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            plugin_authority.validate_exact_managed_schema_manifest(connection)
            audit = plugin_receipts.validate_agent_receipt_ledger(
                connection,
                project_id=project_id,
            )
            self.assertTrue(audit["validated"])
            capability_sql = connection.execute(
                "SELECT sql FROM sqlite_schema "
                "WHERE type='table' AND name='agent_project_capabilities'"
            ).fetchone()[0]
            normalized = MediaRepository._normalized_schema_sql(capability_sql)
            frozen_v15 = MediaRepository._expected_v15_physical_schema()[
                ("table", "agent_project_capabilities")
            ]
            self.assertEqual(
                hashlib.sha256(frozen_v15.encode()).hexdigest(),
                "10ad6d999b9ad849e21d67bab710fb4cef083a37b2e79b8ff53a530c605f8c61",
            )
            self.assertEqual(
                normalized,
                MediaRepository._expected_v18_physical_schema()[
                    ("table", "agent_project_capabilities")
                ],
            )
            self.assertEqual(
                hashlib.sha256(normalized.encode()).hexdigest(),
                "02f0420e06c492e41175691c26c0e5706ca4814ee0213cf6bace8df1482426c2",
            )

        credential = {
            "object": "memolens.agent_project_credential",
            "schema_version": "1",
            "base_url": "http://127.0.0.1:8787",
            "project_id": project_id,
            "pairing_id": "pair_v15_1",
            "capability_id": capability["id"],
            "subject_id": "agent_v15_1",
            "claimed_client_label": "MemoLens preview host 1",
            "proof_secret": secret,
            "database_uuid": self.repository.database_uuid,
            "actions": ["timeline.preview_media"],
            "created_at": capability["issued_at"],
            "expires_at": capability["expires_at"],
            "status": "active",
        }
        with patch.dict(
            os.environ,
            {"MEMOLENS_APP_STATE_DIR": str(self.root / "plugin-state")},
        ):
            saved = plugin_credentials.save_agent_credential(credential)
            loaded = plugin_credentials.load_agent_credential(project_id)
        self.assertEqual(saved["actions"], ["timeline.preview_media"])
        self.assertEqual(loaded["actions"], ["timeline.preview_media"])

        before = self._table_snapshot(self.db_path)
        mutation_called = False

        def forbidden_mutation(_connection: sqlite3.Connection):
            nonlocal mutation_called
            mutation_called = True
            raise AssertionError("preview reached a canonical write mutation")

        request = {}
        with self.assertRaisesRegex(
            ValueError,
            "unsupported_canonical_timeline_command",
        ):
            self.repository.execute_paired_canonical_timeline_command(
                capability_id=str(capability["id"]),
                runtime_authority_epoch="epoch_v15_preview",
                presented_secret_sha256=secret,
                project_id=project_id,
                command_type="timeline.preview_media",
                command_version="1",
                idempotency_key="v15-preview-is-not-write",
                request_value=request,
                request_sha256=hashlib.sha256(canonical_json(request).encode()).hexdigest(),
                mutation=forbidden_mutation,
            )
        self.assertFalse(mutation_called)
        self.assertEqual(self._table_snapshot(self.db_path), before)
        self.assertIn("timeline.preview_media", BACKEND_PAIRING_ACTIONS)
        self.assertIn("timeline.preview_media", plugin_client.PAIRING_ACTIONS)
        self.assertIn("timeline.preview_media", plugin_receipts._CAPABILITY_ACTIONS)
        self.assertNotIn("timeline.preview_media", plugin_receipts._TIMELINE_COMMAND_TYPES)

    def test_populated_v14_migration_preserves_authority_bytes_and_backup(self) -> None:
        assert self.repository is not None
        project_id, _payload = self._materialize_project()
        self._pair(
            project_id,
            actions=["timeline.apply_edit", "timeline.restore_revision"],
        )
        before = self._table_snapshot(self.db_path)
        old_actions = before["agent_project_capabilities"][0][6]
        self.assertEqual(
            old_actions,
            b'["timeline.apply_edit","timeline.restore_revision"]',
        )
        self._close_repository()
        self._downgrade_to_exact_v14(self.db_path)

        self._open_repository()
        self.assertEqual(self._table_snapshot(self.db_path), before)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(MediaRepository._schema_preflight(connection), 20)
            MediaRepository._verify_v14_physical_schema(
                connection,
                allow_v17_activation_trigger=True,
            )
            MediaRepository._verify_v16_physical_schema(connection)
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            plugin_authority.validate_exact_managed_schema_manifest(connection)
            actions_blob = connection.execute(
                "SELECT CAST(actions_json AS BLOB) FROM agent_project_capabilities"
            ).fetchone()[0]
            self.assertEqual(actions_blob, old_actions)
            self.assertNotIn(b"timeline.preview_media", actions_blob)

        backup_dir = self.db_path.parent / "migration-backups"
        backups = sorted(backup_dir.glob("media-v14-to-v15-*.sqlite3"))
        manifests = sorted(backup_dir.glob("media-v14-to-v15-*.manifest.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["from_schema_version"], 14)
        self.assertEqual(manifest["to_schema_version"], 15)
        self.assertEqual(
            hashlib.sha256(backups[0].read_bytes()).hexdigest(),
            manifest["backup_sha256"],
        )
        with closing(sqlite3.connect(backups[0])) as backup:
            backup.row_factory = sqlite3.Row
            plugin_authority.validate_exact_v14_schema_manifest(backup)
            self.assertEqual(
                backup.execute(
                    "SELECT CAST(actions_json AS BLOB) "
                    "FROM agent_project_capabilities"
                ).fetchone()[0],
                old_actions,
            )

    def test_v15_copy_fault_rolls_back_exact_v14_then_recovers(self) -> None:
        project_id, _payload = self._materialize_project()
        self._pair(project_id, actions=["timeline.apply_edit"])
        before = self._table_snapshot(self.db_path)
        self._close_repository()
        self._downgrade_to_exact_v14(self.db_path)
        repository = MediaRepository(self.db_path)
        with patch.object(
            MediaRepository,
            "_assert_v15_projection_equal",
            side_effect=MediaMigrationError("injected_v15_projection_fault"),
        ):
            with self.assertRaisesRegex(
                MediaMigrationError,
                "injected_v15_projection_fault",
            ):
                repository.ensure_schema(self.library)
        repository.close()
        self._assert_exact_v14(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema "
                    "WHERE name='agent_project_capabilities_v14'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=15"
                ).fetchone()[0],
                0,
            )
        retry = MediaRepository(self.db_path)
        retry.ensure_schema(self.library)
        retry.close()
        self.assertEqual(self._table_snapshot(self.db_path), before)
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            plugin_authority.validate_exact_managed_schema_manifest(connection)

    def test_v14_collision_fails_before_backup_or_schema_write(self) -> None:
        self._close_repository()
        self._downgrade_to_exact_v14(self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "CREATE TABLE agent_project_capabilities_v14(marker TEXT)"
            )
        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(
            MediaMigrationError,
            "v15_schema_name_collision:agent_project_capabilities_v14:table",
        ):
            repository.ensure_schema(self.library)
        repository.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (14,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=15"
                ).fetchone()[0],
                0,
            )
        self.assertFalse((self.db_path.parent / "migration-backups").exists())

    def test_future_v21_is_rejected_before_backup_or_write(self) -> None:
        self._close_repository()
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            guard = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='trigger' "
                "AND name='trg_database_meta_no_schema_downgrade'"
            ).fetchone()
            assert guard is not None and guard[0]
            connection.execute("DROP TRIGGER trg_database_meta_no_schema_downgrade")
            connection.execute(
                "UPDATE database_meta SET schema_version=21 WHERE singleton=1"
            )
            connection.execute(str(guard[0]))
        repository = MediaRepository(self.db_path)
        with self.assertRaisesRegex(MediaMigrationError, "future_schema_version:21"):
            repository.ensure_schema(self.library)
        repository.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT schema_version FROM database_meta WHERE singleton=1"
                ).fetchone(),
                (21,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM schema_migrations WHERE version=15"
                ).fetchone()[0],
                1,
            )
        self.assertFalse((self.db_path.parent / "migration-backups").exists())


if __name__ == "__main__":
    unittest.main()
