from __future__ import annotations

import hashlib
import os
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from core.db import ImageIndexRepository
from core.image_analysis_contract import seal_image_analysis_result
from core.image_analysis_job_contract import canonical_sha256
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import MediaRepository


RUNTIME_GENERATION = f"runtime_generation_{'a' * 64}"
RECOVERY_GENERATION = f"runtime_generation_{'b' * 64}"
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


class ImageAnalysisPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-image-a0-")
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
        key: str = "image-import-1",
        profile: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self.repository.enqueue_image_analysis(
            runtime_generation=RUNTIME_GENERATION,
            database_file_identity=self.database_identity,
            asset_id=self.asset_id,
            source_id=self.source_id,
            analysis_profile=profile or PROFILE,
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

    def _checkpoint_result_to_publish(
        self,
        *,
        job_id: str,
        runtime_generation: str,
        expected_attempt: int,
        result: dict[str, object],
    ) -> None:
        binding = self.repository.claim_image_analysis_attempt(
            job_id,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
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
                    {
                        "source_binding_sha256": binding[
                            "source_binding_sha256"
                        ]
                    }
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
                outcome_sha256 = canonical_sha256(stage_result)
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
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                expected_sequence=sequence,
                stage=stage,
                outcome=outcome,
                outcome_sha256=outcome_sha256,
                reason_code=reason_code,
                artifact_digests=artifact_digests,
            )

    def test_enqueue_is_exactly_bound_and_permanently_idempotent(self) -> None:
        first = self._enqueue()
        replay = self._enqueue()

        self.assertEqual(first["id"], replay["id"])
        self.assertFalse(first["reused"])
        self.assertTrue(replay["reused"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "analysis_runs",
                    "media_jobs",
                    "image_analysis_job_bindings",
                )
            )
        self.assertEqual(counts, (1, 1, 1))

        rebound_profile = {**PROFILE, "profile_version": "2"}
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_idempotency_conflict",
        ):
            self._enqueue(profile=rebound_profile)

    def test_caller_owned_enqueue_is_atomic_and_replay_does_not_mint_authority(self) -> None:
        with self.repository._open_admitted_image_source(
            source_id=self.source_id,
            expected_asset_id=self.asset_id,
        ) as pinned:
            with self.assertRaisesRegex(RuntimeError, "force_outer_rollback"):
                with self.repository.transaction(immediate=True) as connection:
                    self.repository.enqueue_image_analysis_in_transaction(
                        connection,
                        runtime_generation=RUNTIME_GENERATION,
                        database_file_identity=self.database_identity,
                        source_binding=pinned.binding,
                        asset_id=self.asset_id,
                        source_id=self.source_id,
                        analysis_profile=PROFILE,
                        expected_head=None,
                        enqueue_scope="media-import/image-analysis",
                        idempotency_key="caller-owned-import-1",
                    )
                    raise RuntimeError("force_outer_rollback")

            with self.repository.transaction(immediate=True) as connection:
                first = self.repository.enqueue_image_analysis_in_transaction(
                    connection,
                    runtime_generation=RUNTIME_GENERATION,
                    database_file_identity=self.database_identity,
                    source_binding=pinned.binding,
                    asset_id=self.asset_id,
                    source_id=self.source_id,
                    analysis_profile=PROFILE,
                    expected_head=None,
                    enqueue_scope="media-import/image-analysis",
                    idempotency_key="caller-owned-import-1",
                )
            with self.repository.transaction(immediate=True) as connection:
                replay = self.repository.enqueue_image_analysis_in_transaction(
                    connection,
                    runtime_generation=RUNTIME_GENERATION,
                    database_file_identity=self.database_identity,
                    source_binding=pinned.binding,
                    asset_id=self.asset_id,
                    source_id=self.source_id,
                    analysis_profile=PROFILE,
                    expected_head=None,
                    enqueue_scope="media-import/image-analysis",
                    idempotency_key="caller-owned-import-1",
                )

        self.assertFalse(first["reused"])
        self.assertTrue(replay["reused"])
        self.assertEqual(first["id"], replay["id"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "analysis_runs",
                    "media_jobs",
                    "image_analysis_job_bindings",
                    "image_analysis_attempt_authorities",
                    "image_analysis_attempt_states",
                )
            }
        self.assertEqual(set(counts.values()), {1})

    def test_publish_is_atomic_strictly_current_and_replay_safe(self) -> None:
        job = self._enqueue()
        result = self._sealed_result(str(job["id"]))

        published = self.repository.publish_image_analysis(
            job_id=str(job["id"]),
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )
        replay = self.repository.publish_image_analysis(
            job_id=str(job["id"]),
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )

        self.assertFalse(published["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            published["publish_receipt"],
            replay["publish_receipt"],
        )
        current = self.repository.get_current_image_analysis(self.asset_id)
        assert current is not None
        self.assertEqual(current["asset_id"], self.asset_id)
        self.assertEqual(current["analysis_binding"]["revision"], 1)
        self.assertEqual(current["projection"]["status"], "pending")
        stored_job = self.repository.get_media_job(str(job["id"]))
        assert stored_job is not None
        self.assertEqual((stored_job["status"], stored_job["stage"]), ("running", "shadow_projection"))
        artifact_names = {
            (item["stage"], item["name"])
            for item in stored_job["checkpoint"]["artifact_digests"]
        }
        self.assertIn(("metadata", "stage_output"), artifact_names)

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "image_analysis_results",
                    "image_analysis_heads",
                    "image_projection_changes",
                    "image_publish_receipts",
                )
            }
            generic_head = connection.execute(
                "SELECT analysis_run_id FROM asset_analysis_heads WHERE asset_id=?",
                (self.asset_id,),
            ).fetchone()
        self.assertEqual(set(counts.values()), {1})
        assert generic_head is not None
        self.assertEqual(generic_head["analysis_run_id"], current["analysis_binding"]["analysis_run_id"])

        rogue = b"rogue"
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "published image artifact set is closed",
        ):
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    """INSERT INTO image_analysis_artifacts(
                           asset_id,analysis_run_id,stage,name,media_type,signal,model_id,
                           dimensions,artifact_sha256,size_bytes,artifact_blob,published_at)
                       VALUES(?,?,'quality','rogue','application/octet-stream',NULL,NULL,
                              NULL,?,?,?,?)""",
                    (
                        self.asset_id,
                        current["analysis_binding"]["analysis_run_id"],
                        hashlib.sha256(rogue).hexdigest(),
                        len(rogue),
                        rogue,
                        "2026-01-01T00:00:00Z",
                    ),
                )

    def test_published_replay_requires_current_authority_source_and_artifacts(
        self,
    ) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        result = self._sealed_result(str(job["id"]))
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )
        before = self.repository.get_media_job(job_id)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_runtime_generation_changed",
        ):
            self.repository.publish_image_analysis(
                job_id=job_id,
                runtime_generation=RECOVERY_GENERATION,
                expected_attempt=99,
                result=result,
                artifacts=self._artifacts(),
            )
        self.assertEqual(self.repository.get_media_job(job_id), before)

        Image.new("RGB", (24, 12), "orange").save(self.image_path)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "UPDATE library_roots SET status='revoked' WHERE id=?",
                (self.root_id,),
            )

        reopened = MediaRepository(self.db_path)
        try:
            replayed_job = reopened.enqueue_image_analysis(
                runtime_generation=RECOVERY_GENERATION,
                database_file_identity=self.database_identity,
                asset_id=self.asset_id,
                source_id=self.source_id,
                analysis_profile=PROFILE,
                expected_head=None,
                enqueue_scope="media-import/image-analysis",
                idempotency_key="image-import-1",
            )
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "image_source_unavailable|image_source_changed",
            ):
                reopened.publish_image_analysis(
                    job_id=job_id,
                    runtime_generation=RUNTIME_GENERATION,
                    expected_attempt=1,
                    result=result,
                    artifacts=self._artifacts(),
                )
        finally:
            reopened.close()

        self.assertTrue(replayed_job["reused"])
        self.assertEqual(replayed_job["id"], job["id"])
        self.assertEqual(self.repository.get_media_job(job_id), before)

    def test_fresh_attempt_recovers_exact_publication_checkpoint_without_republish(
        self,
    ) -> None:
        job = self._enqueue(key="post-publish-recovery")
        job_id = str(job["id"])
        result = self._sealed_result(job_id)
        first = self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self._artifacts(),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            evidence_before = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
                for table in (
                    "image_analysis_results",
                    "image_publish_receipts",
                )
            ) + (
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_changes",
                ).fetchone()[0],
            )

        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        resumed = self.repository.resume_image_analysis_job(
            job_id,
            runtime_generation=RECOVERY_GENERATION,
            database_file_identity=self.database_identity,
            reason="process_recovery",
        )
        self.assertEqual(
            (resumed["status"], resumed["stage"], resumed["attempt"]),
            ("queued", "queued", 2),
        )
        self._checkpoint_result_to_publish(
            job_id=job_id,
            runtime_generation=RECOVERY_GENERATION,
            expected_attempt=2,
            result=result,
        )

        recovered = self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=RECOVERY_GENERATION,
            expected_attempt=2,
            result=result,
            artifacts=self._artifacts(),
        )
        recovered_job = self.repository.get_media_job(job_id)
        assert recovered_job is not None

        self.assertTrue(recovered["replayed"])
        self.assertEqual(recovered["publish_receipt"], first["publish_receipt"])
        self.assertEqual(
            (
                recovered_job["status"],
                recovered_job["stage"],
                recovered_job["attempt"],
            ),
            ("running", "shadow_projection", 2),
        )
        self.assertEqual(recovered_job["checkpoint"]["attempt"], 2)
        self.assertEqual(
            recovered_job["checkpoint"]["publish_binding"][
                "publish_receipt_sha256"
            ],
            first["publish_receipt"]["publish_receipt_sha256"],
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            evidence_after = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE job_id=?",
                    (job_id,),
                ).fetchone()[0]
                for table in (
                    "image_analysis_results",
                    "image_publish_receipts",
                )
            ) + (
                connection.execute(
                    "SELECT COUNT(*) FROM image_projection_changes",
                ).fetchone()[0],
            )
            attempts = connection.execute(
                """SELECT attempt,state,heartbeat_sequence
                     FROM image_analysis_attempt_states
                    WHERE job_id=? ORDER BY attempt""",
                (job_id,),
            ).fetchall()
        self.assertEqual(evidence_after, evidence_before)
        self.assertEqual(attempts, [(1, "interrupted", 2), (2, "claimed", 8)])

        projected = self.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=RECOVERY_GENERATION,
            expected_attempt=2,
        )
        self.assertEqual(projected["change_position"], first["change_position"])

    def test_two_runs_can_share_one_intended_revision_but_only_one_head_wins(self) -> None:
        first = self._enqueue(key="concurrent-a")
        second = self._enqueue(key="concurrent-b")
        first_binding = self.repository.get_image_analysis_job_binding(str(first["id"]))
        second_binding = self.repository.get_image_analysis_job_binding(str(second["id"]))
        assert first_binding is not None and second_binding is not None
        self.assertEqual(first_binding["intended_revision"], 1)
        self.assertEqual(second_binding["intended_revision"], 1)
        self.assertNotEqual(first_binding["run_revision"], second_binding["run_revision"])

        winner = self._sealed_result(str(second["id"]))
        self.repository.publish_image_analysis(
            job_id=str(second["id"]),
            runtime_generation=RUNTIME_GENERATION,
            expected_attempt=1,
            result=winner,
            artifacts=self._artifacts(),
        )
        loser = self._sealed_result(str(first["id"]))
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_head_conflict",
        ):
            self.repository.publish_image_analysis(
                job_id=str(first["id"]),
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                result=loser,
                artifacts=self._artifacts(),
            )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_analysis_results").fetchone()[0],
                1,
            )
            loser_status = connection.execute(
                "SELECT status FROM analysis_runs WHERE id=?",
                (first_binding["analysis_run_id"],),
            ).fetchone()[0]
        self.assertEqual(loser_status, "queued")

    def test_vector_artifact_shape_is_rejected_before_canonical_publish(self) -> None:
        job = self._enqueue()
        result = deepcopy(self._sealed_result(str(job["id"])))
        result.pop("content_sha256")
        vector = b"x"
        embedding_stage = b"embedding"
        result["stages"]["embedding"] = {
            "status": "succeeded",
            "provenance": _provenance(rule="embedding-router"),
            "output": {
                "combined_text_sha256": hashlib.sha256(b"").hexdigest(),
                "vectors": [
                    {
                        "purpose": "image_search",
                        "signal": "text_derived",
                        "model_id": "semantic_hash",
                        "dimensions": 3,
                        "artifact_sha256": hashlib.sha256(vector).hexdigest(),
                    }
                ],
            },
            "artifact_sha256": hashlib.sha256(embedding_stage).hexdigest(),
            "reason_code": None,
        }
        sealed = seal_image_analysis_result(result)
        artifacts = self._artifacts() + [
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(embedding_stage).hexdigest(),
                "bytes": embedding_stage,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/x-float32",
                "signal": "text_derived",
                "model_id": "semantic_hash",
                "dimensions": 3,
                "artifact_sha256": hashlib.sha256(vector).hexdigest(),
                "bytes": vector,
            },
        ]
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_vector_shape_mismatch",
        ):
            self.repository.publish_image_analysis(
                job_id=str(job["id"]),
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                result=sealed,
                artifacts=artifacts,
            )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "image_analysis_results",
                        "image_analysis_heads",
                        "image_projection_changes",
                    )
                ),
                (0, 0, 0),
            )

    def test_source_and_same_path_database_replacement_fail_before_publish(self) -> None:
        job = self._enqueue()
        Image.new("RGB", (24, 12), "orange").save(self.image_path)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_source_changed",
        ):
            self.repository.validate_image_analysis_job_admission(
                str(job["id"]),
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
            )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_analysis_results").fetchone()[0],
                0,
            )

        replacement = self.db_path.with_suffix(".replacement")
        with (
            closing(sqlite3.connect(self.db_path)) as source,
            closing(sqlite3.connect(replacement)) as target,
        ):
            source.backup(target)
        os.replace(replacement, self.db_path)
        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_database_scope_changed",
        ):
            self.repository.validate_image_analysis_job_admission(
                str(job["id"]),
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
            )

    def test_admitted_source_handle_starts_at_zero_after_digest_verification(self) -> None:
        with self.repository._open_admitted_image_source(
            source_id=self.source_id,
            expected_asset_id=self.asset_id,
            expected_input_sha256=self.asset_sha256,
        ) as source:
            self.assertEqual(source.handle.tell(), 0)
            payload = source.handle.read()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), self.asset_sha256)

    def test_enqueue_rechecks_root_authority_inside_the_write_transaction(self) -> None:
        original = self.repository._open_admitted_image_source

        @contextmanager
        def revoke_after_open(**kwargs: object):
            source = original(**kwargs)
            try:
                with closing(sqlite3.connect(self.db_path)) as connection, connection:
                    connection.execute(
                        "UPDATE library_roots SET status='revoked' WHERE id=?",
                        (self.root_id,),
                    )
                yield source
            finally:
                source.close()

        with patch.object(
            self.repository,
            "_open_admitted_image_source",
            new=revoke_after_open,
        ), self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_source_authority_changed",
        ):
            self._enqueue()

        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "analysis_runs",
                    "media_jobs",
                    "image_analysis_job_bindings",
                )
            )
        self.assertEqual(counts, (0, 0, 0))

    def test_publish_rechecks_root_authority_inside_the_write_transaction(self) -> None:
        job = self._enqueue()
        result = self._sealed_result(str(job["id"]))
        original = self.repository._open_admitted_image_source
        opens = 0

        @contextmanager
        def revoke_on_publish_open(**kwargs: object):
            nonlocal opens
            source = original(**kwargs)
            opens += 1
            try:
                if opens == 2:
                    with closing(sqlite3.connect(self.db_path)) as connection, connection:
                        connection.execute(
                            "UPDATE library_roots SET status='revoked' WHERE id=?",
                            (self.root_id,),
                        )
                yield source
            finally:
                source.close()

        with patch.object(
            self.repository,
            "_open_admitted_image_source",
            new=revoke_on_publish_open,
        ), self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_source_authority_changed",
        ):
            self.repository.publish_image_analysis(
                job_id=str(job["id"]),
                runtime_generation=RUNTIME_GENERATION,
                expected_attempt=1,
                result=result,
                artifacts=self._artifacts(),
            )

        with closing(sqlite3.connect(self.db_path)) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "image_analysis_results",
                    "image_analysis_heads",
                    "image_projection_changes",
                    "image_publish_receipts",
                )
            )
        self.assertEqual(counts, (0, 0, 0, 0))


if __name__ == "__main__":
    unittest.main()
