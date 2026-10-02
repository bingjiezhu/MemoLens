from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from PIL import Image

from backend.src.media.video import MediaJobRunner
from core.db import ImageIndexRepository
from core.image_analysis_contract import seal_image_analysis_result
from core.image_analysis_job_contract import (
    IMAGE_ANALYSIS_MAX_ATTEMPTS,
    ImageAnalysisJobContractError,
    canonical_sha256,
)
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import MediaRepository


GENERATION_A = f"runtime_generation_{'a' * 64}"
GENERATION_B = f"runtime_generation_{'b' * 64}"
PROFILE_SHA256 = hashlib.sha256(b"canonical-image-local-v1").hexdigest()
PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": PROFILE_SHA256,
}


def _provenance(*, rule: str) -> dict[str, object]:
    return {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": None,
        "model_version": None,
        "rule_id": rule,
        "rule_version": "1",
    }


def _disabled_stage(*, rule: str, reason: str) -> dict[str, object]:
    return {
        "status": "disabled",
        "provenance": _provenance(rule=rule),
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason,
    }


class ImageAttemptLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-image-attempt-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.image_path = self.library / "still.png"
        Image.new("RGB", (24, 12), "purple").save(self.image_path)
        payload = self.image_path.read_bytes()
        self.asset_sha256 = hashlib.sha256(payload).hexdigest()
        observed = self.image_path.stat()
        asset = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path=self.image_path.name,
            filename=self.image_path.name,
            kind="image",
            sha256=self.asset_sha256,
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.asset_id = str(asset["id"])
        self.source_id = str(asset["asset_source_id"])
        self.repository.update_image_probe(self.asset_id, width=24, height=12)
        database_stat = os.stat(self.db_path, follow_symlinks=False)
        self.database_identity = (database_stat.st_dev, database_stat.st_ino)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _enqueue(
        self,
        *,
        generation: str = GENERATION_A,
        key: str = "image-import-1",
    ) -> dict[str, object]:
        return self.repository.enqueue_image_analysis(
            runtime_generation=generation,
            database_file_identity=self.database_identity,
            asset_id=self.asset_id,
            source_id=self.source_id,
            analysis_profile=PROFILE,
            expected_head=None,
            enqueue_scope="media-import/image-analysis",
            idempotency_key=key,
        )

    def _sealed_result(self, job_id: str) -> dict[str, object]:
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        return seal_image_analysis_result(
            {
                "object": "memolens.image_analysis_result",
                "schema_version": "1",
                "asset_id": self.asset_id,
                "asset_sha256": self.asset_sha256,
                "analysis_run_id": binding["analysis_run_id"],
                "revision": binding["intended_revision"],
                "source_binding": {
                    "source_id": self.source_id,
                    "library_root_id": self.root_id,
                    "observed_size": binding["observed_size"],
                    "observed_mtime_ns": binding["observed_mtime_ns"],
                    "file_identity_sha256": binding["file_identity_sha256"],
                },
                "analysis_profile": {
                    "id": PROFILE["profile_id"],
                    "version": PROFILE["profile_version"],
                    "content_sha256": PROFILE["profile_sha256"],
                },
                "stages": {
                    "metadata": {
                        "status": "succeeded",
                        "provenance": _provenance(rule="pillow-probe"),
                        "output": {
                            "mime_type": "image/png",
                            "file_size": binding["observed_size"],
                            "width": 24,
                            "height": 12,
                            "taken_at": None,
                            "latitude": None,
                            "longitude": None,
                            "altitude": None,
                        },
                        "artifact_sha256": hashlib.sha256(b"metadata").hexdigest(),
                        "reason_code": None,
                    },
                    "geocode": _disabled_stage(
                        rule="network-policy",
                        reason="network_not_authorized",
                    ),
                    "vision": _disabled_stage(
                        rule="provider-policy",
                        reason="provider_not_authorized",
                    ),
                    "embedding": _disabled_stage(
                        rule="embedding-policy",
                        reason="embedding_not_configured",
                    ),
                    "quality": _disabled_stage(
                        rule="quality-policy",
                        reason="quality_not_configured",
                    ),
                },
            }
        )

    @staticmethod
    def _artifacts() -> list[dict[str, object]]:
        return [
            {
                "stage": "metadata",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(b"metadata").hexdigest(),
                "bytes": b"metadata",
            }
        ]

    @staticmethod
    def _fresh_checkpoint(job_id: str, attempt: int) -> dict[str, object]:
        return {
            "object": "memolens.image_analysis_checkpoint",
            "schema_version": "1",
            "worker_kind": "image_analysis",
            "job_id": job_id,
            "attempt": attempt,
            "current_stage": "queued",
            "completed_stages": [],
            "stage_outcomes": [],
            "artifact_digests": [],
            "publish_binding": None,
        }

    def _authority_documents(self, job_id: str) -> list[dict[str, object]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            rows = connection.execute(
                """SELECT authority_json FROM image_analysis_attempt_authorities
                    WHERE job_id=? ORDER BY attempt""",
                (job_id,),
            ).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def _attempt_states(self, job_id: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(
                """SELECT attempt,authority_sha256,state,heartbeat_sequence,
                          claimed_at,heartbeat_at,finished_at
                     FROM image_analysis_attempt_states
                    WHERE job_id=? ORDER BY attempt""",
                (job_id,),
            ).fetchall()

    def _run_row(self, run_id: str) -> dict[str, object]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM analysis_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        assert row is not None
        return dict(row)

    def _mutation_snapshot(self, job_id: str) -> dict[str, object]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            job = connection.execute(
                """SELECT status,stage,progress,attempt,cancel_requested,
                          checkpoint_json,error_json,started_at,heartbeat_at,finished_at
                     FROM media_jobs WHERE id=?""",
                (job_id,),
            ).fetchone()
            published_counts = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
                for table in (
                    "image_analysis_results",
                    "image_publish_receipts",
                )
            )
        assert job is not None
        return {
            "job": job,
            "authorities": self._authority_documents(job_id),
            "attempt_states": self._attempt_states(job_id),
            "published_counts": published_counts,
        }

    def _retryable_failure_at_attempt_limit(
        self,
        *,
        key: str,
        fail_last_attempt: bool = True,
    ) -> str:
        job = self._enqueue(key=key)
        job_id = str(job["id"])
        final_failure = (
            IMAGE_ANALYSIS_MAX_ATTEMPTS
            if fail_last_attempt
            else IMAGE_ANALYSIS_MAX_ATTEMPTS - 1
        )
        for attempt in range(1, final_failure + 1):
            generation = GENERATION_A if attempt == 1 else GENERATION_B
            self.assertTrue(
                self.repository.fail_image_analysis_attempt(
                    job_id,
                    runtime_generation=generation,
                    expected_attempt=attempt,
                    code="stage_failed",
                    stage="queued",
                    retryable=True,
                    detail="bounded_retry_fixture",
                )
            )
            if attempt < IMAGE_ANALYSIS_MAX_ATTEMPTS:
                resumed = self.repository.resume_image_analysis_job(
                    job_id,
                    runtime_generation=GENERATION_B,
                    database_file_identity=self.database_identity,
                    reason="process_recovery",
                )
                self.assertEqual(resumed["attempt"], attempt + 1)
        return job_id

    def test_idempotent_replay_is_generation_stable_without_minting_authority(self) -> None:
        first = self._enqueue(generation=GENERATION_A)
        job_id = str(first["id"])
        first_binding = self.repository.get_image_analysis_job_binding(job_id)
        assert first_binding is not None
        first_authorities = self._authority_documents(job_id)

        replay = self._enqueue(generation=GENERATION_B)
        replay_binding = self.repository.get_image_analysis_job_binding(job_id)
        assert replay_binding is not None

        self.assertFalse(first["reused"])
        self.assertTrue(replay["reused"])
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(replay_binding["request_sha256"], first_binding["request_sha256"])
        self.assertNotIn(
            "runtime_generation",
            first_binding["request"]["database_binding"],
        )
        self.assertEqual(self._authority_documents(job_id), first_authorities)
        self.assertEqual(len(first_authorities), 1)
        self.assertEqual(
            (
                first_authorities[0]["attempt"],
                first_authorities[0]["runtime_generation"],
                first_authorities[0]["request_sha256"],
            ),
            (1, GENERATION_A, first_binding["request_sha256"]),
        )
        admitted = self.repository.validate_image_analysis_job_admission(
            job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
        )
        self.assertEqual(admitted["attempt_authority"], first_authorities[0])
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_runtime_generation_changed",
        ):
            self.repository.validate_image_analysis_job_admission(
                job_id,
                runtime_generation=GENERATION_B,
                expected_attempt=1,
            )
        self.assertEqual(self._authority_documents(job_id), first_authorities)

    def test_resume_atomically_revokes_old_attempt_and_binds_exact_predecessor(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        initial_binding = self.repository.get_image_analysis_job_binding(job_id)
        assert initial_binding is not None
        first_authority = initial_binding["attempt_authority"]

        claimed = self.repository.claim_image_analysis_attempt(
            job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
        )
        self.assertEqual(
            (claimed["attempt_state"], claimed["heartbeat_sequence"], claimed["job_stage"]),
            ("claimed", 1, "source_admission"),
        )
        self.assertEqual(
            self.repository.heartbeat_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                expected_sequence=1,
            ),
            2,
        )
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_heartbeat_conflict",
        ):
            self.repository.heartbeat_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                expected_sequence=1,
            )
        self.assertEqual(
            self.repository.heartbeat_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                expected_sequence=2,
            ),
            3,
        )
        before_interrupt = self.repository.get_media_job(job_id)
        assert before_interrupt is not None
        prior_checkpoint = before_interrupt["checkpoint"]
        self.assertTrue(
            self.repository.interrupt_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                detail="runtime_handoff",
            )
        )
        interrupted = self.repository.get_media_job(job_id)
        assert interrupted is not None
        self.assertEqual(
            (interrupted["status"], interrupted["stage"], interrupted["attempt"]),
            ("interrupted", "source_admission", 1),
        )
        self.assertEqual(interrupted["checkpoint"], prior_checkpoint)

        resumed = self.repository.resume_image_analysis_job(
            job_id,
            runtime_generation=GENERATION_B,
            database_file_identity=self.database_identity,
        )
        expected_checkpoint = self._fresh_checkpoint(job_id, 2)
        second_authority = resumed["attempt_authority"]
        self.assertEqual(
            (resumed["status"], resumed["stage"], resumed["attempt"]),
            ("queued", "queued", 2),
        )
        self.assertEqual(resumed["checkpoint"], expected_checkpoint)
        self.assertEqual(
            second_authority["start_checkpoint_sha256"],
            canonical_sha256(expected_checkpoint),
        )
        self.assertEqual(
            second_authority["predecessor"],
            {
                "attempt": 1,
                "status": "interrupted",
                "stage": "source_admission",
                "checkpoint_sha256": canonical_sha256(prior_checkpoint),
                "authority_sha256": first_authority["authority_sha256"],
            },
        )
        self.assertEqual(
            self._authority_documents(job_id),
            [first_authority, second_authority],
        )
        states = self._attempt_states(job_id)
        self.assertEqual(
            [(row[0], row[2], row[3]) for row in states],
            [(1, "interrupted", 3), (2, "queued", 0)],
        )

        stale_result = self._sealed_result(job_id)
        mutation_snapshot = self._mutation_snapshot(job_id)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_runtime_generation_changed",
        ):
            self.repository.claim_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
            )
        self.assertEqual(self._mutation_snapshot(job_id), mutation_snapshot)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_heartbeat_conflict",
        ):
            self.repository.heartbeat_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                expected_sequence=3,
            )
        self.assertEqual(self._mutation_snapshot(job_id), mutation_snapshot)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_runtime_generation_changed",
        ):
            self.repository.publish_image_analysis(
                job_id=job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                result=stale_result,
                artifacts=self._artifacts(),
            )
        self.assertEqual(self._mutation_snapshot(job_id), mutation_snapshot)

    def test_process_recovery_after_publish_preserves_succeeded_run(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        run_id = str(binding["analysis_run_id"])
        result = self._sealed_result(job_id)
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )
        published_job = self.repository.get_media_job(job_id)
        assert published_job is not None
        published_checkpoint = published_job["checkpoint"]
        published_run = self._run_row(run_id)
        self.assertEqual(
            (published_job["status"], published_job["stage"]),
            ("running", "shadow_projection"),
        )
        self.assertEqual(published_run["status"], "succeeded")

        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        interrupted = self.repository.get_media_job(job_id)
        assert interrupted is not None
        self.assertEqual(
            (interrupted["status"], interrupted["stage"], interrupted["attempt"]),
            ("interrupted", "shadow_projection", 1),
        )
        self.assertEqual(interrupted["checkpoint"], published_checkpoint)
        self.assertEqual(self._run_row(run_id), published_run)

        resumed = self.repository.resume_image_analysis_job(
            job_id,
            runtime_generation=GENERATION_B,
            database_file_identity=self.database_identity,
            reason="process_recovery",
        )
        self.assertEqual(
            (resumed["status"], resumed["stage"], resumed["attempt"]),
            ("queued", "queued", 2),
        )
        self.assertEqual(
            resumed["attempt_authority"]["predecessor"]["checkpoint_sha256"],
            canonical_sha256(published_checkpoint),
        )
        self.assertEqual(self._run_row(run_id), published_run)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_analysis_results WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0],
                1,
            )

    def test_publish_rejects_a_checkpoint_that_does_not_bind_final_result(
        self,
    ) -> None:
        job = self._enqueue(key="checkpoint-result-mismatch")
        job_id = str(job["id"])
        result = self._sealed_result(job_id)
        binding = self.repository.claim_image_analysis_attempt(
            job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
        )
        sequence = int(binding["heartbeat_sequence"])
        stages = result["stages"]
        assert type(stages) is dict
        for stage in (
            "source_admission",
            "metadata",
            "geocode",
            "vision",
            "embedding",
            "quality",
        ):
            if stage == "source_admission":
                outcome = "succeeded"
                outcome_sha256 = canonical_sha256(
                    {"source_binding_sha256": binding["source_binding_sha256"]}
                )
                reason_code = None
                artifact_digests = (
                    {
                        "stage": stage,
                        "name": "file_identity",
                        "sha256": binding["file_identity_sha256"],
                    },
                )
            else:
                stage_result = stages[stage]
                assert type(stage_result) is dict
                outcome = str(stage_result["status"])
                outcome_sha256 = (
                    "f" * 64
                    if stage == "metadata"
                    else canonical_sha256(stage_result)
                )
                reason_code = (
                    str(stage_result["reason_code"])
                    if stage_result["reason_code"] is not None
                    else None
                )
                artifact_digests = (
                    (
                        {
                            "stage": "metadata",
                            "name": "stage_output",
                            "sha256": hashlib.sha256(b"metadata").hexdigest(),
                        },
                    )
                    if stage == "metadata"
                    else ()
                )
            sequence = self.repository.checkpoint_image_analysis_stage(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                expected_sequence=sequence,
                stage=stage,
                outcome=outcome,
                outcome_sha256=outcome_sha256,
                reason_code=reason_code,
                artifact_digests=artifact_digests,
            )

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_checkpoint_result_mismatch",
        ):
            self.repository.publish_image_analysis(
                job_id=job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                result=result,
                artifacts=self._artifacts(),
            )
        self.assertEqual(
            self._mutation_snapshot(job_id)["published_counts"],
            (0, 0),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM image_analysis_heads WHERE asset_id=?",
                    (self.asset_id,),
                ).fetchone()[0],
                0,
            )

    def test_retryable_exact_failure_can_resume_under_a_new_authority(self) -> None:
        job = self._enqueue(key="retryable-worker-failure")
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        run_id = str(binding["analysis_run_id"])
        self.repository.claim_image_analysis_attempt(
            job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
        )

        self.assertTrue(
            self.repository.fail_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                code="stage_failed",
                stage="source_admission",
                retryable=True,
                detail="pinned_source_changed",
            )
        )
        failed = self.repository.get_media_job(job_id)
        assert failed is not None
        self.assertEqual(
            (failed["status"], failed["stage"], failed["error"]["code"]),
            ("failed", "source_admission", "stage_failed"),
        )
        self.assertEqual(self._attempt_states(job_id)[0][2], "finished")
        self.assertEqual(self._run_row(run_id)["status"], "failed")

        resumed = self.repository.resume_image_analysis_job(
            job_id,
            runtime_generation=GENERATION_B,
            database_file_identity=self.database_identity,
        )
        self.assertEqual(
            (resumed["status"], resumed["attempt"]),
            ("queued", 2),
        )
        self.assertEqual(
            resumed["attempt_authority"]["predecessor"]["status"],
            "failed",
        )
        self.assertEqual(
            [(row[0], row[2]) for row in self._attempt_states(job_id)],
            [(1, "finished"), (2, "queued")],
        )

    def test_attempt_one_hundred_exhausts_retry_budget_with_zero_write(self) -> None:
        job_id = self._retryable_failure_at_attempt_limit(
            key="bounded-retry-limit"
        )
        before = self._mutation_snapshot(job_id)

        for _ in range(2):
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "^image_analysis_retry_budget_exhausted$",
            ):
                self.repository.resume_image_analysis_job(
                    job_id,
                    runtime_generation=GENERATION_B,
                    database_file_identity=self.database_identity,
                    reason="process_recovery",
                )

        self.assertEqual(self._mutation_snapshot(job_id), before)
        self.assertEqual(len(before["authorities"]), IMAGE_ANALYSIS_MAX_ATTEMPTS)

    def test_startup_recovery_at_attempt_limit_stays_explicit_and_zero_write(self) -> None:
        job_id = self._retryable_failure_at_attempt_limit(
            key="bounded-retry-startup",
            fail_last_attempt=False,
        )
        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        before = self._mutation_snapshot(job_id)
        runner = MediaJobRunner(
            self.repository,
            self.root / "bounded-retry-recovery-cache",
        )
        runner.activate_runtime_generation(
            GENERATION_B,
            active_library_root_id=self.root_id,
        )
        try:
            runner.recover()
        finally:
            runner.shutdown()

        observed = self.repository.get_media_job(job_id)
        assert observed is not None
        self.assertEqual(
            (observed["status"], observed["attempt"], observed["error"]["code"]),
            ("interrupted", IMAGE_ANALYSIS_MAX_ATTEMPTS, "worker_interrupted"),
        )
        self.assertEqual(self._mutation_snapshot(job_id), before)

    def test_concurrent_resume_at_attempt_limit_is_stable_and_zero_write(self) -> None:
        job_id = self._retryable_failure_at_attempt_limit(
            key="bounded-retry-concurrent"
        )
        before = self._mutation_snapshot(job_id)
        barrier = threading.Barrier(3)
        errors: list[str] = []

        def resume() -> None:
            repository = MediaRepository(self.db_path)
            try:
                barrier.wait()
                repository.resume_image_analysis_job(
                    job_id,
                    runtime_generation=GENERATION_B,
                    database_file_identity=self.database_identity,
                    reason="process_recovery",
                )
            except ImageAnalysisPersistenceError as exc:
                errors.append(str(exc))
            finally:
                repository.close()

        threads = [threading.Thread(target=resume) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=10)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(
            sorted(errors),
            [
                "image_analysis_retry_budget_exhausted",
                "image_analysis_retry_budget_exhausted",
            ],
        )
        self.assertEqual(self._mutation_snapshot(job_id), before)

    def test_preclaim_nonretryable_failure_finishes_queued_authority(self) -> None:
        job = self._enqueue(key="preclaim-source-failure")
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        run_id = str(binding["analysis_run_id"])

        self.assertTrue(
            self.repository.fail_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
                code="source_changed",
                stage="queued",
                retryable=False,
                detail="source_changed_before_claim",
            )
        )
        failed = self.repository.get_media_job(job_id)
        assert failed is not None
        self.assertEqual(
            (failed["status"], failed["stage"], failed["error"]["code"]),
            ("failed", "queued", "source_changed"),
        )
        self.assertEqual(self._attempt_states(job_id)[0][2], "finished")
        self.assertEqual(self._run_row(run_id)["status"], "failed")
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_job_not_resumable",
        ):
            self.repository.resume_image_analysis_job(
                job_id,
                runtime_generation=GENERATION_B,
                database_file_identity=self.database_identity,
            )

    def test_running_cancel_is_finished_only_by_exact_attempt_authority(self) -> None:
        job = self._enqueue(key="running-cancel")
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        run_id = str(binding["analysis_run_id"])
        self.repository.claim_image_analysis_attempt(
            job_id,
            runtime_generation=GENERATION_A,
            expected_attempt=1,
        )
        self.assertTrue(self.repository.request_media_job_cancel(job_id))

        cancelling = self.repository.get_media_job(job_id)
        assert cancelling is not None
        self.assertEqual(cancelling["status"], "cancelling")
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_runtime_generation_changed",
        ):
            self.repository.cancel_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_B,
                expected_attempt=1,
            )
        self.assertTrue(
            self.repository.cancel_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
            )
        )
        cancelled = self.repository.get_media_job(job_id)
        assert cancelled is not None
        self.assertEqual(
            (
                cancelled["status"],
                cancelled["stage"],
                cancelled["cancel_requested"],
                cancelled["error"]["code"],
            ),
            ("cancelled", "source_admission", True, "cancelled"),
        )
        self.assertEqual(self._attempt_states(job_id)[0][2], "cancelled")
        self.assertEqual(self._run_row(run_id)["status"], "cancelled")
        self.assertFalse(
            self.repository.cancel_image_analysis_attempt(
                job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
            )
        )

    def test_invalid_generation_and_nonretryable_job_append_no_authority(self) -> None:
        invalid_job = self._enqueue(key="invalid-generation")
        invalid_job_id = str(invalid_job["id"])
        self.assertTrue(
            self.repository.interrupt_image_analysis_attempt(
                invalid_job_id,
                runtime_generation=GENERATION_A,
                expected_attempt=1,
            )
        )
        invalid_snapshot = self._mutation_snapshot(invalid_job_id)
        with self.assertRaises(ImageAnalysisJobContractError):
            self.repository.resume_image_analysis_job(
                invalid_job_id,
                runtime_generation=f"runtime_generation_{'g' * 64}",
                database_file_identity=self.database_identity,
            )
        self.assertEqual(self._mutation_snapshot(invalid_job_id), invalid_snapshot)

        cancelled_job = self._enqueue(key="nonretryable-cancel")
        cancelled_job_id = str(cancelled_job["id"])
        self.assertTrue(self.repository.request_media_job_cancel(cancelled_job_id))
        cancelled = self.repository.get_media_job(cancelled_job_id)
        assert cancelled is not None
        self.assertEqual(
            (
                cancelled["status"],
                cancelled["cancel_requested"],
                cancelled["error"]["retryable"],
            ),
            ("cancelled", True, False),
        )
        cancelled_snapshot = self._mutation_snapshot(cancelled_job_id)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_job_not_resumable",
        ):
            self.repository.resume_image_analysis_job(
                cancelled_job_id,
                runtime_generation=GENERATION_B,
                database_file_identity=self.database_identity,
            )
        self.assertEqual(self._mutation_snapshot(cancelled_job_id), cancelled_snapshot)


if __name__ == "__main__":
    unittest.main()
