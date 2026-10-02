from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from core.db import ImageIndexRepository
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_row,
    projected_image_index_row_sha256,
    require_valid_projected_image_index_row,
)
from core.schemas import StoredImageRecord
from tests import test_image_projection_schema_guards as schema_fixture


FIRST_NOW = "2026-08-29T01:00:00+00:00"
SECOND_NOW = "2026-08-29T02:00:00+00:00"
PROJECTOR_AUTHORITY_ERROR = "image_index_projector_authority_required"


class _FailAfterPhysicalMaterializationConnection:
    def __init__(self, connection: sqlite3.Connection, *, asset_id: str) -> None:
        self._connection = connection
        self._asset_id = asset_id
        self.materialization_seen = False

    @staticmethod
    def _normalized(statement: str) -> str:
        return " ".join(statement.split()).upper()

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        normalized = self._normalized(statement)
        if normalized.startswith("INSERT INTO IMAGE_PROJECTION_MANIFESTS"):
            physical = self._connection.execute(
                "SELECT 1 FROM image_index WHERE id=?",
                (self._asset_id,),
            ).fetchone()
            if not self.materialization_seen or physical is None:
                raise AssertionError(
                    "manifest reached before canonical image_index materialization"
                )
            raise sqlite3.OperationalError(
                "injected failure after image_index materialization"
            )
        cursor = self._connection.execute(statement, parameters)
        if normalized.startswith(("INSERT INTO IMAGE_INDEX", "UPDATE IMAGE_INDEX")):
            self.materialization_seen = True
        return cursor

    def executemany(
        self,
        statement: str,
        parameters: object,
    ) -> sqlite3.Cursor:
        normalized = self._normalized(statement)
        if normalized.startswith("INSERT INTO IMAGE_PROJECTION_MANIFESTS"):
            physical = self._connection.execute(
                "SELECT 1 FROM image_index WHERE id=?",
                (self._asset_id,),
            ).fetchone()
            if not self.materialization_seen or physical is None:
                raise AssertionError(
                    "manifest reached before canonical image_index materialization"
                )
            raise sqlite3.OperationalError(
                "injected failure after image_index materialization"
            )
        cursor = self._connection.executemany(statement, parameters)
        if normalized.startswith(("INSERT INTO IMAGE_INDEX", "UPDATE IMAGE_INDEX")):
            self.materialization_seen = True
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


