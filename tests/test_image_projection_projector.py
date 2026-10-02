from __future__ import annotations

from contextlib import contextmanager
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import render_projected_image_index_row
from tests import test_image_projection_schema_guards as schema_guards


class _ManifestInsertFailureConnection:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        if "INSERT INTO image_projection_manifests" in statement:
            raise sqlite3.OperationalError("injected image projection manifest failure")
        return self._connection.execute(statement, parameters)

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


class ImageProjectionProjectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(methodName="runTest")
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _project(self) -> dict[str, object]:
        return self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
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

    def _assert_publication_survived_projection_failure(self) -> None:
        with self.fixture.repository.transaction() as connection:
            publication = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_analysis_results) AS results,
                     (SELECT COUNT(*) FROM image_analysis_heads) AS heads,
                     (SELECT COUNT(*) FROM image_publish_receipts) AS publish_receipts"""
            ).fetchone()
            job = connection.execute(
                """SELECT status,stage,attempt FROM media_jobs WHERE id=?""",
                (self.fixture.job_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT state FROM image_analysis_attempt_states
                    WHERE job_id=? AND attempt=1""",
                (self.fixture.job_id,),
            ).fetchone()
        assert publication is not None and job is not None and attempt is not None
        self.assertEqual(tuple(publication), (1, 1, 1))
        self.assertEqual(tuple(job), ("running", "shadow_projection", 1))
        self.assertEqual(attempt["state"], "claimed")

    def test_single_call_atomically_projects_activates_and_finalizes_job(self) -> None:
        self.assertEqual(self._projection_counts(), (0, 0, 0, 0))

        projected = self._project()

        self.assertIs(projected["replayed"], False)
        self.assertEqual(self._projection_counts(), (1, 1, 1, 1))
        receipt = projected["projection_receipt"]
        assert isinstance(receipt, dict)
        with self.fixture.repository.transaction() as connection:
            generation = connection.execute(
                """SELECT status,is_active FROM image_projection_generations
                    WHERE id=?""",
                (projected["generation_id"],),
            ).fetchone()
            job = connection.execute(
                """SELECT status,stage,progress,attempt,checkpoint_json
                     FROM media_jobs WHERE id=?""",
                (self.fixture.job_id,),
            ).fetchone()
            attempt = connection.execute(
                """SELECT state,finished_at FROM image_analysis_attempt_states
                    WHERE job_id=? AND attempt=1""",
                (self.fixture.job_id,),
            ).fetchone()
        assert generation is not None and job is not None and attempt is not None
        self.assertEqual(tuple(generation), ("complete", 1))
        self.assertEqual(
            (job["status"], job["stage"], job["progress"]),
            ("succeeded", "completed", 1.0),
        )
        self.assertEqual(attempt["state"], "finished")
        self.assertIsNotNone(attempt["finished_at"])
        checkpoint = json.loads(str(job["checkpoint_json"]))
        self.assertEqual(checkpoint["current_stage"], "completed")
        self.assertEqual(checkpoint["completed_stages"][-1], "shadow_projection")
        self.assertEqual(
            checkpoint["publish_binding"]["projection_receipt_sha256"],
            receipt["receipt_sha256"],
        )

        current = self.fixture.repository.get_current_image_analysis(self.fixture.asset_id)
        self.assertIsNotNone(current)
        assert current is not None
        self.assertEqual(current["projection"]["status"], "current")
        self.assertEqual(
            current["projection"]["receipt_sha256"],
            receipt["receipt_sha256"],
        )
        self.assertEqual(
            current["projection"]["generation_id"],
            projected["generation_id"],
        )

    def test_replay_returns_same_receipt_without_new_projection_rows(self) -> None:
        first = self._project()
        counts_after_first = self._projection_counts()

        replay = self._project()

        self.assertIs(first["replayed"], False)
        self.assertIs(replay["replayed"], True)
        self.assertEqual(replay["projection_receipt"], first["projection_receipt"])
        self.assertEqual(replay["generation_id"], first["generation_id"])
        self.assertEqual(replay["manifest"], first["manifest"])
        self.assertEqual(self._projection_counts(), counts_after_first)
        self.assertEqual(counts_after_first, (1, 1, 1, 1))

    def test_replay_rejects_wrong_runtime_and_attempt_without_writes(self) -> None:
        self._project()
        counts = self._projection_counts()

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_runtime_generation_changed$",
        ):
            self.fixture.repository.project_image_analysis_change(
                job_id=self.fixture.job_id,
                runtime_generation=f"runtime_generation_{'b' * 64}",
                expected_attempt=1,
            )
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_analysis_attempt_changed$",
        ):
            self.fixture.repository.project_image_analysis_change(
                job_id=self.fixture.job_id,
                runtime_generation=schema_guards.RUNTIME_GENERATION,
                expected_attempt=999,
            )

        self.assertEqual(self._projection_counts(), counts)

    def test_replay_rejects_revoked_root_without_projection_writes(self) -> None:
        self._project()
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE library_roots SET status='revoked' WHERE id=?",
                (self.fixture.root_id,),
            )
        counts = self._projection_counts()

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_source_unavailable$",
        ):
            self._project()

        self.assertEqual(self._projection_counts(), counts)

    def test_replay_rejects_same_path_source_replacement_without_projection_writes(
        self,
    ) -> None:
        self._project()
        source_path = self.fixture.library / "guard.png"
        replacement = self.fixture.library / "replacement.png"
        replacement.write_bytes(b"replacement image bytes")
        os.replace(replacement, source_path)
        counts = self._projection_counts()

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_source_changed$",
        ):
            self._project()

        self.assertEqual(self._projection_counts(), counts)

    def test_manifest_failure_rolls_back_only_projection_transaction(self) -> None:
        repository = self.fixture.repository
        real_transaction = repository.transaction

        @contextmanager
        def failing_transaction(*, immediate: bool = False):
            with real_transaction(immediate=immediate) as connection:
                yield _ManifestInsertFailureConnection(connection)

        with patch.object(repository, "transaction", failing_transaction):
            with self.assertRaisesRegex(
                sqlite3.OperationalError,
                "injected image projection manifest failure",
            ):
                self._project()

        self.assertEqual(self._projection_counts(), (0, 0, 0, 0))
        self._assert_publication_survived_projection_failure()

    def test_self_consistent_wrong_source_projection_is_rejected(self) -> None:
        wrong_document = render_projected_image_index_row(
            result=self.fixture._sealed_result(),
            source_projection={
                "filename": "guard.png",
                "relative_path": "wrong/guard.png",
            },
            artifacts=self.fixture._artifacts(),
            projector_version=schema_guards.PROJECTOR_VERSION,
        )
        wrong_row = wrong_document["row"]
        assert isinstance(wrong_row, dict)
        self.assertEqual(wrong_row["relative_path"], "wrong/guard.png")

        with patch(
            "core.image_analysis_persistence.render_projected_image_index_row",
            return_value=wrong_document,
        ):
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "image_analysis_projection_(?:blocked|corrupt)",
            ):
                self._project()

        self.assertEqual(self._projection_counts(), (0, 0, 0, 0))
        self._assert_publication_survived_projection_failure()


if __name__ == "__main__":
    unittest.main()
