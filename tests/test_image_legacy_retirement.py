from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from core.db import (
    IMAGE_INDEX_MANAGED_SCHEMA_ERROR,
    IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR,
    ImageIndexRepository,
)
from core.media_db import MediaRepository, SCHEMA_STATEMENTS, _KNOWN_MIGRATIONS


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-08-29T00:00:00+00:00"


class _AuditedConnection(sqlite3.Connection):
    final_total_changes: int | None = None

    def close(self) -> None:
        self.final_total_changes = self.total_changes
        super().close()


def _seed_image_row(
    connection: sqlite3.Connection,
    *,
    image_id: str,
    sha256: str,
    relative_path: str,
) -> None:
    connection.execute(
        """INSERT INTO image_index(
               id,sha256,filename,relative_path,mime_type,file_size,
               description,tags_json,combined_text,embedding_backend,embedding,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            image_id,
            sha256,
            Path(relative_path).name,
            relative_path,
            "image/png",
            4,
            image_id,
            "[]",
            image_id,
            "legacy",
            b"abcd",
            NOW,
            NOW,
        ),
    )


def _database_files(db_path: Path) -> dict[str, tuple[int, str]]:
    snapshot: dict[str, tuple[int, str]] = {}
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(f"{db_path}{suffix}")
        if candidate.exists():
            payload = candidate.read_bytes()
            snapshot[suffix or "db"] = (
                len(payload),
                hashlib.sha256(payload).hexdigest(),
            )
    return snapshot


class ImageLegacyWriteRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-image-legacy-retirement-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        media = MediaRepository(self.db_path)
        try:
            media.ensure_schema(self.library)
        finally:
            media.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_managed_ensure_schema_is_exactly_read_pure(self) -> None:
        before = _database_files(self.db_path)
        audited = sqlite3.connect(
            self.db_path,
            factory=_AuditedConnection,
        )
        audited.row_factory = sqlite3.Row
        actions: list[int] = []

        def authorize(
            action: int,
            _arg1: str | None,
            _arg2: str | None,
            _db_name: str | None,
            _trigger_name: str | None,
        ) -> int:
            actions.append(action)
            return sqlite3.SQLITE_OK

        audited.set_authorizer(authorize)
        repository = ImageIndexRepository(self.db_path)
        with mock.patch.object(repository, "_connect", return_value=audited):
            repository.ensure_schema()

        write_actions = {
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_ANALYZE,
            sqlite3.SQLITE_ATTACH,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_INDEX,
            sqlite3.SQLITE_CREATE_TEMP_TABLE,
            sqlite3.SQLITE_CREATE_TEMP_TRIGGER,
            sqlite3.SQLITE_CREATE_TEMP_VIEW,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_CREATE_VIEW,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_DETACH,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_INDEX,
            sqlite3.SQLITE_DROP_TEMP_TABLE,
            sqlite3.SQLITE_DROP_TEMP_TRIGGER,
            sqlite3.SQLITE_DROP_TEMP_VIEW,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_DROP_VIEW,
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_REINDEX,
            sqlite3.SQLITE_UPDATE,
        }
        self.assertEqual(audited.final_total_changes, 0)
        self.assertTrue(actions)
        self.assertEqual([action for action in actions if action in write_actions], [])
        self.assertEqual(_database_files(self.db_path), before)

    def test_managed_schema_damage_fails_without_deduplicate_or_repair(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DROP INDEX idx_image_index_relative_path")
            connection.execute(
                "CREATE INDEX idx_image_index_relative_path ON image_index(relative_path)"
            )
            _seed_image_row(
                connection,
                image_id="orphan-a",
                sha256="a" * 64,
                relative_path="duplicate.png",
            )
            _seed_image_row(
                connection,
                image_id="orphan-b",
                sha256="b" * 64,
                relative_path="duplicate.png",
            )

        before = _database_files(self.db_path)
        audited = sqlite3.connect(
            self.db_path,
            factory=_AuditedConnection,
        )
        audited.row_factory = sqlite3.Row
        repository = ImageIndexRepository(self.db_path)
        with self.assertRaisesRegex(
            RuntimeError,
            f"{IMAGE_INDEX_MANAGED_SCHEMA_ERROR}:image_index",
        ), mock.patch.object(repository, "_connect", return_value=audited):
            repository.ensure_schema()

        with sqlite3.connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT id FROM image_index ORDER BY id"
            ).fetchall()
            index = next(
                row
                for row in connection.execute("PRAGMA index_list(image_index)")
                if row[1] == "idx_image_index_relative_path"
            )
        self.assertEqual(rows, [("orphan-a",), ("orphan-b",)])
        self.assertEqual(index[2], 0)
        self.assertEqual(audited.final_total_changes, 0)
        after = _database_files(self.db_path)
        self.assertEqual(after["db"], before["db"])
        self.assertEqual(after.get("-wal"), before.get("-wal"))
        # WAL shared-memory lock bytes may change when a read connection opens;
        # its size remains stable and durable DB/WAL bytes remain identical.
        self.assertEqual(after.get("-shm", (None,))[0], before.get("-shm", (None,))[0])

    def test_structurally_valid_pre_v14_owner_can_run_legacy_repair(self) -> None:
        legacy_db = self.root / "pre-v14.db"
        repository = ImageIndexRepository(legacy_db)
        repository.ensure_schema()
        with sqlite3.connect(legacy_db) as connection:
            connection.execute("DROP INDEX idx_image_index_relative_path")
            connection.execute(
                "CREATE INDEX idx_image_index_relative_path ON image_index(relative_path)"
            )
            _seed_image_row(
                connection,
                image_id="legacy-old",
                sha256="c" * 64,
                relative_path="duplicate.png",
            )
            _seed_image_row(
                connection,
                image_id="legacy-new",
                sha256="d" * 64,
                relative_path="duplicate.png",
            )
            for statement in SCHEMA_STATEMENTS[:2]:
                connection.execute(statement)
            for version in range(1, 14):
                name, checksum = _KNOWN_MIGRATIONS[version]
                connection.execute(
                    """INSERT INTO schema_migrations(version,name,checksum,applied_at)
                       VALUES(?,?,?,?)""",
                    (version, name, checksum, NOW),
                )
            connection.execute(
                """INSERT INTO database_meta(
                       singleton,database_uuid,schema_version,created_at,updated_at)
                   VALUES(1,?,13,?,?)""",
                ("database-pre-v14", NOW, NOW),
            )

        repository.ensure_schema()
        with sqlite3.connect(legacy_db) as connection:
            rows = connection.execute(
                "SELECT id FROM image_index ORDER BY id"
            ).fetchall()
            index = next(
                row
                for row in connection.execute("PRAGMA index_list(image_index)")
                if row[1] == "idx_image_index_relative_path"
            )
        self.assertEqual(rows, [("legacy-new",)])
        self.assertEqual(index[2], 1)

    def test_cutover_race_cannot_cross_guarded_legacy_transaction(self) -> None:
        legacy_db = self.root / "cutover-race.db"
        repository = ImageIndexRepository(legacy_db)
        repository.ensure_schema()
        with sqlite3.connect(legacy_db) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            _seed_image_row(
                connection,
                image_id="legacy-race",
                sha256="f" * 64,
                relative_path="race.png",
            )
            for statement in SCHEMA_STATEMENTS[:2]:
                connection.execute(statement)
            for version in range(1, 14):
                name, checksum = _KNOWN_MIGRATIONS[version]
                connection.execute(
                    """INSERT INTO schema_migrations(version,name,checksum,applied_at)
                       VALUES(?,?,?,?)""",
                    (version, name, checksum, NOW),
                )
            connection.execute(
                """INSERT INTO database_meta(
                       singleton,database_uuid,schema_version,created_at,updated_at)
                   VALUES(1,?,13,?,?)""",
                ("database-cutover-race", NOW, NOW),
            )

        original_guard = ImageIndexRepository._require_legacy_mutation_allowed

        def cut_over_after_guard(connection: sqlite3.Connection) -> None:
            original_guard(connection)
            name, checksum = _KNOWN_MIGRATIONS[14]
            with sqlite3.connect(legacy_db, timeout=0) as concurrent:
                concurrent.execute(
                    """INSERT INTO schema_migrations(version,name,checksum,applied_at)
                       VALUES(?,?,?,?)""",
                    (14, name, checksum, NOW),
                )
                concurrent.execute(
                    "UPDATE database_meta SET schema_version=14,updated_at=? WHERE singleton=1",
                    (NOW,),
                )

        with mock.patch.object(
            ImageIndexRepository,
            "_require_legacy_mutation_allowed",
            side_effect=cut_over_after_guard,
        ), self.assertRaises(sqlite3.OperationalError):
            repository.update_image_quality(
                image_id="legacy-race",
                aesthetic_score=0.9,
                aesthetic_model="legacy-race",
                technical_quality_score=0.9,
                aesthetic_updated_at=NOW,
            )

        with sqlite3.connect(legacy_db) as connection:
            row = connection.execute(
                "SELECT aesthetic_score,aesthetic_model FROM image_index WHERE id='legacy-race'"
            ).fetchone()
            version = connection.execute(
                "SELECT schema_version FROM database_meta WHERE singleton=1"
            ).fetchone()
        self.assertEqual(row, (None, None))
        self.assertEqual(version, (13,))

    def test_managed_backfill_commands_fail_before_model_or_row_update(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            _seed_image_row(
                connection,
                image_id="managed-orphan",
                sha256="e" * 64,
                relative_path="managed.png",
            )
            before = connection.execute(
                """SELECT aesthetic_score,aesthetic_model,
                          combined_text,text_embedding_model,
                          combined_text_embedding,updated_at
                     FROM image_index WHERE id='managed-orphan'"""
            ).fetchone()

        environment = os.environ.copy()
        environment.update(
            {
                "APP_CONFIG_PATH": str(PROJECT_ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "app-state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "PYTHONWARNINGS": "error",
            }
        )
        commands = (
            (
                "quality",
                "scripts/backfill_image_quality.py",
                ["--db-path", str(self.db_path), "--image-dir", str(self.library)],
            ),
            (
                "text",
                "scripts/backfill_text_embeddings.py",
                ["--db-path", str(self.db_path)],
            ),
        )
        for name, script, arguments in commands:
            process = subprocess.run(
                ["bash", "scripts/run_python.sh", script, *arguments],
                cwd=PROJECT_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            with self.subTest(backfill=name):
                self.assertEqual(process.returncode, 78, process.stderr)
                self.assertIn(IMAGE_INDEX_PROJECTOR_AUTHORITY_ERROR, process.stderr)
                self.assertIn("canonical image projector", process.stderr)

        with sqlite3.connect(self.db_path) as connection:
            after = connection.execute(
                """SELECT aesthetic_score,aesthetic_model,
                          combined_text,text_embedding_model,
                          combined_text_embedding,updated_at
                     FROM image_index WHERE id='managed-orphan'"""
            ).fetchone()
        self.assertEqual(after, before)

    def test_production_raw_writers_are_projector_or_guarded_legacy_owner(self) -> None:
        raw_image_index_write = re.compile(
            r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|UPDATE|DELETE\s+FROM)"
            r"\s+image_index\b",
            re.IGNORECASE,
        )
        scripts = (
            PROJECT_ROOT / "scripts" / "backfill_image_quality.py",
            PROJECT_ROOT / "scripts" / "backfill_text_embeddings.py",
        )
        for script in scripts:
            content = script.read_text(encoding="utf-8")
            with self.subTest(script=script.name):
                guard = content.index("require_legacy_mutation_authority()")
                schema = content.index("repository.ensure_schema()")
                self.assertLess(guard, schema)
                self.assertIsNone(raw_image_index_write.search(content))

        production_writers = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for root in ("core", "backend", "indexing", "scripts")
            for path in (PROJECT_ROOT / root).rglob("*.py")
            if raw_image_index_write.search(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            production_writers,
            {"core/db.py", "core/image_analysis_persistence.py"},
        )


if __name__ == "__main__":
    unittest.main()
