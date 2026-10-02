from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from core.media_db import MediaRepository


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = (
    REPOSITORY_ROOT / ".agents" / "plugins" / "plugins" / "memolens" / "scripts"
)
sys.path.insert(0, str(PLUGIN_SCRIPTS))

from memolens_blueprint_authority import (  # noqa: E402
    CURRENT_MANAGED_SCHEMA_VERSION,
    _V20_MIGRATIONS,
    _V20_PHYSICAL_SCHEMA_SHA256,
    validate_exact_managed_schema_manifest,
)
from tests.test_blueprint_persistence import (  # noqa: E402
    initialize_fresh,
    initialize_v3,
)


def _inspect_current_database(
    path: Path,
) -> tuple[list[tuple[object, ...]], list[tuple[str, str, str, str]]]:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        validate_exact_managed_schema_manifest(connection)
        MediaRepository._verify_blueprint_full_physical_schema(connection)
        migrations = [
            tuple(row)
            for row in connection.execute(
                "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
            )
        ]
        schema = [
            (
                str(row["type"]),
                str(row["name"]),
                str(row["tbl_name"]),
                " ".join(str(row["sql"] or "").split()),
            )
            for row in connection.execute(
                """SELECT type,name,tbl_name,sql FROM sqlite_schema
                     WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"""
            )
        ]
    return migrations, schema


class B2ANoMigrationTests(unittest.TestCase):
    def test_current_manifest_validates_on_a_read_only_connection(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-b2a-read-only-schema-"
        ) as temporary:
            repository, _library, db_path = initialize_fresh(Path(temporary))
            repository.close()
            uri = f"{db_path.resolve().as_uri()}?mode=ro"
            with closing(sqlite3.connect(uri, uri=True)) as connection, connection:
                connection.row_factory = sqlite3.Row
                validate_exact_managed_schema_manifest(connection)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute(
                        "UPDATE database_meta SET updated_at=updated_at WHERE singleton=1"
                    )

    def test_b2a_adds_no_private_schema_to_exact_managed_v20(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-b2a-schema-oracle-"
        ) as temporary:
            root = Path(temporary)
            _fresh_repository, _fresh_library, fresh_db = initialize_fresh(
                root / "fresh"
            )
            upgraded_repository, upgraded_library, upgraded_db = initialize_v3(
                root / "upgraded"
            )
            upgraded_repository.ensure_schema(upgraded_library)

            fresh_migrations, fresh_schema = _inspect_current_database(fresh_db)
            upgraded_migrations, upgraded_schema = _inspect_current_database(upgraded_db)
            _fresh_repository.close()
            upgraded_repository.close()

        expected_migrations = [tuple(item) for item in _V20_MIGRATIONS]
        self.assertEqual(fresh_migrations, expected_migrations)
        self.assertEqual(upgraded_migrations, expected_migrations)
        self.assertEqual(fresh_migrations[-1][0], CURRENT_MANAGED_SCHEMA_VERSION)
        self.assertEqual(fresh_schema, upgraded_schema)
        self.assertEqual(set(upgraded_schema) - set(fresh_schema), set())
        self.assertEqual(set(fresh_schema) - set(upgraded_schema), set())

        snapshot_json = json.dumps(
            fresh_schema,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        print(
            "B2A_NO_MIGRATION_ORACLE "
            + json.dumps(
                {
                    "fresh_vs_v3_to_v20_missing_object_count": 0,
                    "fresh_vs_v3_to_v20_new_object_count": 0,
                    "managed_schema_version": CURRENT_MANAGED_SCHEMA_VERSION,
                    "manifest_object_count": len(_V20_PHYSICAL_SCHEMA_SHA256),
                    "migration_count": len(fresh_migrations),
                    "normalized_schema_object_count": len(fresh_schema),
                    "normalized_schema_sha256": hashlib.sha256(
                        snapshot_json.encode()
                    ).hexdigest(),
                    "status": "PASS",
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
