from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
for search_path in (REPOSITORY_ROOT, SCRIPTS):
    if str(search_path) not in sys.path:
        sys.path.insert(0, str(search_path))

from core.image_projection_renderer import (  # noqa: E402
    IMAGE_INDEX_SQL_COLUMNS,
    materialize_projected_image_index_values,
)
from memolens_creator_store import CreatorMemoryReader  # noqa: E402
from memolens_mcp import INBOX_ASSET_OUTPUT  # noqa: E402
from memolens_sqlite import ReadOnlyDatabase  # noqa: E402
from tests import test_image_projection_schema_guards as schema_guards  # noqa: E402


class CanonicalImageInboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _page(self, path: Path | None = None) -> dict[str, object]:
        return CreatorMemoryReader(
            ReadOnlyDatabase(path or self.fixture.db_path)
        ).inbox_list(
            state="all",
            kinds=["image"],
            limit=24,
            cursor=None,
        )

    def _asset(self, path: Path | None = None) -> dict[str, object]:
        page = self._page(path)
        assets = page["assets"]
        assert isinstance(assets, list)
        self.assertEqual(len(assets), 1)
        asset = assets[0]
        assert isinstance(asset, dict)
        return asset

    def _activate_clean_generation(self, generation_id: str) -> None:
        with self.fixture.repository.transaction(immediate=True) as connection:
            active_rows = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 AND id!=? ORDER BY id""",
                (generation_id,),
            ).fetchall()
            self.assertLessEqual(len(active_rows), 1)
            previous_generation_id = (
                str(active_rows[0]["id"]) if active_rows else None
            )
            completed = connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=?
                    WHERE id=? AND status='building' AND is_active=0""",
                (schema_guards.NOW, generation_id),
            )
            self.assertEqual(completed.rowcount, 1)
            read_manifest = connection.execute(
                """INSERT INTO image_projection_read_manifests(
                       generation_id,previous_generation_id,database_uuid,
                       canonical_high_water_position,compiler_id,
                       projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,
                       mismatched_count,blocked_count,alias_set_sha256,
                       row_set_sha256,manifest_json,manifest_sha256,created_at)
                   SELECT manifest.generation_id,?,manifest.database_uuid,
                          manifest.canonical_high_water_position,
                          manifest.compiler_id,manifest.projector_version,
                          manifest.eligible_count,manifest.projected_count,
                          manifest.alias_count,manifest.missing_count,
                          manifest.unexpected_count,manifest.mismatched_count,
                          manifest.blocked_count,manifest.alias_set_sha256,
                          manifest.row_set_sha256,manifest.manifest_json,
                          manifest.manifest_sha256,?
                     FROM image_projection_manifests manifest
                    WHERE manifest.generation_id=?""",
                (previous_generation_id, schema_guards.NOW, generation_id),
            )
            self.assertEqual(read_manifest.rowcount, 1)
            if previous_generation_id is not None:
                deactivated = connection.execute(
                    """UPDATE image_projection_generations SET is_active=0
                        WHERE id=? AND status='complete' AND is_active=1""",
                    (previous_generation_id,),
                )
                self.assertEqual(deactivated.rowcount, 1)
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (generation_id,),
            )
            self.assertEqual(activated.rowcount, 1)

    def _install_complete_projection(self) -> str:
        generation_id = self.fixture._create_generation(with_row=True)
        receipt = self.fixture._projection_receipt(generation_id)
        self.fixture._insert_projection_receipt(generation_id, receipt)
        self.fixture._insert_manifest(generation_id, self.fixture._manifest())
        values = materialize_projected_image_index_values(
            self.fixture.projected_row_document,
            created_at=schema_guards.NOW,
            updated_at=schema_guards.NOW,
        )
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _column in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                values,
            )
        self._activate_clean_generation(generation_id)
        return generation_id

    def _publish_second_revision(self) -> tuple[str, dict[str, object]]:
        database_stat = self.fixture.db_path.stat(follow_symlinks=False)
        job = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=schema_guards.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope="canonical-inbox/same-asset-revision",
            idempotency_key="canonical-inbox-same-asset-revision-two",
        )
        job_id = str(job["id"])
        binding = self.fixture.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        original_binding = self.fixture.binding
        try:
            self.fixture.binding = binding
            result = self.fixture._sealed_result()
        finally:
            self.fixture.binding = original_binding
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return job_id, {
            "analysis_run_id": str(binding["analysis_run_id"]),
            "revision": int(binding["intended_revision"]),
            "content_sha256": str(result["content_sha256"]),
        }

    def test_current_image_inbox_preserves_full_verified_observation(self) -> None:
        generation_id = self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO asset_review_revisions(
                       asset_id,revision,inbox_state,favorite,project_ready,
                       note,provenance_json,created_at)
                   VALUES(?,1,'kept',1,1,?,'{}',?)""",
                (
                    self.fixture.asset_id,
                    "PRIVATE_REVIEW_NOTE_MUST_NOT_SURFACE",
                    schema_guards.NOW,
                ),
            )

        asset = self._asset()
        observation = asset["canonical_image_observation"]
        assert isinstance(observation, dict)

        self.assertEqual(observation["status"], "current")
        self.assertEqual(observation["provenance_status"], "verified_current")
        self.assertEqual(observation["asset_id"], self.fixture.asset_id)
        self.assertEqual(
            observation["analysis_binding"], self.fixture.analysis_binding
        )
        projection = observation["projection"]
        assert isinstance(projection, dict)
        self.assertEqual(projection["generation_id"], generation_id)
        self.assertIsInstance(observation["stages"], dict)
        self.assertNotIn("review", observation)
        review = asset["review"]
        assert isinstance(review, dict)
        self.assertNotIn("canonical_image_observation", review)
        self.assertTrue(review["has_note"])
        serialized = json.dumps(asset, ensure_ascii=False)
        self.assertNotIn("PRIVATE_REVIEW_NOTE_MUST_NOT_SURFACE", serialized)
        self.assertNotIn(str(self.fixture.library), serialized)
        self.assertNotIn("relative_path", serialized)

    def test_unpublished_and_superseded_images_are_explicitly_pending(self) -> None:
        initially_pending = self._asset()["canonical_image_observation"]
        assert isinstance(initially_pending, dict)
        self.assertEqual(initially_pending["status"], "pending")
        self.assertEqual(
            initially_pending["provenance_status"], "verified_pending"
        )
        self.assertIsNone(initially_pending["stages"])
        self.assertNotEqual(
            initially_pending["provenance_status"], "verified_current"
        )

        self._install_complete_projection()
        _job_id, second_binding = self._publish_second_revision()
        stale = self._asset()["canonical_image_observation"]
        assert isinstance(stale, dict)
        self.assertEqual(stale["status"], "pending")
        self.assertEqual(stale["analysis_binding"], second_binding)
        self.assertEqual(stale["reason_code"], "projection_not_published")
        self.assertIsNone(stale["stages"])

    def test_corrupt_projection_is_unavailable_and_legacy_text_never_leaks(
        self,
    ) -> None:
        self._install_complete_projection()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET description=? WHERE id=?",
                (
                    "FORGED_LEGACY_ANALYSIS_TEXT_MUST_NOT_SURFACE",
                    self.fixture.asset_id,
                ),
            )

        asset = self._asset()
        observation = asset["canonical_image_observation"]
        assert isinstance(observation, dict)
        self.assertEqual(observation["status"], "unavailable")
        self.assertEqual(observation["provenance_status"], "unavailable")
        self.assertEqual(
            observation["reason_code"],
            "canonical_image_projection_corrupt",
        )
        self.assertIsNone(observation["stages"])
        self.assertNotIn(
            "FORGED_LEGACY_ANALYSIS_TEXT_MUST_NOT_SURFACE",
            json.dumps(asset, ensure_ascii=False),
        )

    def test_legacy_inbox_is_audit_only_without_path_or_analysis_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-legacy-inbox-") as root:
            path = Path(root) / "legacy.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(
                    """
                    CREATE TABLE assets (
                        id TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL
                    );
                    CREATE TABLE asset_sources (
                        id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                        filename TEXT NOT NULL, relative_path TEXT NOT NULL,
                        status TEXT NOT NULL
                    );
                    CREATE TABLE asset_review_revisions (
                        asset_id TEXT NOT NULL, revision INTEGER NOT NULL,
                        inbox_state TEXT NOT NULL, favorite INTEGER NOT NULL,
                        project_ready INTEGER NOT NULL, note TEXT
                    );
                    CREATE TABLE image_index (
                        id TEXT PRIMARY KEY, description TEXT, relative_path TEXT
                    );
                    """
                )
                connection.execute(
                    "INSERT INTO assets VALUES('asset_legacy','image',?)",
                    ("a" * 64,),
                )
                connection.execute(
                    """INSERT INTO asset_sources VALUES(
                           'source_legacy','asset_legacy','safe.jpg',
                           'PRIVATE/LEGACY_PATH_SECRET.jpg','available')"""
                )
                connection.execute(
                    """INSERT INTO asset_review_revisions VALUES(
                           'asset_legacy',1,'inbox',0,0,'PRIVATE_NOTE_SECRET')"""
                )
                connection.execute(
                    """INSERT INTO image_index VALUES(
                           'asset_legacy','LEGACY_ANALYSIS_SECRET',
                           'PRIVATE/LEGACY_PATH_SECRET.jpg')"""
                )
                connection.commit()

            asset = self._asset(path)
            observation = asset["canonical_image_observation"]
            assert isinstance(observation, dict)
            self.assertEqual(observation["status"], "unavailable")
            self.assertEqual(observation["provenance_status"], "unavailable")
            self.assertEqual(
                observation["reason_code"],
                "canonical_image_schema_unavailable",
            )
            self.assertIsNone(observation["stages"])
            serialized = json.dumps(asset, ensure_ascii=False)
            for secret in (
                "LEGACY_ANALYSIS_SECRET",
                "LEGACY_PATH_SECRET",
                "PRIVATE_NOTE_SECRET",
            ):
                self.assertNotIn(secret, serialized)

    def test_mcp_inbox_schema_requires_closed_observation_as_review_sibling(
        self,
    ) -> None:
        properties = INBOX_ASSET_OUTPUT["properties"]
        required = INBOX_ASSET_OUTPUT["required"]
        self.assertIn("canonical_image_observation", properties)
        self.assertIn("canonical_image_observation", required)
        self.assertFalse(INBOX_ASSET_OUTPUT["additionalProperties"])
        observation_union = properties["canonical_image_observation"]["anyOf"][0]
        variants = observation_union["oneOf"]
        self.assertEqual(
            {variant["properties"]["status"]["const"] for variant in variants},
            {"current", "pending", "unavailable"},
        )
        for variant in variants:
            self.assertFalse(variant["additionalProperties"])
            self.assertNotIn("review", variant["properties"])
        review = properties["review"]
        self.assertFalse(review["additionalProperties"])
        self.assertNotIn("canonical_image_observation", review["properties"])


if __name__ == "__main__":
    unittest.main()