def _stored_record(row: dict[str, object]) -> StoredImageRecord:
    tags = json.loads(str(row["tags_json"]))
    assert isinstance(tags, list) and all(isinstance(tag, str) for tag in tags)
    return StoredImageRecord(
        id=str(row["id"]),
        sha256=str(row["sha256"]),
        filename=str(row["filename"]),
        relative_path=str(row["relative_path"]),
        mime_type=str(row["mime_type"]),
        file_size=int(row["file_size"]),
        width=row["width"],
        height=row["height"],
        taken_at=row["taken_at"],
        lat=row["lat"],
        lon=row["lon"],
        altitude=row["altitude"],
        place_name=row["place_name"],
        country=row["country"],
        description=str(row["description"]),
        tags=tags,
        combined_text=str(row["combined_text"]),
        text_embedding_model=row["text_embedding_model"],
        combined_text_embedding_blob=row["combined_text_embedding"],
        embedding_backend=str(row["embedding_backend"]),
        embedding_blob=bytes(row["embedding"]),
        aesthetic_score=row["aesthetic_score"],
        aesthetic_model=row["aesthetic_model"],
        technical_quality_score=row["technical_quality_score"],
        aesthetic_updated_at=row["aesthetic_updated_at"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


class ImageProjectionMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_fixture.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _project(
        self,
        job_id: str | None = None,
        *,
        now: str = FIRST_NOW,
    ) -> dict[str, object]:
        with patch(
            "core.image_analysis_persistence._utc_now_iso",
            return_value=now,
        ):
            return self.fixture.repository.project_image_analysis_change(
                job_id=job_id or self.fixture.job_id,
                runtime_generation=schema_fixture.RUNTIME_GENERATION,
                expected_attempt=1,
            )

    def _active_document(self) -> tuple[str, dict[str, object]]:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                """SELECT generation.id,row.row_json
                     FROM image_projection_generations generation
                     JOIN image_projection_rows row ON row.generation_id=generation.id
                    WHERE generation.is_active=1 AND row.asset_id=?""",
                (self.fixture.asset_id,),
            ).fetchone()
        assert row is not None
        document = json.loads(str(row["row_json"]))
        require_valid_projected_image_index_row(document)
        return str(row["id"]), document

    def _physical_rows(self) -> list[dict[str, object]]:
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                f"SELECT {columns} FROM image_index ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def _insert_physical(self, row: dict[str, object]) -> None:
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                tuple(row[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )

    def _seed_expected_physical_if_missing(
        self,
        *,
        now: str,
    ) -> tuple[str, dict[str, object], dict[str, object]]:
        generation_id, document = self._active_document()
        expected = materialize_projected_image_index_row(
            document,
            created_at=now,
            updated_at=now,
        )
        physical = self._physical_rows()
        if not physical:
            self._insert_physical(expected)
        else:
            self.assertEqual(physical, [expected])
        return generation_id, document, expected

    def _replace_physical(self, row: dict[str, object]) -> None:
        mutable_columns = tuple(
            column for column in IMAGE_INDEX_SQL_COLUMNS if column != "id"
        )
        assignments = ",".join(f"{column}=?" for column in mutable_columns)
        with self.fixture.repository.transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"UPDATE image_index SET {assignments} WHERE id=?",
                (
                    *(row[column] for column in mutable_columns),
                    row["id"],
                ),
            )
        self.assertEqual(cursor.rowcount, 1)

    def _assert_current_projection_corrupt(self) -> None:
        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )

    def _publish_revision_two(
        self,
        *,
        enqueue_scope: str = "schema-guard/image-analysis",
        idempotency_key: str = "schema-guard-materialization-revision-2",
        source_id: str | None = None,
    ) -> tuple[str, dict[str, object]]:
        selected_source_id = source_id or self.fixture.source_id
        database_stat = self.fixture.db_path.stat(follow_symlinks=False)
        job = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.fixture.asset_id,
            source_id=selected_source_id,
            analysis_profile=schema_fixture.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope=enqueue_scope,
            idempotency_key=idempotency_key,
        )
        job_id = str(job["id"])
        binding = self.fixture.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        first_binding = self.fixture.binding
        first_source_id = self.fixture.source_id
        try:
            self.fixture.binding = binding
            self.fixture.source_id = selected_source_id
            result = self.fixture._sealed_result()
        finally:
            self.fixture.binding = first_binding
            self.fixture.source_id = first_source_id
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return job_id, {
            "analysis_run_id": str(binding["analysis_run_id"]),
            "revision": int(binding["intended_revision"]),
            "content_sha256": str(result["content_sha256"]),
        }

    def test_projector_materializes_one_exact_canonical_physical_row(self) -> None:
        projected = self._project(now=FIRST_NOW)

        generation_id, document = self._active_document()
        expected = materialize_projected_image_index_row(
            document,
            created_at=FIRST_NOW,
            updated_at=FIRST_NOW,
        )
        physical = self._physical_rows()
        self.assertEqual(generation_id, projected["generation_id"])
        self.assertEqual(physical, [expected])
        self.assertEqual(physical[0]["id"], self.fixture.asset_id)
        self.assertFalse(str(physical[0]["id"]).startswith("img_"))
        with self.fixture.repository.transaction() as connection:
            legacy_identity_count = connection.execute(
                "SELECT COUNT(*) FROM image_index WHERE id LIKE 'img_%'"
            ).fetchone()[0]
        self.assertEqual(legacy_identity_count, 0)

    def test_verified_active_row_allows_same_asset_source_relocation(self) -> None:
        first = self._project(now=FIRST_NOW)
        _, _, old_physical = self._seed_expected_physical_if_missing(
            now=FIRST_NOW
        )
        original = self.fixture.library / "guard.png"
        relocated_directory = self.fixture.library / "relocated"
        relocated_directory.mkdir()
        relocated = relocated_directory / "guard-renamed.png"
        original.rename(relocated)
        observed = relocated.stat()
        asset = self.fixture.repository.upsert_asset_source(
            root_id=self.fixture.root_id,
            relative_path="relocated/guard-renamed.png",
            filename=relocated.name,
            kind="image",
            sha256=self.fixture.asset_sha256,
            mime_type="image/png",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.assertEqual(asset["id"], self.fixture.asset_id)
        relocated_source_id = str(asset["asset_source_id"])
        second_job_id, second_binding = self._publish_revision_two(
            source_id=relocated_source_id,
            idempotency_key="schema-guard-source-relocation-revision-2",
        )

        second = self._project(second_job_id, now=SECOND_NOW)

        physical = self._physical_rows()
        self.assertEqual(len(physical), 1)
        self.assertEqual(physical[0]["id"], self.fixture.asset_id)
        self.assertEqual(
            physical[0]["relative_path"],
            "relocated/guard-renamed.png",
        )
        self.assertNotEqual(physical[0], old_physical)
        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["analysis_binding"], second_binding)
        self.assertEqual(current["source"]["source_id"], relocated_source_id)
        self.assertEqual(current["projection"]["status"], "current")
        self.assertEqual(second["previous_generation_id"], first["generation_id"])
        with self.fixture.repository.transaction() as connection:
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1"""
            ).fetchall()
        self.assertEqual([str(row["id"]) for row in active], [second["generation_id"]])

    def test_source_relocation_rejects_tampered_active_physical_row(self) -> None:
        first = self._project(now=FIRST_NOW)
        self._seed_expected_physical_if_missing(now=FIRST_NOW)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE image_index SET description=?,updated_at=? WHERE id=?""",
                ("externally tampered", SECOND_NOW, self.fixture.asset_id),
            )
            before_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "image_projection_generations",
                    "image_projection_rows",
                    "image_projection_manifests",
                    "image_projection_read_manifests",
                    "image_projection_receipts",
                )
            )
        tampered_physical = self._physical_rows()

        original = self.fixture.library / "guard.png"
        relocated_directory = self.fixture.library / "relocated"
        relocated_directory.mkdir()
        relocated = relocated_directory / "guard-renamed.png"
        original.rename(relocated)
        observed = relocated.stat()
        asset = self.fixture.repository.upsert_asset_source(
            root_id=self.fixture.root_id,
            relative_path="relocated/guard-renamed.png",
            filename=relocated.name,
            kind="image",
            sha256=self.fixture.asset_sha256,
            mime_type="image/png",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        second_job_id, _ = self._publish_revision_two(
            source_id=str(asset["asset_source_id"]),
            idempotency_key="tampered-source-relocation-revision-2",
        )

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_physical_projection_conflict",
        ):
            self._project(second_job_id, now=SECOND_NOW)

        with self.fixture.repository.transaction() as connection:
            after_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "image_projection_generations",
                    "image_projection_rows",
                    "image_projection_manifests",
                    "image_projection_read_manifests",
                    "image_projection_receipts",
                )
            )
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1"""
            ).fetchall()
        self.assertEqual(after_counts, before_counts)
        self.assertEqual(
            [str(row["id"]) for row in active],
            [first["generation_id"]],
        )
        self.assertEqual(self._physical_rows(), tampered_physical)

    def test_legacy_reference_resolves_only_in_exact_root_scope_to_canonical_id(self) -> None:
        legacy_id = "historic-image-reference-v1"
        legacy_physical = materialize_projected_image_index_row(
            self.fixture.projected_row_document,
            created_at=FIRST_NOW,
            updated_at=FIRST_NOW,
        )
        legacy_physical["id"] = legacy_id
        self._insert_physical(legacy_physical)

        backfill_job_id, backfill_binding = self._publish_revision_two(
            enqueue_scope="legacy-image-backfill/image-analysis",
            idempotency_key="explicit-legacy-backfill-revision-2",
        )
        self._project(backfill_job_id, now=FIRST_NOW)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_alias_scope_required",
        ):
            self.fixture.repository.resolve_image_asset_reference(legacy_id)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "legacy_alias_conflict",
        ):
            self.fixture.repository.resolve_image_asset_reference(
                legacy_id,
                library_root_id=f"root_{'f' * 24}",
            )
        resolved = self.fixture.repository.resolve_image_asset_reference(
            legacy_id,
            library_root_id=self.fixture.root_id,
        )
        assert resolved is not None
        self.assertEqual(
            (resolved["reference_kind"], resolved["asset_id"]),
            ("legacy_alias", self.fixture.asset_id),
        )
        self.assertEqual(resolved["analysis_binding"], backfill_binding)
        self.assertEqual(resolved["projection"]["status"], "current")
        canonical = self.fixture.repository.resolve_image_asset_reference(
            self.fixture.asset_id,
        )
        assert canonical is not None
        self.assertEqual(
            (canonical["reference_kind"], canonical["asset_id"]),
            ("canonical", self.fixture.asset_id),
        )

    def test_projector_rejects_same_sha_and_path_without_explicit_backfill_scope(
        self,
    ) -> None:
        legacy_id = "ordinary-import-must-not-mint-alias"
        legacy_physical = materialize_projected_image_index_row(
            self.fixture.projected_row_document,
            created_at=FIRST_NOW,
            updated_at=FIRST_NOW,
        )
        legacy_physical["id"] = legacy_id
        self._insert_physical(legacy_physical)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_legacy_backfill_required",
        ):
            self._project(now=FIRST_NOW)

        with self.fixture.repository.transaction() as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "legacy_image_aliases",
                    "image_projection_generations",
                    "image_projection_rows",
                    "image_projection_manifests",
                    "image_projection_receipts",
                )
            )
            legacy_survived = connection.execute(
                "SELECT 1 FROM image_index WHERE id=?",
                (legacy_id,),
            ).fetchone()
        self.assertEqual(counts, (0, 0, 0, 0, 0))
        self.assertIsNotNone(legacy_survived)

    def test_first_projection_does_not_trust_legacy_row_that_reuses_asset_id(
        self,
    ) -> None:
        legacy_physical = materialize_projected_image_index_row(
            self.fixture.projected_row_document,
            created_at=FIRST_NOW,
            updated_at=FIRST_NOW,
        )
        self.assertEqual(legacy_physical["id"], self.fixture.asset_id)
        self._insert_physical(legacy_physical)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_legacy_backfill_required",
        ):
            self._project(now=FIRST_NOW)

        with self.fixture.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_generations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM legacy_image_aliases"
                ).fetchone()[0],
                0,
            )

    def test_projector_rejects_backfill_scope_when_relative_source_is_not_exact(
        self,
    ) -> None:
        legacy_id = "wrong-relative-source-must-not-alias"
        legacy_physical = materialize_projected_image_index_row(
            self.fixture.projected_row_document,
            created_at=FIRST_NOW,
            updated_at=FIRST_NOW,
        )
        legacy_physical["id"] = legacy_id
        legacy_physical["filename"] = "wrong.png"
        legacy_physical["relative_path"] = "other/wrong.png"
        self._insert_physical(legacy_physical)
        backfill_job_id, _ = self._publish_revision_two(
            enqueue_scope="legacy-image-backfill/image-analysis",
            idempotency_key="wrong-relative-backfill-revision-2",
        )

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_legacy_backfill_required",
        ):
            self._project(backfill_job_id, now=FIRST_NOW)

        with self.fixture.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM legacy_image_aliases"
                ).fetchone()[0],
                0,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM image_index WHERE id=?",
                    (legacy_id,),
                ).fetchone()
            )

    def test_fault_after_physical_write_rolls_back_to_old_active_generation(self) -> None:
        first = self._project(now=FIRST_NOW)
        old_generation, _, old_physical = self._seed_expected_physical_if_missing(
            now=FIRST_NOW
        )
        self.assertEqual(old_generation, first["generation_id"])
        second_job_id, second_binding = self._publish_revision_two()
        repository = self.fixture.repository
        real_transaction = repository.transaction
        observed: list[_FailAfterPhysicalMaterializationConnection] = []

        @contextmanager
        def failing_transaction(*, immediate: bool = False):
            with real_transaction(immediate=immediate) as connection:
                proxy = _FailAfterPhysicalMaterializationConnection(
                    connection,
                    asset_id=self.fixture.asset_id,
                )
                observed.append(proxy)
                yield proxy

        with patch.object(repository, "transaction", failing_transaction):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "injected failure after image_index materialization",
            ):
                self._project(second_job_id, now=SECOND_NOW)
        self.assertTrue(any(item.materialization_seen for item in observed))

        with repository.transaction() as connection:
            projection_counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "image_projection_generations",
                    "image_projection_rows",
                    "image_projection_manifests",
                    "image_projection_receipts",
                )
            )
            active = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1"""
            ).fetchone()
            head = connection.execute(
                """SELECT analysis_run_id,revision,content_sha256
                     FROM image_analysis_heads WHERE asset_id=?""",
                (self.fixture.asset_id,),
            ).fetchone()
        assert active is not None and head is not None
        self.assertEqual(projection_counts, (1, 1, 1, 1))
        self.assertEqual(active["id"], old_generation)
        self.assertEqual(self._physical_rows(), [old_physical])
        self.assertEqual(dict(head), second_binding)

    def test_self_consistent_external_physical_rewrite_is_not_trusted(self) -> None:
        self._project(now=FIRST_NOW)
        _, document, _ = self._seed_expected_physical_if_missing(now=FIRST_NOW)
        forged = deepcopy(document)
        forged_row = forged["row"]
        assert isinstance(forged_row, dict)
        forged_row["filename"] = "forged.png"
        forged_row["relative_path"] = "forged/guard.png"
        forged["row_sha256"] = projected_image_index_row_sha256(forged_row)
        require_valid_projected_image_index_row(forged)
        forged_physical = materialize_projected_image_index_row(
            forged,
            created_at=FIRST_NOW,
            updated_at=SECOND_NOW,
        )

        self._replace_physical(forged_physical)

        self._assert_current_projection_corrupt()

    def test_single_column_external_physical_tamper_is_not_trusted(self) -> None:
        self._project(now=FIRST_NOW)
        self._seed_expected_physical_if_missing(now=FIRST_NOW)
        with self.fixture.repository.transaction(immediate=True) as connection:
            cursor = connection.execute(
                """UPDATE image_index SET description=?,updated_at=? WHERE id=?""",
                ("externally tampered", SECOND_NOW, self.fixture.asset_id),
            )
        self.assertEqual(cursor.rowcount, 1)

        self._assert_current_projection_corrupt()

    def test_v14_legacy_mutation_apis_require_projector_authority(self) -> None:
        self._project(now=FIRST_NOW)
        _, _, physical = self._seed_expected_physical_if_missing(now=FIRST_NOW)
        record = _stored_record(physical)
        legacy = ImageIndexRepository(self.fixture.db_path)
        mutations = (
            ("upsert", lambda: legacy.upsert(record)),
            (
                "refresh",
                lambda: legacy.refresh_existing_file_metadata(
                    sha256=record.sha256,
                    filename=record.filename,
                    relative_path=record.relative_path,
                    mime_type=record.mime_type,
                    file_size=record.file_size,
                    width=record.width,
                    height=record.height,
                    taken_at=record.taken_at,
                    lat=record.lat,
                    lon=record.lon,
                    altitude=record.altitude,
                    updated_at=SECOND_NOW,
                ),
            ),
            (
                "quality",
                lambda: legacy.update_image_quality(
                    image_id=record.id,
                    aesthetic_score=0.5,
                    aesthetic_model="legacy-direct-write",
                    technical_quality_score=0.5,
                    aesthetic_updated_at=SECOND_NOW,
                ),
            ),
            (
                "delete",
                lambda: legacy.delete_by_relative_path(record.relative_path),
            ),
        )

        for name, mutation in mutations:
            with self.subTest(name=name), self.assertRaisesRegex(
                RuntimeError,
                PROJECTOR_AUTHORITY_ERROR,
            ):
                mutation()

        self.assertEqual(self._physical_rows(), [physical])


