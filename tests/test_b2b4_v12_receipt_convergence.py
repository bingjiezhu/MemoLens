from __future__ import annotations

from contextlib import closing
import gc
from pathlib import Path
import sqlite3
import tempfile
import unittest
import warnings

from core.db import ImageIndexRepository
from core.media_db import (
    SCHEMA_VERSION,
    V11_CHECKSUM,
    V12_CHECKSUM,
    MediaRepository,
)


class V12ReceiptConvergenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-v12-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _open_fresh(self) -> MediaRepository:
        ImageIndexRepository(self.db_path).ensure_schema()
        repository = MediaRepository(self.db_path)
        repository.ensure_schema(self.library)
        return repository

    def test_current_reopen_preserves_v12_generic_receipt_authority(self) -> None:
        repository = self._open_fresh()
        repository.close()

        for _attempt in range(2):
            reopened = MediaRepository(self.db_path)
            reopened.ensure_schema(self.library)
            reopened.close()
            with closing(sqlite3.connect(self.db_path)) as connection:
                self.assertEqual(SCHEMA_VERSION, 20)
                self.assertEqual(
                    connection.execute(
                        "SELECT schema_version FROM database_meta WHERE singleton=1"
                    ).fetchone(),
                    (SCHEMA_VERSION,),
                )
                self.assertEqual(
                    tuple(
                        connection.execute(
                            "SELECT name,checksum FROM schema_migrations WHERE version=11"
                        ).fetchone()
                    ),
                    ("paired_agent_canonical_timeline_edit", V11_CHECKSUM),
                )
                self.assertEqual(
                    tuple(
                        connection.execute(
                            "SELECT name,checksum FROM schema_migrations WHERE version=12"
                        ).fetchone()
                    ),
                    (
                        "paired_agent_project_command_receipt_convergence",
                        V12_CHECKSUM,
                    ),
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_schema "
                        "WHERE name='agent_blueprint_command_receipts'"
                    ).fetchone()
                )
                columns = tuple(
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(agent_project_command_receipts)"
                    )
                )
                self.assertEqual(
                    columns,
                    (
                        "id",
                        "database_uuid",
                        "authenticated_principal",
                        "capability_id",
                        "paired_subject_id",
                        "claimed_client_label",
                        "project_id",
                        "command_type",
                        "command_version",
                        "idempotency_key",
                        "request_json",
                        "request_sha256",
                        "actor_json",
                        "origin_json",
                        "operation_id",
                        "response_status",
                        "response_json",
                        "response_sha256",
                        "receipt_sha256",
                        "resource_type",
                        "resource_id",
                        "created_at",
                        "receipt_schema_version",
                        "request_capture",
                    ),
                )
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
        self.assertEqual(
            [warning for warning in caught if warning.category is ResourceWarning],
            [],
        )


if __name__ == "__main__":
    unittest.main()
