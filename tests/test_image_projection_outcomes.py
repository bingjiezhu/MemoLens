from __future__ import annotations

import hashlib
import json
import os
import unittest

from core.image_analysis_contract import (
    projection_receipt_sha256,
    require_valid_projection_receipt,
)
from core.image_analysis_job_contract import (
    canonical_sha256 as canonical_job_sha256,
    require_valid_image_analysis_error,
)
from tests import test_image_analysis_persistence as persistence_fixture
from tests import test_image_projection_schema_guards as schema_fixture


class BlockedImageProjectionOutcomeTests(unittest.TestCase):
    REASON_CODE = "image_projection_image_search_vector_missing"

    def setUp(self) -> None:
        self.fixture = persistence_fixture.ImageAnalysisPersistenceTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _publish(self) -> str:
        job = self.fixture._enqueue(key="blocked-projection")
        job_id = str(job["id"])
        result = self.fixture._sealed_result(job_id)
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=persistence_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return job_id

    def _project(self, job_id: str) -> dict[str, object]:
        return self.fixture.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=persistence_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )

    def _projection_counts(self) -> tuple[int, int, int, int]:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_projection_generations),
                     (SELECT COUNT(*) FROM image_projection_rows),
                     (SELECT COUNT(*) FROM image_projection_manifests),
                     (SELECT COUNT(*) FROM image_projection_receipts)"""
            ).fetchone()
        assert row is not None
        return tuple(int(value) for value in row)

    def test_blocked_projection_is_durable_terminal_and_replayable(self) -> None:
        job_id = self._publish()

        projected = self._project(job_id)

        self.assertIs(projected["replayed"], False)
        self.assertIsNone(projected["generation_id"])
        receipt = projected["projection_receipt"]
        assert isinstance(receipt, dict)
        require_valid_projection_receipt(receipt)
        self.assertEqual(receipt["outcome"], "blocked")
        self.assertEqual(receipt["reason_code"], self.REASON_CODE)
        self.assertIsNone(receipt["projected_row_sha256"])
        self.assertEqual(
            receipt["receipt_sha256"],
            projection_receipt_sha256(receipt),
        )
        self.assertEqual(self._projection_counts(), (0, 0, 0, 1))

        with self.fixture.repository.transaction() as connection:
            canonical_counts = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_analysis_results),
                     (SELECT COUNT(*) FROM image_analysis_heads),
                     (SELECT COUNT(*) FROM image_publish_receipts)"""
            ).fetchone()
            stored_receipt = connection.execute(
                """SELECT generation_id,receipt_sha256 FROM image_projection_receipts
                    WHERE change_position=?""",
                (projected["change_position"],),
            ).fetchone()
            attempt = connection.execute(
                """SELECT state,finished_at FROM image_analysis_attempt_states
                    WHERE job_id=? AND attempt=1""",
                (job_id,),
            ).fetchone()
        assert canonical_counts is not None and stored_receipt is not None
        assert attempt is not None
        self.assertEqual(tuple(canonical_counts), (1, 1, 1))
        self.assertIsNone(stored_receipt["generation_id"])
        self.assertEqual(stored_receipt["receipt_sha256"], receipt["receipt_sha256"])
        self.assertEqual(attempt["state"], "finished")
        self.assertIsNotNone(attempt["finished_at"])

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["projection"]["status"], "blocked")
        self.assertEqual(current["projection"]["outcome"], "blocked")
        self.assertEqual(current["projection"]["reason_code"], self.REASON_CODE)
        self.assertEqual(
            current["projection"]["receipt_sha256"],
            receipt["receipt_sha256"],
        )

        job = self.fixture.repository.get_media_job(job_id)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual((job["status"], job["stage"]), ("failed", "shadow_projection"))
        error = job["error"]
        assert isinstance(error, dict)
        require_valid_image_analysis_error(error)
        self.assertEqual(error["code"], "projection_failed")
        self.assertEqual(error["stage"], "shadow_projection")
        self.assertIs(error["retryable"], False)
        self.assertEqual(
            error["detail_sha256"],
            hashlib.sha256(self.REASON_CODE.encode("utf-8")).hexdigest(),
        )

        replay = self._project(job_id)

        self.assertIs(replay["replayed"], True)
        self.assertIsNone(replay["generation_id"])
        self.assertEqual(replay["projection_receipt"], receipt)
        self.assertEqual(self._projection_counts(), (0, 0, 0, 1))


class SupersededImageProjectionOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_fixture.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _publish_revision_two(self) -> tuple[str, dict[str, object], int]:
        database_stat = os.stat(self.fixture.db_path, follow_symlinks=False)
        second = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=schema_fixture.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope="schema-guard/image-analysis",
            idempotency_key="schema-guard-publication-revision-2",
        )
        job_id = str(second["id"])
        binding = self.fixture.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        self.assertEqual(binding["intended_revision"], 2)
        first_binding = self.fixture.binding
        try:
            self.fixture.binding = binding
            result = self.fixture._sealed_result()
        finally:
            self.fixture.binding = first_binding
        published = self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        analysis_binding = {
            "analysis_run_id": str(binding["analysis_run_id"]),
            "revision": int(binding["intended_revision"]),
            "content_sha256": str(result["content_sha256"]),
        }
        return job_id, analysis_binding, int(published["change_position"])

    def _project(self, job_id: str) -> dict[str, object]:
        return self.fixture.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=schema_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )

    def _projection_counts(self) -> tuple[int, int, int, int]:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_projection_generations),
                     (SELECT COUNT(*) FROM image_projection_rows),
                     (SELECT COUNT(*) FROM image_projection_manifests),
                     (SELECT COUNT(*) FROM image_projection_receipts)"""
            ).fetchone()
        assert row is not None
        return tuple(int(value) for value in row)

    @staticmethod
    def _shadow_outcome(job: dict[str, object]) -> dict[str, object]:
        checkpoint = job["checkpoint"]
        assert isinstance(checkpoint, dict)
        matches = [
            item
            for item in checkpoint["stage_outcomes"]
            if item["stage"] == "shadow_projection"
        ]
        assert len(matches) == 1
        return matches[0]

    @staticmethod
    def _shadow_receipt_digest(job: dict[str, object]) -> str:
        checkpoint = job["checkpoint"]
        assert isinstance(checkpoint, dict)
        matches = [
            item["sha256"]
            for item in checkpoint["artifact_digests"]
            if item["stage"] == "shadow_projection"
            and item["name"] == "projection_receipt"
        ]
        assert len(matches) == 1
        return str(matches[0])

    def test_high_water_projection_finalizes_superseded_and_current_jobs(self) -> None:
        first_job_id = self.fixture.job_id
        first_binding = dict(self.fixture.analysis_binding)
        first_position = self.fixture.change_position
        second_job_id, second_binding, second_position = self._publish_revision_two()
        self.assertEqual((first_position, second_position), (1, 2))

        projected = self._project(second_job_id)

        self.assertIs(projected["replayed"], False)
        self.assertEqual(projected["change_position"], second_position)
        self.assertEqual(projected["projection_receipt"]["outcome"], "applied")
        with self.fixture.repository.transaction() as connection:
            receipt_rows = connection.execute(
                """SELECT change_position,generation_id,receipt_json
                     FROM image_projection_receipts ORDER BY change_position"""
            ).fetchall()
            active = connection.execute(
                """SELECT generation.canonical_high_water_position,
                          generation.status,generation.is_active,
                          projected.analysis_run_id AS row_run_id,
                          projected.revision AS row_revision,
                          projected.content_sha256 AS row_content_sha256,
                          head.analysis_run_id AS head_run_id,
                          head.revision AS head_revision,
                          head.content_sha256 AS head_content_sha256,
                          mirror.analysis_run_id AS mirror_run_id
                     FROM image_projection_generations generation
                     JOIN image_projection_rows projected
                       ON projected.generation_id=generation.id
                      AND projected.asset_id=?
                     JOIN image_analysis_heads head ON head.asset_id=projected.asset_id
                     JOIN asset_analysis_heads mirror ON mirror.asset_id=projected.asset_id
                    WHERE generation.is_active=1""",
                (self.fixture.asset_id,),
            ).fetchone()
            attempts = {
                str(row["job_id"]): (str(row["state"]), row["finished_at"])
                for row in connection.execute(
                    """SELECT job_id,state,finished_at
                         FROM image_analysis_attempt_states
                        WHERE job_id IN (?,?)""",
                    (first_job_id, second_job_id),
                )
            }
        self.assertEqual(len(receipt_rows), 2)
        receipts = {
            int(row["change_position"]): json.loads(str(row["receipt_json"]))
            for row in receipt_rows
        }
        first_receipt = receipts[first_position]
        second_receipt = receipts[second_position]
        require_valid_projection_receipt(first_receipt)
        require_valid_projection_receipt(second_receipt)
        self.assertEqual(
            (first_receipt["outcome"], first_receipt["reason_code"]),
            ("superseded", "newer_head_published"),
        )
        self.assertEqual(
            (second_receipt["outcome"], second_receipt["reason_code"]),
            ("applied", None),
        )
        self.assertEqual(first_receipt["analysis_binding"], first_binding)
        self.assertEqual(second_receipt["analysis_binding"], second_binding)
        self.assertEqual(
            {row["generation_id"] for row in receipt_rows},
            {projected["generation_id"]},
        )

        assert active is not None
        self.assertEqual(
            (active["canonical_high_water_position"], active["status"], active["is_active"]),
            (2, "complete", 1),
        )
        self.assertEqual(
            (active["row_run_id"], active["row_revision"], active["row_content_sha256"]),
            (
                second_binding["analysis_run_id"],
                second_binding["revision"],
                second_binding["content_sha256"],
            ),
        )
        self.assertEqual(
            (active["head_run_id"], active["head_revision"], active["head_content_sha256"]),
            (
                second_binding["analysis_run_id"],
                second_binding["revision"],
                second_binding["content_sha256"],
            ),
        )
        self.assertEqual(active["mirror_run_id"], second_binding["analysis_run_id"])

        first_job = self.fixture.repository.get_media_job(first_job_id)
        second_job = self.fixture.repository.get_media_job(second_job_id)
        assert first_job is not None and second_job is not None
        self.assertEqual(
            (first_job["status"], first_job["stage"]),
            ("succeeded", "completed"),
        )
        self.assertEqual(
            (second_job["status"], second_job["stage"]),
            ("succeeded", "completed"),
        )
        first_outcome = self._shadow_outcome(first_job)
        second_outcome = self._shadow_outcome(second_job)
        self.assertEqual(
            (first_outcome["outcome"], first_outcome["reason_code"]),
            ("partial", "newer_head_published"),
        )
        self.assertEqual(
            first_outcome["outcome_sha256"],
            canonical_job_sha256(first_receipt),
        )
        self.assertEqual(
            (second_outcome["outcome"], second_outcome["reason_code"]),
            ("succeeded", None),
        )
        self.assertEqual(
            second_outcome["outcome_sha256"],
            canonical_job_sha256(second_receipt),
        )
        self.assertEqual(
            self._shadow_receipt_digest(first_job),
            first_receipt["receipt_sha256"],
        )
        self.assertEqual(
            self._shadow_receipt_digest(second_job),
            second_receipt["receipt_sha256"],
        )
        self.assertEqual(attempts[first_job_id][0], "finished")
        self.assertIsNotNone(attempts[first_job_id][1])
        self.assertEqual(attempts[second_job_id][0], "finished")
        self.assertIsNotNone(attempts[second_job_id][1])

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["analysis_binding"], second_binding)
        self.assertEqual(current["projection"]["status"], "current")

        counts_before_replay = self._projection_counts()
        first_replay = self._project(first_job_id)
        second_replay = self._project(second_job_id)

        self.assertIs(first_replay["replayed"], True)
        self.assertIs(second_replay["replayed"], True)
        self.assertEqual(first_replay["projection_receipt"], first_receipt)
        self.assertEqual(second_replay["projection_receipt"], second_receipt)
        self.assertEqual(self._projection_counts(), counts_before_replay)
        self.assertEqual(counts_before_replay, (1, 1, 1, 2))


if __name__ == "__main__":
    unittest.main()
