from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    ImageProjectionRenderError,
)
from tests import test_image_bridge_fault_matrix as bridge_fault_matrix
from tests import test_image_projection_schema_guards as schema_guards
from tests import test_image_projection_verified_read as verified_read


_PROJECTION_TABLES = (
    "image_projection_generations",
    "image_projection_rows",
    "image_projection_aliases",
    "image_projection_manifests",
    "image_projection_read_manifests",
    "image_projection_receipts",
    "image_index",
)


def _normalized(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes_hex": value.hex()}
    return value


class _InjectedRebuildFault(RuntimeError):
    pass


class _FaultingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        target: str,
        phase: str,
    ) -> None:
        self._connection = connection
        self._target = target
        self._phase = phase
        self.hits = 0

    @staticmethod
    def _normalized_sql(statement: str) -> str:
        return " ".join(statement.split()).upper()

    def _matches(self, statement: str) -> bool:
        normalized = self._normalized_sql(statement)
        if self._target == "manifest":
            return normalized.startswith("INSERT INTO IMAGE_PROJECTION_MANIFESTS")
        if self._target == "read_manifest":
            return normalized.startswith(
                "INSERT INTO IMAGE_PROJECTION_READ_MANIFESTS"
            )
        if self._target == "materialize":
            return normalized.startswith("INSERT INTO IMAGE_INDEX")
        if self._target == "physical_clear":
            return normalized == "DELETE FROM IMAGE_INDEX WHERE ID=?"
        if self._target == "deactivate":
            return (
                normalized.startswith("UPDATE IMAGE_PROJECTION_GENERATIONS")
                and "SET IS_ACTIVE=0" in normalized
                and "WHERE ID=?" in normalized
            )
        if self._target == "activate":
            return (
                normalized.startswith("UPDATE IMAGE_PROJECTION_GENERATIONS")
                and "SET IS_ACTIVE=1" in normalized
                and "WHERE ID=?" in normalized
            )
        return False

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        matches = self._matches(statement)
        if matches and self._phase == "before":
            self.hits += 1
            raise _InjectedRebuildFault(f"{self._target}:before")
        cursor = self._connection.execute(statement, parameters)
        if matches and self._phase == "after":
            self.hits += 1
            raise _InjectedRebuildFault(f"{self._target}:after")
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class SameHighWaterProjectionRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.first = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self.second_asset_id, second_job_id = (
            verified_read.ImageProjectionVerifiedReadTests._publish_second_asset(
                self
            )
        )
        self.second = self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self.old_generation_id = str(self.second["generation_id"])
        with self.fixture.repository.transaction() as connection:
            high_water = connection.execute(
                "SELECT MAX(position) FROM image_projection_changes"
            ).fetchone()
        assert high_water is not None
        self.high_water = int(high_water[0])

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _rebuild(self, nonce: str) -> dict[str, object]:
        return self.fixture.repository.rebuild_image_projection_generation(
            fixed_high_water=self.high_water,
            expected_alias_set_sha256=self.fixture.alias_set_sha256,
            rebuild_nonce=nonce,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
        )

    def _projection_snapshot(self) -> str:
        with self.fixture.repository.transaction() as connection:
            snapshot: dict[str, object] = {}
            for table in _PROJECTION_TABLES:
                rows = connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                snapshot[table] = [
                    [_normalized(value) for value in tuple(row)] for row in rows
                ]
        return json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _canonical_snapshot(self) -> str:
        queries = {
            "results": """SELECT job_id,asset_id,analysis_run_id,revision,
                                  content_sha256,source_id,result_json
                             FROM image_analysis_results
                            ORDER BY asset_id,revision""",
            "heads": """SELECT asset_id,analysis_run_id,revision,content_sha256
                           FROM image_analysis_heads ORDER BY asset_id""",
            "changes": """SELECT position,database_uuid,asset_id,analysis_run_id,
                                    revision,content_sha256,source_id,
                                    source_binding_sha256,operation,change_json,
                                    change_sha256
                               FROM image_projection_changes ORDER BY position""",
            "publish_receipts": """SELECT job_id,database_uuid,asset_id,
                                             analysis_run_id,revision,content_sha256,
                                             change_position,publish_receipt_json,
                                             publish_receipt_sha256
                                        FROM image_publish_receipts ORDER BY job_id""",
            "artifacts": """SELECT asset_id,analysis_run_id,stage,name,
                                     artifact_sha256,size_bytes,artifact_blob
                                FROM image_analysis_artifacts
                               ORDER BY asset_id,analysis_run_id,stage,name""",
            "sources": """SELECT id,asset_id,library_root_id,relative_path,
                                   display_filename,observed_size,
                                   observed_mtime_ns,source_file_id,availability,
                                   is_preferred,last_verified_at
                              FROM asset_sources ORDER BY id""",
            "roots": """SELECT id,canonical_path,label,
                                 permission_fingerprint,status
                            FROM library_roots ORDER BY id""",
        }
        with self.fixture.repository.transaction() as connection:
            snapshot = {
                name: [
                    [_normalized(value) for value in tuple(row)]
                    for row in connection.execute(query).fetchall()
                ]
                for name, query in queries.items()
            }
        return json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _generation_bytes(
        self,
        generation_id: str,
    ) -> tuple[bytes, bytes, bytes]:
        with self.fixture.repository.transaction() as connection:
            manifest = connection.execute(
                """SELECT manifest_json FROM image_projection_manifests
                    WHERE generation_id=?""",
                (generation_id,),
            ).fetchone()
            rows = connection.execute(
                """SELECT asset_id,row_json,row_sha256,alias_set_sha256
                     FROM image_projection_rows
                    WHERE generation_id=? ORDER BY asset_id""",
                (generation_id,),
            ).fetchall()
            aliases = connection.execute(
                """SELECT legacy_id,canonical_asset_id,library_root_id,source_id,
                          alias_scope,alias_sha256
                     FROM image_projection_aliases
                    WHERE generation_id=? ORDER BY legacy_id""",
                (generation_id,),
            ).fetchall()
        assert manifest is not None
        return (
            str(manifest["manifest_json"]).encode("utf-8"),
            json.dumps(
                [list(row) for row in rows],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            json.dumps(
                [list(row) for row in aliases],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8"),
        )

    def _physical_bytes(self) -> str:
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM image_index ORDER BY id"
            ).fetchall()
        return json.dumps(
            [[_normalized(value) for value in tuple(row)] for row in rows],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _physical_semantic_bytes(self) -> str:
        columns = tuple(
            column
            for column in IMAGE_INDEX_SQL_COLUMNS
            if column not in {"created_at", "updated_at"}
        )
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                f"SELECT {','.join(columns)} FROM image_index ORDER BY id"
            ).fetchall()
        return json.dumps(
            [[_normalized(value) for value in tuple(row)] for row in rows],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _physical_envelopes(self) -> list[tuple[str, str, str]]:
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT id,created_at,updated_at
                     FROM image_index ORDER BY id"""
            ).fetchall()
        return [
            (str(row["id"]), str(row["created_at"]), str(row["updated_at"]))
            for row in rows
        ]

    def _generation_created_at(self, generation_id: str) -> str:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                """SELECT created_at FROM image_projection_generations
                    WHERE id=?""",
                (generation_id,),
            ).fetchone()
        assert row is not None
        return str(row["created_at"])

    def _delete_physical_projection(self) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM image_index")

    def _insert_unmanaged_physical_row(self) -> None:
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            source = connection.execute(
                f"SELECT {columns} FROM image_index WHERE id=?",
                (self.fixture.asset_id,),
            ).fetchone()
            assert source is not None
            orphan = dict(source)
            orphan.update(
                {
                    "id": "orphan_legacy_row",
                    "sha256": hashlib.sha256(b"orphan legacy row").hexdigest(),
                    "filename": "orphan.jpg",
                    "relative_path": "orphan/orphan.jpg",
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                tuple(orphan[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )

    def _insert_unprojected_canonical_asset_physical_row(self) -> str:
        asset_sha256 = hashlib.sha256(
            b"unprojected canonical image asset"
        ).hexdigest()
        asset_id = f"asset_{asset_sha256[:24]}"
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            source = connection.execute(
                f"SELECT {columns} FROM image_index WHERE id=?",
                (self.fixture.asset_id,),
            ).fetchone()
            assert source is not None
            connection.execute(
                """INSERT INTO assets(
                       id,kind,sha256,mime_type,file_size,duration_ms,width,height,
                       rotation_degrees,codec_json,captured_at,probe_status,
                       error_code,created_at,updated_at)
                   VALUES(?,'image',?,?,?,NULL,?,?,NULL,'{}',NULL,'ready',NULL,?,?)""",
                (
                    asset_id,
                    asset_sha256,
                    source["mime_type"],
                    source["file_size"],
                    source["width"],
                    source["height"],
                    source["created_at"],
                    source["updated_at"],
                ),
            )
            physical = dict(source)
            physical.update(
                {
                    "id": asset_id,
                    "sha256": asset_sha256,
                    "filename": "unprojected-canonical.jpg",
                    "relative_path": "unprojected/unprojected-canonical.jpg",
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                tuple(physical[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )
        return asset_id

    def _assert_only_generation_is_active(self, generation_id: str) -> None:
        with self.fixture.repository.transaction() as connection:
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 ORDER BY id"""
            ).fetchall()
        self.assertEqual([str(row["id"]) for row in active], [generation_id])

    def test_same_high_water_successor_is_semantically_identical_and_canonical_read_only(
        self,
    ) -> None:
        canonical_before = self._canonical_snapshot()
        projection_receipts_before = None
        with self.fixture.repository.transaction() as connection:
            projection_receipts_before = connection.execute(
                """SELECT change_position,generation_id,receipt_json,receipt_sha256
                     FROM image_projection_receipts ORDER BY change_position"""
            ).fetchall()
        old_generation_bytes = self._generation_bytes(self.old_generation_id)
        physical_semantics_before = self._physical_semantic_bytes()

        rebuilt = self._rebuild("same-high-water-successor")

        successor_id = str(rebuilt["generation_id"])
        self.assertNotEqual(successor_id, self.old_generation_id)
        self.assertEqual(rebuilt["fixed_high_water"], self.high_water)
        self.assertIs(rebuilt["replayed"], False)
        self.assertIs(rebuilt["is_active"], True)
        self._assert_only_generation_is_active(successor_id)
        self.assertEqual(
            self._generation_bytes(successor_id),
            old_generation_bytes,
        )
        self.assertEqual(
            self._physical_semantic_bytes(),
            physical_semantics_before,
        )
        successor_created_at = self._generation_created_at(successor_id)
        self.assertEqual(
            self._physical_envelopes(),
            sorted(
                [
                    (
                        self.fixture.asset_id,
                        successor_created_at,
                        successor_created_at,
                    ),
                    (
                        self.second_asset_id,
                        successor_created_at,
                        successor_created_at,
                    ),
                ]
            ),
        )
        self.assertEqual(self._canonical_snapshot(), canonical_before)
        with self.fixture.repository.transaction() as connection:
            projection_receipts_after = connection.execute(
                """SELECT change_position,generation_id,receipt_json,receipt_sha256
                     FROM image_projection_receipts ORDER BY change_position"""
            ).fetchall()
            successor_receipts = connection.execute(
                """SELECT COUNT(*) FROM image_projection_receipts
                    WHERE generation_id=?""",
                (successor_id,),
            ).fetchone()
        self.assertEqual(
            [tuple(row) for row in projection_receipts_after],
            [tuple(row) for row in projection_receipts_before],
        )
        assert successor_receipts is not None
        self.assertEqual(int(successor_receipts[0]), 0)
        expected_processing_generations = {
            self.fixture.asset_id: str(self.first["generation_id"]),
            self.second_asset_id: self.old_generation_id,
        }
        for asset_id in (self.fixture.asset_id, self.second_asset_id):
            current = self.fixture.repository.get_current_image_analysis(asset_id)
            assert current is not None
            self.assertEqual(current["projection"]["status"], "current")
            self.assertEqual(current["projection"]["generation_id"], successor_id)
            self.assertEqual(
                current["projection"]["processing_generation_id"],
                expected_processing_generations[asset_id],
            )

    def test_deleted_physical_projection_is_rebuilt_only_from_canonical_state(
        self,
    ) -> None:
        canonical_before = self._canonical_snapshot()
        immutable_generation_before = self._generation_bytes(
            self.old_generation_id
        )
        physical_semantics_before = self._physical_semantic_bytes()
        with self.fixture.repository.transaction() as connection:
            receipts_before = connection.execute(
                """SELECT change_position,generation_id,receipt_json,receipt_sha256
                     FROM image_projection_receipts ORDER BY change_position"""
            ).fetchall()

        self._delete_physical_projection()
        self.assertEqual(self._physical_bytes(), "[]")

        rebuilt = self._rebuild("rebuild-after-physical-delete")

        successor_id = str(rebuilt["generation_id"])
        self.assertEqual(
            self._physical_semantic_bytes(),
            physical_semantics_before,
        )
        self.assertEqual(self._canonical_snapshot(), canonical_before)
        self.assertEqual(
            self._generation_bytes(self.old_generation_id),
            immutable_generation_before,
        )
        with self.fixture.repository.transaction() as connection:
            receipts_after = connection.execute(
                """SELECT change_position,generation_id,receipt_json,receipt_sha256
                     FROM image_projection_receipts ORDER BY change_position"""
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in receipts_after],
            [tuple(row) for row in receipts_before],
        )
        self._assert_only_generation_is_active(successor_id)
        for asset_id in (self.fixture.asset_id, self.second_asset_id):
            current = self.fixture.repository.get_current_image_analysis(asset_id)
            assert current is not None
            self.assertEqual(current["projection"]["status"], "current")
            self.assertEqual(current["projection"]["generation_id"], successor_id)

    def test_tampered_physical_projection_is_replaced_not_copied(self) -> None:
        physical_semantics_before = self._physical_semantic_bytes()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description=? WHERE id=?",
                ("tampered derived description", self.fixture.asset_id),
            )
        self.assertNotEqual(
            self._physical_semantic_bytes(),
            physical_semantics_before,
        )

        rebuilt = self._rebuild("rebuild-after-physical-tamper")

        self.assertEqual(
            self._physical_semantic_bytes(),
            physical_semantics_before,
        )
        self._assert_only_generation_is_active(str(rebuilt["generation_id"]))

    def test_unmanaged_legacy_row_is_preserved_by_scoped_rematerialization(
        self,
    ) -> None:
        canonical_before = self._canonical_snapshot()
        self._insert_unmanaged_physical_row()
        unprojected_asset_id = (
            self._insert_unprojected_canonical_asset_physical_row()
        )
        with self.fixture.repository.transaction() as connection:
            unmanaged_before = connection.execute(
                "SELECT * FROM image_index WHERE id='orphan_legacy_row'"
            ).fetchone()
            unprojected_before = connection.execute(
                "SELECT * FROM image_index WHERE id=?",
                (unprojected_asset_id,),
            ).fetchone()
        assert unmanaged_before is not None
        assert unprojected_before is not None

        rebuilt = self._rebuild("preserve-unmanaged-during-rematerialization")

        with self.fixture.repository.transaction() as connection:
            unmanaged_after = connection.execute(
                "SELECT * FROM image_index WHERE id='orphan_legacy_row'"
            ).fetchone()
            unprojected_after = connection.execute(
                "SELECT * FROM image_index WHERE id=?",
                (unprojected_asset_id,),
            ).fetchone()
        assert unmanaged_after is not None
        assert unprojected_after is not None
        self.assertEqual(tuple(unmanaged_after), tuple(unmanaged_before))
        self.assertEqual(tuple(unprojected_after), tuple(unprojected_before))
        self.assertEqual(self._canonical_snapshot(), canonical_before)
        self.assertIs(rebuilt["is_active"], False)
        self.assertEqual(rebuilt["manifest"]["counts"]["unexpected"], 2)
        self._assert_only_generation_is_active(self.old_generation_id)

    def test_rebuild_fails_closed_when_canonical_renderer_cannot_render(self) -> None:
        before = self._projection_snapshot()
        with patch(
            "core.image_analysis_persistence.render_projected_image_index_row",
            side_effect=ImageProjectionRenderError(
                "image_projection_artifact_set_incomplete"
            ),
        ):
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "image_analysis_projection_rebuild_render_failed",
            ):
                self._rebuild("canonical-render-failure")
        self.assertEqual(self._projection_snapshot(), before)

    def test_active_nonce_replay_repairs_missing_physical_projection(self) -> None:
        first = self._rebuild("active-replay-repair")
        physical_semantics = self._physical_semantic_bytes()
        self._delete_physical_projection()

        replay = self._rebuild("active-replay-repair")

        self.assertIs(replay["replayed"], True)
        self.assertEqual(replay["generation_id"], first["generation_id"])
        self.assertIs(replay["is_active"], True)
        self.assertEqual(self._physical_semantic_bytes(), physical_semantics)
        created_at = self._generation_created_at(str(first["generation_id"]))
        self.assertEqual(
            self._physical_envelopes(),
            sorted(
                [
                    (self.fixture.asset_id, created_at, created_at),
                    (self.second_asset_id, created_at, created_at),
                ]
            ),
        )

    def test_same_nonce_replays_one_verified_generation_for_100_rounds(self) -> None:
        first = self._rebuild("stable-rebuild-nonce")
        snapshot = self._projection_snapshot()

        for _round in range(100):
            replay = self._rebuild("stable-rebuild-nonce")
            self.assertIs(replay["replayed"], True)
            self.assertEqual(replay["generation_id"], first["generation_id"])
            self.assertEqual(replay["manifest"], first["manifest"])
            self.assertEqual(self._projection_snapshot(), snapshot)

    def test_different_nonce_creates_another_same_high_water_successor(self) -> None:
        first = self._rebuild("successor-nonce-one")
        second = self._rebuild("successor-nonce-two")

        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertEqual(first["manifest"], second["manifest"])
        self.assertIs(second["replayed"], False)
        self._assert_only_generation_is_active(str(second["generation_id"]))

        replay_first = self._rebuild("successor-nonce-one")
        self.assertIs(replay_first["replayed"], True)
        self.assertEqual(replay_first["generation_id"], first["generation_id"])
        self.assertEqual(
            replay_first["previous_generation_id"],
            first["previous_generation_id"],
        )
        self.assertIs(replay_first["is_active"], False)
        self._assert_only_generation_is_active(str(second["generation_id"]))

    def test_inactive_nonce_replay_repairs_current_physical_without_reactivation(
        self,
    ) -> None:
        inactive = self._rebuild("inactive-replay-one")
        active = self._rebuild("inactive-replay-two")
        physical_semantics = self._physical_semantic_bytes()
        self._delete_physical_projection()

        replay = self._rebuild("inactive-replay-one")

        self.assertIs(replay["replayed"], True)
        self.assertEqual(replay["generation_id"], inactive["generation_id"])
        self.assertIs(replay["is_active"], False)
        self._assert_only_generation_is_active(str(active["generation_id"]))
        self.assertEqual(self._physical_semantic_bytes(), physical_semantics)
        active_created_at = self._generation_created_at(
            str(active["generation_id"])
        )
        self.assertEqual(
            self._physical_envelopes(),
            sorted(
                [
                    (
                        self.fixture.asset_id,
                        active_created_at,
                        active_created_at,
                    ),
                    (
                        self.second_asset_id,
                        active_created_at,
                        active_created_at,
                    ),
                ]
            ),
        )

    def test_runtime_generation_is_bound_into_rebuild_identity(self) -> None:
        first = self._rebuild("runtime-bound-rebuild")
        alternate_runtime = f"runtime_generation_{'b' * 64}"

        second = self.fixture.repository.rebuild_image_projection_generation(
            fixed_high_water=self.high_water,
            expected_alias_set_sha256=self.fixture.alias_set_sha256,
            rebuild_nonce="runtime-bound-rebuild",
            runtime_generation=alternate_runtime,
        )

        self.assertIs(second["replayed"], False)
        self.assertNotEqual(first["generation_id"], second["generation_id"])
        self.assertNotEqual(
            first["runtime_generation_sha256"],
            second["runtime_generation_sha256"],
        )
        self._assert_only_generation_is_active(str(second["generation_id"]))

        replay_first = self._rebuild("runtime-bound-rebuild")
        self.assertIs(replay_first["replayed"], True)
        self.assertEqual(replay_first["generation_id"], first["generation_id"])
        self.assertIs(replay_first["is_active"], False)
        self._assert_only_generation_is_active(str(second["generation_id"]))

    def test_managed_alias_orphan_is_removed_while_unmanaged_legacy_is_preserved(
        self,
    ) -> None:
        alias_fixture = bridge_fault_matrix.ImageProjectionFaultMatrixTests(
            methodName="runTest"
        )
        alias_fixture.setUp()
        try:
            projected = alias_fixture.repository.project_image_analysis_change(
                job_id=alias_fixture.second_job_id,
                runtime_generation=schema_guards.RUNTIME_GENERATION,
                expected_attempt=1,
            )
            old_generation_id = str(projected["generation_id"])
            with alias_fixture.repository.transaction() as connection:
                high_water = int(
                    connection.execute(
                        "SELECT MAX(position) FROM image_projection_changes"
                    ).fetchone()[0]
                )
                alias_count, alias_set_sha256 = (
                    alias_fixture.repository._image_alias_set_in_transaction(
                        connection,
                        alias_fixture.repository.database_uuid,
                    )
                )
                old_aliases = connection.execute(
                    """SELECT legacy_id,canonical_asset_id,library_root_id,
                              source_id,alias_scope,alias_sha256
                         FROM image_projection_aliases
                        WHERE generation_id=? ORDER BY legacy_id""",
                    (old_generation_id,),
                ).fetchall()
            columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
            placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
            with alias_fixture.repository.transaction(
                immediate=True
            ) as connection:
                source = connection.execute(
                    f"SELECT {columns} FROM image_index WHERE id=?",
                    (alias_fixture.fixture.asset_id,),
                ).fetchone()
                assert source is not None
                for row_id, relative_path in (
                    (
                        "fault-matrix-legacy-alias",
                        "managed-alias/legacy.jpg",
                    ),
                    ("unmanaged-legacy-row", "unmanaged/legacy.jpg"),
                ):
                    physical = dict(source)
                    physical.update(
                        {
                            "id": row_id,
                            "sha256": hashlib.sha256(
                                row_id.encode("ascii")
                            ).hexdigest(),
                            "filename": "legacy.jpg",
                            "relative_path": relative_path,
                        }
                    )
                    connection.execute(
                        f"INSERT INTO image_index({columns}) "
                        f"VALUES({placeholders})",
                        tuple(
                            physical[column]
                            for column in IMAGE_INDEX_SQL_COLUMNS
                        ),
                    )
                unmanaged_before = connection.execute(
                    """SELECT * FROM image_index
                        WHERE id='unmanaged-legacy-row'"""
                ).fetchone()
                assert unmanaged_before is not None
            self.assertEqual(alias_count, 1)

            rebuilt = alias_fixture.repository.rebuild_image_projection_generation(
                fixed_high_water=high_water,
                expected_alias_set_sha256=alias_set_sha256,
                rebuild_nonce="nonempty-alias-rebuild",
                runtime_generation=schema_guards.RUNTIME_GENERATION,
            )

            with alias_fixture.repository.transaction() as connection:
                successor_aliases = connection.execute(
                    """SELECT legacy_id,canonical_asset_id,library_root_id,
                              source_id,alias_scope,alias_sha256
                         FROM image_projection_aliases
                        WHERE generation_id=? ORDER BY legacy_id""",
                    (rebuilt["generation_id"],),
                ).fetchall()
                physical_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        "SELECT id FROM image_index ORDER BY id"
                    ).fetchall()
                ]
                unmanaged_after = connection.execute(
                    """SELECT * FROM image_index
                        WHERE id='unmanaged-legacy-row'"""
                ).fetchone()
            self.assertEqual(
                [tuple(row) for row in successor_aliases],
                [tuple(row) for row in old_aliases],
            )
            self.assertEqual(
                physical_ids,
                sorted(
                    [
                        alias_fixture.fixture.asset_id,
                        "unmanaged-legacy-row",
                    ]
                ),
            )
            assert unmanaged_after is not None
            self.assertEqual(tuple(unmanaged_after), tuple(unmanaged_before))
        finally:
            alias_fixture.tearDown()

    def test_lower_and_future_high_water_are_rejected_without_writes(self) -> None:
        before = self._projection_snapshot()
        for high_water, reason in (
            (self.high_water - 1, "image_projection_rebuild_high_water_stale"),
            (self.high_water + 1, "image_projection_rebuild_high_water_future"),
        ):
            with self.subTest(high_water=high_water):
                with self.assertRaisesRegex(
                    ImageAnalysisPersistenceError,
                    reason,
                ):
                    self.fixture.repository.rebuild_image_projection_generation(
                        fixed_high_water=high_water,
                        expected_alias_set_sha256=self.fixture.alias_set_sha256,
                        rebuild_nonce=f"rejected-high-water-{high_water}",
                        runtime_generation=schema_guards.RUNTIME_GENERATION,
                    )
                self.assertEqual(self._projection_snapshot(), before)

    def test_rebuild_inputs_are_closed_and_rejected_without_writes(self) -> None:
        before = self._projection_snapshot()
        valid = {
            "fixed_high_water": self.high_water,
            "expected_alias_set_sha256": self.fixture.alias_set_sha256,
            "rebuild_nonce": "closed-rebuild-input",
            "runtime_generation": schema_guards.RUNTIME_GENERATION,
        }
        cases = (
            (
                {"fixed_high_water": True},
                "image_projection_rebuild_high_water_invalid",
            ),
            (
                {"expected_alias_set_sha256": "bad"},
                "image_projection_rebuild_digest_invalid",
            ),
            (
                {"expected_alias_set_sha256": "0" * 64},
                "image_projection_rebuild_alias_set_changed",
            ),
            (
                {"rebuild_nonce": "bad nonce"},
                "image_projection_rebuild_nonce_invalid",
            ),
            (
                {"runtime_generation": "runtime_generation_bad"},
                "image_projection_rebuild_runtime_generation_invalid",
            ),
            (
                {"projector_version": "bad projector"},
                "image_projection_rebuild_token_invalid",
            ),
            (
                {"compiler_id": "bad compiler"},
                "image_projection_rebuild_token_invalid",
            ),
        )
        for replacement, reason in cases:
            with self.subTest(replacement=replacement):
                arguments = {**valid, **replacement}
                with self.assertRaisesRegex(
                    ImageAnalysisPersistenceError,
                    reason,
                ):
                    self.fixture.repository.rebuild_image_projection_generation(
                        **arguments
                    )
                self.assertEqual(self._projection_snapshot(), before)

    def test_each_rebuild_write_boundary_rolls_back_to_old_active_and_physical(
        self,
    ) -> None:
        baseline = self._projection_snapshot()
        physical = self._physical_bytes()
        repository = self.fixture.repository
        real_transaction = repository.transaction
        for target in (
            "manifest",
            "physical_clear",
            "materialize",
            "read_manifest",
            "deactivate",
            "activate",
        ):
            for phase in ("before", "after"):
                fault: _FaultingConnection | None = None

                @contextmanager
                def faulting_transaction(*, immediate: bool = False):
                    nonlocal fault
                    with real_transaction(immediate=immediate) as connection:
                        fault = _FaultingConnection(
                            connection,
                            target=target,
                            phase=phase,
                        )
                        yield fault

                with self.subTest(target=target, phase=phase):
                    with patch.object(
                        repository,
                        "transaction",
                        faulting_transaction,
                    ):
                        with self.assertRaisesRegex(
                            _InjectedRebuildFault,
                            rf"{target}:{phase}",
                        ):
                            self._rebuild(f"fault-{target}-{phase}")
                    assert fault is not None
                    self.assertEqual(fault.hits, 1)
                    self.assertEqual(self._projection_snapshot(), baseline)
                    self.assertEqual(self._physical_bytes(), physical)
                    self._assert_only_generation_is_active(
                        self.old_generation_id
                    )

    def test_each_fault_preserves_a_missing_physical_baseline(self) -> None:
        self._delete_physical_projection()
        baseline = self._projection_snapshot()
        repository = self.fixture.repository
        real_transaction = repository.transaction
        for target in (
            "manifest",
            "physical_clear",
            "materialize",
            "read_manifest",
            "deactivate",
            "activate",
        ):
            for phase in ("before", "after"):
                fault: _FaultingConnection | None = None

                @contextmanager
                def faulting_transaction(*, immediate: bool = False):
                    nonlocal fault
                    with real_transaction(immediate=immediate) as connection:
                        fault = _FaultingConnection(
                            connection,
                            target=target,
                            phase=phase,
                        )
                        yield fault

                with self.subTest(target=target, phase=phase):
                    with patch.object(
                        repository,
                        "transaction",
                        faulting_transaction,
                    ):
                        with self.assertRaisesRegex(
                            _InjectedRebuildFault,
                            rf"{target}:{phase}",
                        ):
                            self._rebuild(
                                f"missing-physical-fault-{target}-{phase}"
                            )
                    assert fault is not None
                    self.assertEqual(fault.hits, 1)
                    self.assertEqual(self._projection_snapshot(), baseline)
                    self._assert_only_generation_is_active(
                        self.old_generation_id
                    )


if __name__ == "__main__":
    unittest.main()