class LegacyImageIndexMutationCompatibilityTests(unittest.TestCase):
    def test_pre_v14_index_only_database_keeps_legacy_mutation_compatibility(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-pre-v14-image-index-"
        ) as temporary:
            db_path = Path(temporary) / "legacy.db"
            repository = ImageIndexRepository(db_path)
            repository.ensure_schema()
            row = {
                "id": "img_legacy_fixture",
                "sha256": "a" * 64,
                "filename": "legacy.png",
                "relative_path": "legacy.png",
                "mime_type": "image/png",
                "file_size": 4,
                "width": 1,
                "height": 1,
                "taken_at": None,
                "lat": None,
                "lon": None,
                "altitude": None,
                "place_name": None,
                "country": None,
                "description": "legacy",
                "tags_json": "[]",
                "combined_text": "legacy",
                "text_embedding_model": None,
                "combined_text_embedding": None,
                "embedding_backend": "legacy",
                "embedding": b"abcd",
                "aesthetic_score": None,
                "aesthetic_model": None,
                "technical_quality_score": None,
                "aesthetic_updated_at": None,
                "created_at": FIRST_NOW,
                "updated_at": FIRST_NOW,
            }
            record = _stored_record(row)

            repository.upsert(record)
            repository.refresh_existing_file_metadata(
                sha256=record.sha256,
                filename="moved.png",
                relative_path="moved.png",
                mime_type=record.mime_type,
                file_size=record.file_size,
                width=record.width,
                height=record.height,
                taken_at=record.taken_at,
                lat=record.lat,
                lon=record.lon,
                altitude=record.altitude,
                updated_at=SECOND_NOW,
            )
            repository.update_image_quality(
                image_id=record.id,
                aesthetic_score=0.8,
                aesthetic_model="legacy-quality",
                technical_quality_score=0.7,
                aesthetic_updated_at=SECOND_NOW,
            )
            with closing(sqlite3.connect(db_path)) as connection:
                moved = connection.execute(
                    """SELECT relative_path,aesthetic_score,aesthetic_model
                         FROM image_index WHERE id=?""",
                    (record.id,),
                ).fetchone()
                database_meta = connection.execute(
                    """SELECT 1 FROM sqlite_master
                        WHERE type='table' AND name='database_meta'"""
                ).fetchone()
            self.assertEqual(moved, ("moved.png", 0.8, "legacy-quality"))
            self.assertIsNone(database_meta)

            repository.delete_by_relative_path("moved.png")

            self.assertEqual(repository.fetch_candidates(), [])


if __name__ == "__main__":
    unittest.main()
