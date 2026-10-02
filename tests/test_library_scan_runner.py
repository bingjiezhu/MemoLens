from __future__ import annotations

from copy import deepcopy
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from flask import Flask

from backend.src.api.routes import api_blueprint
from backend.src.media.library_scan import (
    LibraryScanCheckpoint,
    LibraryScanContractError,
    LibraryScanRunner,
    discover_library_scan_batch,
    public_library_scan_job,
)
from backend.src.media.video import MediaJobRunner
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
from indexing.files import open_pinned_library_root


FINGERPRINT = "a" * 64
JOB_ID = f"job_{'b' * 32}"
CREATED_AT = "2026-08-30T00:00:00+00:00"
GENERATION_A = f"runtime_generation_{'d' * 64}"
GENERATION_B = f"runtime_generation_{'e' * 64}"


class _InjectedCrash(BaseException):
    pass


def _checkpoint(
    *,
    last_relative_path: str = "still.jpg",
    processed: int = 1,
) -> dict[str, object]:
    return {
        "object": "memolens.library_scan_checkpoint",
        "schema_version": "1",
        "root_permission_fingerprint": FINGERPRINT,
        "last_relative_path": last_relative_path,
        "processed_count": processed,
        "imported_count": processed,
        "skipped_count": 0,
        "rejected_count": 0,
        "child_job_count": processed,
        "batch_count": 1,
    }


def _job() -> dict[str, object]:
    return {
        "id": JOB_ID,
        "database_uuid": "12345678-1234-4123-8123-123456789abc",
        "kind": "library_scan",
        "asset_id": None,
        "analysis_run_id": None,
        "status": "running",
        "stage": "scanning",
        "progress": 0.0,
        "attempt": 1,
        "cancel_requested": False,
        "checkpoint": _checkpoint(),
        "error": None,
        "created_at": CREATED_AT,
        "started_at": CREATED_AT,
        "heartbeat_at": CREATED_AT,
        "finished_at": None,
        "root_path": "/private/library",
        "cursor": "still.jpg",
    }


class LibraryScanPureContractTests(unittest.TestCase):
    def test_descriptor_discovery_is_globally_utf8_ordered_and_advances_rejections(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-library-scan-order-") as directory:
            library = Path(directory).resolve()
            (library / "a").mkdir()
            (library / "a" / "child.jpg").write_bytes(b"child")
            (library / "a.jpg").write_bytes(b"root")
            (library / "b.mp4").symlink_to(library / "a.jpg")
            (library / "c.jpg").mkdir()
            (library / "c.jpg" / "inner.png").write_bytes(b"nested")
            (library / "ignored.txt").write_text("ignored", encoding="utf-8")

            with open_pinned_library_root(library) as root:
                first = discover_library_scan_batch(root, last_relative_path=None, batch_size=2)
                second = discover_library_scan_batch(
                    root,
                    last_relative_path=first.relative_paths[-1],
                    batch_size=2,
                )
                third = discover_library_scan_batch(
                    root,
                    last_relative_path=second.relative_paths[-1],
                    batch_size=2,
                )

        entries = [*first.entries, *second.entries, *third.entries]
        self.assertEqual(
            [(entry.relative_path, entry.importable) for entry in entries],
            [
                ("a.jpg", True),
                ("a/child.jpg", True),
                ("b.mp4", False),
                ("c.jpg", False),
                ("c.jpg/inner.png", True),
            ],
        )
        self.assertTrue(first.has_more)
        self.assertTrue(second.has_more)
        self.assertFalse(third.has_more)

    def test_checkpoint_is_closed_normalized_and_count_bounded(self) -> None:
        checkpoint = LibraryScanCheckpoint.from_mapping(_checkpoint())
        self.assertEqual(checkpoint.to_dict(), _checkpoint())
        for mutation in (
            {"unknown": True},
            {"last_relative_path": "../escape.jpg"},
            {"processed_count": 0},
            {"imported_count": 2},
            {"child_job_count": 2},
            {"batch_count": 2},
        ):
            with self.subTest(mutation=mutation):
                invalid = {**_checkpoint(), **mutation}
                with self.assertRaisesRegex(
                    LibraryScanContractError,
                    "library_scan_checkpoint_invalid",
                ):
                    LibraryScanCheckpoint.from_mapping(invalid)

    def test_public_projection_is_exact_and_path_free(self) -> None:
        public = public_library_scan_job(_job())
        self.assertEqual(
            set(public),
            {
                "object",
                "schema_version",
                "id",
                "kind",
                "status",
                "stage",
                "progress",
                "attempt",
                "cancel_requested",
                "resumable",
                "created_at",
                "started_at",
                "heartbeat_at",
                "finished_at",
                "counts",
                "no_supported_media",
                "eta_seconds",
                "error",
            },
        )
        self.assertEqual(public["object"], "memolens.library_scan_job")
        self.assertEqual(
            public["counts"],
            {
                "processed": 1,
                "imported": 1,
                "skipped": 0,
                "rejected": 0,
                "child_jobs": 1,
                "batches": 1,
            },
        )
        self.assertIsNone(public["eta_seconds"])
        self.assertIsNone(public["error"])
        serialized = repr(public)
        for forbidden in (
            "/private/library",
            "still.jpg",
            "checkpoint",
            "database_uuid",
            "asset_id",
            "analysis_run_id",
            "fingerprint",
            "cursor",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_public_projection_rejects_open_state_error_and_timestamp_unions(self) -> None:
        cases: list[dict[str, object]] = []
        bad_code = _job()
        bad_code.update(
            status="failed",
            stage="failed",
            finished_at=CREATED_AT,
            error={
                "object": "memolens.library_scan_error",
                "schema_version": "1",
                "code": "path_leaked",
                "stage": "scanning",
                "retryable": False,
                "detail_sha256": "c" * 64,
            },
        )
        cases.append(bad_code)
        retryability = deepcopy(bad_code)
        retryability["error"] = {
            **bad_code["error"],  # type: ignore[arg-type]
            "code": "batch_failed",
            "retryable": False,
        }
        cases.append(retryability)
        initial_running = _job()
        initial_running["checkpoint"] = {}
        cases.append(initial_running)
        bad_progress = _job()
        bad_progress["progress"] = float("nan")
        cases.append(bad_progress)
        bad_attempt = _job()
        bad_attempt["attempt"] = 9_007_199_254_740_992
        cases.append(bad_attempt)
        bool_attempt = _job()
        bool_attempt["attempt"] = True
        cases.append(bool_attempt)
        bad_time = _job()
        bad_time["heartbeat_at"] = "/private/library"
        cases.append(bad_time)
        non_utc = _job()
        non_utc["heartbeat_at"] = "2026-08-30T08:00:00+08:00"
        cases.append(non_utc)
        reversed_time = _job()
        reversed_time["heartbeat_at"] = "2026-08-29T23:59:59+00:00"
        cases.append(reversed_time)

        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(LibraryScanContractError):
                    public_library_scan_job(case)

    def test_preclaim_nonretryable_failure_does_not_invent_started_timestamp(self) -> None:
        failed = _job()
        failed.update(
            status="failed",
            stage="failed",
            progress=0.0,
            started_at=None,
            heartbeat_at=CREATED_AT,
            finished_at=CREATED_AT,
            checkpoint=LibraryScanCheckpoint.initial(root_permission_fingerprint=FINGERPRINT).to_dict(),
            error={
                "object": "memolens.library_scan_error",
                "schema_version": "1",
                "code": "root_binding_changed",
                "stage": "awaiting_scan_worker",
                "retryable": False,
                "detail_sha256": "c" * 64,
            },
        )

        public = public_library_scan_job(failed)

        self.assertIsNone(public["started_at"])
        self.assertEqual(
            public["error"],
            {"code": "root_binding_changed", "retryable": False},
        )
        self.assertFalse(public["resumable"])


class LibraryScanRunnerIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-library-scan-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(None)
        descriptor = os.open(
            self.library,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        identity = os.fstat(descriptor)
        now_ms = int(time.time() * 1000)
        try:
            committed = self.repository.commit_library_bootstrap(
                request_id=f"lb_{'a' * 64}",
                intent_created_at_ms=now_ms - 1_000,
                intent_expires_at_ms=now_ms + 299_000,
                native_confirmed_at_ms=now_ms,
                candidate_binding_sha256=self.repository.library_bootstrap_candidate_binding_sha256(
                    canonical_root=self.library,
                    expected_library_root_device=int(identity.st_dev),
                    expected_library_root_inode=int(identity.st_ino),
                ),
                library_root_path=self.library,
                library_root_fd=descriptor,
                expected_library_root_device=int(identity.st_dev),
                expected_library_root_inode=int(identity.st_ino),
            )
        finally:
            os.close(descriptor)
        self.assertEqual(committed["response"]["status"], "library_authority_committed")
        context = self.repository.list_recoverable_library_scans()[0]
        self.job_id = str(context["job_id"])
        self.root_id = str(context["library_root_id"])
        self.runners: list[MediaJobRunner] = []

    def tearDown(self) -> None:
        for runner in reversed(self.runners):
            runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def _processor(
        self,
        *,
        fault: str | None = None,
        submitted: list[str] | None = None,
    ) -> LibraryScanRunner:
        recorded = submitted if submitted is not None else []

        def inject(boundary: str) -> None:
            if boundary == fault:
                raise _InjectedCrash(boundary)

        return LibraryScanRunner(
            self.repository,
            submit_child=recorded.append,
            fault_injector=inject if fault is not None else None,
        )

    def _run(self, processor: LibraryScanRunner, *, generation: str = GENERATION_A) -> None:
        processor.run(
            self.job_id,
            runtime_generation_id=generation,
            active_library_root_id=self.root_id,
            should_stop=lambda: False,
        )

    def _job(self) -> dict[str, object]:
        job = self.repository.get_media_job(self.job_id)
        assert job is not None
        return job

    def _count(self, table: str, *, where: str = "1=1") -> int:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}"  # noqa: S608 - fixed test inputs
                ).fetchone()[0]
            )

    def _app(self, *, runner: MediaJobRunner | None = None) -> Flask:
        app = Flask(__name__)
        app.config.update(TESTING=True, DESKTOP_SESSION_TOKEN="desktop-token")
        app.extensions["media_repository"] = self.repository
        if runner is not None:
            app.extensions["media_job_runner"] = runner
        app.register_blueprint(api_blueprint)
        return app

    def _media_runner(self, name: str) -> MediaJobRunner:
        runner = MediaJobRunner(self.repository, self.root / name)
        runner.activate_runtime_generation(
            GENERATION_A,
            active_library_root_id=self.root_id,
        )
        self.runners.append(runner)
        return runner

    def test_fixed_batches_complete_same_job_and_submit_only_after_commit(self) -> None:
        for index in range(17):
            (self.library / f"clip-{index:02d}.mp4").write_bytes(f"video-{index}".encode())
        submitted: list[str] = []

        self._run(self._processor(submitted=submitted))

        job = self._job()
        self.assertEqual((job["status"], job["stage"]), ("succeeded", "scan_complete"))
        self.assertEqual(
            job["checkpoint"],
            {
                "object": "memolens.library_scan_checkpoint",
                "schema_version": "1",
                "root_permission_fingerprint": job["checkpoint"]["root_permission_fingerprint"],
                "last_relative_path": "clip-16.mp4",
                "processed_count": 17,
                "imported_count": 17,
                "skipped_count": 0,
                "rejected_count": 0,
                "child_job_count": 17,
                "batch_count": 2,
            },
        )
        self.assertEqual(len(submitted), 17)
        self.assertEqual(self._count("assets"), 17)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 17)

    def test_byte_bounded_batches_advance_ordered_cursor_without_dropping_remainder(self) -> None:
        for index in range(5):
            (self.library / f"clip-{index:02d}.mp4").write_bytes(f"clip-{index}".encode())
        committed: list[tuple[int, str]] = []
        submitted: list[str] = []

        def observe_commit(job_id: str) -> None:
            submitted.append(job_id)
            checkpoint = self._job()["checkpoint"]
            cursor = (checkpoint["processed_count"], checkpoint["last_relative_path"])
            if not committed or committed[-1] != cursor:
                committed.append(cursor)

        processor = LibraryScanRunner(self.repository, submit_child=observe_commit)
        with patch("backend.src.media.importing.MAX_IMPORT_BYTES", 12):
            self._run(processor)

        job = self._job()
        self.assertEqual((job["status"], job["stage"]), ("succeeded", "scan_complete"))
        self.assertEqual(committed, [(2, "clip-01.mp4"), (4, "clip-03.mp4"), (5, "clip-04.mp4")])
        self.assertEqual(job["checkpoint"]["imported_count"], 5)
        self.assertEqual(job["checkpoint"]["batch_count"], 3)
        self.assertEqual(len(set(submitted)), 5)
        self.assertEqual(self._count("assets"), 5)

    def test_oversized_file_is_rejected_without_stalling_following_byte_batches(self) -> None:
        (self.library / "a-too-large.mp4").write_bytes(b"x" * 13)
        for index in range(3):
            (self.library / f"b-{index}.mp4").write_bytes(f"clip-{index}".encode())
        (self.library / "c-link.mp4").symlink_to(self.library / "b-0.mp4")

        with patch("backend.src.media.importing.MAX_IMPORT_BYTES", 12):
            self._run(self._processor())

        job = self._job()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["checkpoint"]["processed_count"], 5)
        self.assertEqual(job["checkpoint"]["imported_count"], 3)
        self.assertEqual(job["checkpoint"]["rejected_count"], 2)
        self.assertEqual(job["checkpoint"]["last_relative_path"], "c-link.mp4")
        self.assertEqual(self._count("assets"), 3)

    def test_byte_batch_resume_starts_after_committed_prefix_and_preserves_remainder(self) -> None:
        for index in range(5):
            (self.library / f"clip-{index:02d}.mp4").write_bytes(f"clip-{index}".encode())

        with patch("backend.src.media.importing.MAX_IMPORT_BYTES", 12):
            with self.assertRaisesRegex(_InjectedCrash, "after_batch_commit"):
                self._run(self._processor(fault="after_batch_commit"))
            self.assertEqual(self._job()["checkpoint"]["last_relative_path"], "clip-01.mp4")
            self.assertEqual(self._count("assets"), 2)
            self.repository.mark_running_jobs_interrupted()
            processor = self._processor()
            processor.resume(self.job_id, active_library_root_id=self.root_id)
            self._run(processor, generation=GENERATION_B)

        job = self._job()
        self.assertEqual((job["id"], job["status"], job["attempt"]), (self.job_id, "succeeded", 2))
        self.assertEqual(job["checkpoint"]["last_relative_path"], "clip-04.mp4")
        self.assertEqual(job["checkpoint"]["processed_count"], 5)
        self.assertEqual(job["checkpoint"]["imported_count"], 5)
        self.assertEqual(job["checkpoint"]["child_job_count"], 5)
        self.assertEqual(job["checkpoint"]["batch_count"], 3)
        self.assertEqual(self._count("assets"), 5)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 5)

    def test_fault_inside_transaction_rolls_back_then_resumes_same_job(self) -> None:
        (self.library / "clip.mp4").write_bytes(b"video")
        with self.assertRaisesRegex(_InjectedCrash, "inside_batch_transaction"):
            self._run(self._processor(fault="inside_batch_transaction"))
        self.assertEqual(self._count("assets"), 0)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 0)
        self.assertEqual(self._job()["checkpoint"]["processed_count"], 0)

        self.repository.mark_running_jobs_interrupted()
        processor = self._processor()
        resumed = processor.resume(
            self.job_id,
            active_library_root_id=self.root_id,
        )
        self.assertEqual(resumed.job_id, self.job_id)
        self._run(processor, generation=GENERATION_B)

        job = self._job()
        self.assertEqual((job["id"], job["attempt"], job["status"]), (self.job_id, 2, "succeeded"))
        self.assertEqual(self._count("assets"), 1)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 1)

    def test_fault_before_transaction_writes_nothing_then_resumes_same_job(self) -> None:
        (self.library / "clip.mp4").write_bytes(b"video")
        with self.assertRaisesRegex(_InjectedCrash, "before_batch_commit"):
            self._run(self._processor(fault="before_batch_commit"))
        self.assertEqual(self._count("assets"), 0)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 0)
        self.assertEqual(self._job()["checkpoint"]["processed_count"], 0)

        self.repository.mark_running_jobs_interrupted()
        processor = self._processor()
        processor.resume(self.job_id, active_library_root_id=self.root_id)
        self._run(processor, generation=GENERATION_B)

        job = self._job()
        self.assertEqual((job["id"], job["attempt"], job["status"]), (self.job_id, 2, "succeeded"))
        self.assertEqual(self._count("assets"), 1)

    def test_fault_after_commit_preserves_cursor_and_explicit_resume_recovers_child(self) -> None:
        (self.library / "clip.mp4").write_bytes(b"video")
        submitted: list[str] = []
        with self.assertRaisesRegex(_InjectedCrash, "after_batch_commit"):
            self._run(
                self._processor(
                    fault="after_batch_commit",
                    submitted=submitted,
                )
            )
        self.assertEqual(submitted, [])
        committed_child = self.repository.list_media_jobs(active=True, limit=10)
        child_ids = [str(job["id"]) for job in committed_child if job["kind"] == "video_index"]
        self.assertEqual(len(child_ids), 1)
        self.assertEqual(self._job()["checkpoint"]["processed_count"], 1)

        self.repository.mark_running_jobs_interrupted()
        media_runner = self._media_runner("cache")
        try:
            with patch.object(media_runner, "submit") as submit:
                self.assertTrue(media_runner.resume(self.job_id, submit=False))
                submit.assert_called_once_with(child_ids[0])
        finally:
            media_runner.shutdown()
            self.runners.remove(media_runner)

        self._run(self._processor(), generation=GENERATION_B)
        job = self._job()
        self.assertEqual((job["id"], job["attempt"], job["status"]), (self.job_id, 2, "succeeded"))
        self.assertEqual(job["checkpoint"]["processed_count"], 1)
        self.assertEqual(job["checkpoint"]["child_job_count"], 1)
        self.assertEqual(self._count("assets"), 1)
        self.assertEqual(self._count("media_jobs", where="kind='video_index'"), 1)

    def test_no_media_and_supported_symlink_are_terminal_without_following(self) -> None:
        target = self.root / "outside.mp4"
        target.write_bytes(b"outside")
        (self.library / "link.mp4").symlink_to(target)

        self._run(self._processor())

        job = self._job()
        self.assertEqual((job["status"], job["stage"]), ("succeeded", "scan_complete"))
        self.assertEqual(job["checkpoint"]["processed_count"], 1)
        self.assertEqual(job["checkpoint"]["rejected_count"], 1)
        self.assertEqual(self._count("assets"), 0)

    def test_empty_library_completes_no_supported_media(self) -> None:
        self._run(self._processor())

        job = self._job()
        self.assertEqual(
            (job["status"], job["stage"]),
            ("succeeded", "no_supported_media"),
        )
        self.assertEqual(job["checkpoint"]["processed_count"], 0)

    def test_root_replacement_fails_nonretryable_before_import(self) -> None:
        (self.library / "clip.mp4").write_bytes(b"approved")
        displaced = self.root / "displaced-library"
        self.library.rename(displaced)
        self.library.mkdir()
        (self.library / "clip.mp4").write_bytes(b"replacement")

        self._run(self._processor())

        job = self._job()
        self.assertEqual((job["status"], job["stage"]), ("failed", "failed"))
        self.assertEqual(job["error"]["code"], "root_binding_changed")
        self.assertFalse(job["error"]["retryable"])
        self.assertEqual(self._count("assets"), 0)

    def test_queued_cancel_and_resume_preserve_same_job_and_checkpoint(self) -> None:
        processor = self._processor()
        cancelled = processor.cancel(
            self.job_id,
            runtime_generation_id=GENERATION_A,
            active_library_root_id=self.root_id,
        )
        self.assertEqual((cancelled.status, cancelled.stage), ("cancelled", "cancelled"))
        self.assertEqual(cancelled.error["code"], "user_cancelled")  # type: ignore[index]
        self.assertEqual(cancelled.checkpoint["processed_count"], 0)

        resumed = processor.resume(
            self.job_id,
            active_library_root_id=self.root_id,
        )
        self.assertEqual((resumed.job_id, resumed.attempt), (self.job_id, 2))
        self._run(processor, generation=GENERATION_B)

        job = self._job()
        self.assertEqual(
            (job["id"], job["attempt"], job["status"], job["stage"]),
            (self.job_id, 2, "succeeded", "no_supported_media"),
        )

    def test_unknown_job_kind_without_complete_identity_is_never_admitted_or_scanned(self) -> None:
        unknown_id = f"job_{'f' * 32}"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO media_jobs(
                     id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                     progress,attempt,cancel_requested,checkpoint_json,created_at)
                   VALUES(?,?,'unknown_kind',NULL,NULL,'queued','queued',0,1,0,'{}',?)""",
                (unknown_id, self.repository.database_uuid, CREATED_AT),
            )
        runner = self._media_runner("unknown-cache")
        try:
            with self.assertRaisesRegex(ValueError, "complete durable job identity"):
                runner.submit(unknown_id)
        finally:
            runner.shutdown()
            self.runners.remove(runner)
        job = self.repository.get_media_job(unknown_id)
        assert job is not None
        self.assertEqual((job["status"], job["stage"], job["error"]), ("queued", "queued", None))
        self.assertEqual(self._count("assets"), 0)

    def test_scan_dispatch_requires_receipt_root_runtime_and_one_shot_admission(self) -> None:
        runner = self._media_runner("scan-admission-cache")
        original = self._job()

        # Direct invocation without the process-private one-shot ticket is inert.
        runner._run(self.job_id)
        self.assertEqual(self._job(), original)

        identity = runner._library_scan_admission_identity(
            self.job_id,
            runtime_generation=GENERATION_A,
            active_library_root_id=self.root_id,
        )
        assert identity is not None
        stale_generation = runner._admission_authority.issue(self.job_id, identity)
        with runner._lock:
            runner._runtime_generation = GENERATION_B
        runner._run(self.job_id, admission_ticket=stale_generation)
        self.assertEqual(self._job(), original)
        runner._admission_authority.revoke(stale_generation)

        with runner._lock:
            runner._runtime_generation = GENERATION_A
            runner._active_image_library_root_id = f"root_{'f' * 24}"
        runner.submit(self.job_id)
        self.assertNotIn(self.job_id, runner._submitted)
        self.assertEqual(self._job(), original)

        rogue_id = f"job_{'8' * 32}"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO media_jobs(
                     id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                     progress,attempt,cancel_requested,checkpoint_json,created_at)
                   VALUES(?,?,'library_scan',NULL,NULL,'queued',
                          'awaiting_scan_worker',0,1,0,'{}',?)""",
                (rogue_id, self.repository.database_uuid, CREATED_AT),
            )
        with runner._lock:
            runner._active_image_library_root_id = self.root_id
        runner.submit(rogue_id)
        self.assertNotIn(rogue_id, runner._submitted)
        rogue = self.repository.get_media_job(rogue_id)
        assert rogue is not None
        self.assertEqual((rogue["status"], rogue["stage"]), ("queued", "awaiting_scan_worker"))
        self.assertEqual(self._count("assets"), 0)

    def test_unknown_checkpoint_is_replaced_by_zero_terminal_checkpoint(self) -> None:
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE media_jobs SET checkpoint_json=? WHERE id=?",
                ('{"unknown":true}', self.job_id),
            )

        self._run(self._processor())

        job = self._job()
        self.assertEqual((job["status"], job["stage"]), ("failed", "failed"))
        self.assertEqual(job["error"]["code"], "checkpoint_invalid")
        self.assertFalse(job["error"]["retryable"])
        self.assertEqual(job["checkpoint"]["processed_count"], 0)
        self.assertEqual(job["checkpoint"]["batch_count"], 0)
        self.assertEqual(self._count("assets"), 0)

    def test_get_and_list_are_receipt_anchored_and_rogue_scan_fails_closed(self) -> None:
        app = self._app()
        client = app.test_client()
        valid = client.get(f"/v1/index/jobs/{self.job_id}")
        self.assertEqual(valid.status_code, 200)
        public = valid.get_json()["job"]
        self.assertEqual(public["object"], "memolens.library_scan_job")
        self.assertEqual(public["id"], self.job_id)
        self.assertNotIn(str(self.library), repr(public))

        rogue_id = f"job_{'9' * 32}"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO media_jobs(
                     id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                     progress,attempt,cancel_requested,checkpoint_json,created_at)
                   VALUES(?,?,'library_scan',NULL,NULL,'queued',
                          'awaiting_scan_worker',0,1,0,'{}',?)""",
                (rogue_id, self.repository.database_uuid, CREATED_AT),
            )

        for response in (
            client.get(f"/v1/index/jobs/{rogue_id}"),
            client.get("/v1/index/jobs?active=true&limit=100"),
        ):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["code"], "library_scan_integrity_error")
            self.assertNotIn(str(self.library), response.get_data(as_text=True))

    def test_cancel_and_resume_routes_freeze_receipt_anchored_public_responses(self) -> None:
        runner = self._media_runner("route-cache")
        client = self._app(runner=runner).test_client()
        headers = {
            "X-MemoLens-Desktop-Token": "desktop-token",
            "Content-Type": "application/json",
        }
        cancelled = client.post(
            f"/v1/index/jobs/{self.job_id}/cancel",
            data="{}",
            headers={**headers, "Idempotency-Key": "scan-cancel-1"},
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.get_data(as_text=True))
        cancelled_job = cancelled.get_json()["job"]
        self.assertEqual(cancelled_job["object"], "memolens.library_scan_job")
        self.assertEqual(cancelled_job["error"], {"code": "user_cancelled", "retryable": True})
        cancelled_replay = client.post(
            f"/v1/index/jobs/{self.job_id}/cancel",
            data="{}",
            headers={**headers, "Idempotency-Key": "scan-cancel-1"},
        )
        self.assertEqual(cancelled_replay.status_code, 202)
        self.assertEqual(cancelled_replay.data, cancelled.data)

        resumed = client.post(
            f"/v1/index/jobs/{self.job_id}/resume",
            data="{}",
            headers={**headers, "Idempotency-Key": "scan-resume-1"},
        )
        self.assertEqual(resumed.status_code, 202, resumed.get_data(as_text=True))
        resumed_job = resumed.get_json()["job"]
        self.assertEqual(
            (resumed_job["id"], resumed_job["status"], resumed_job["attempt"]),
            (self.job_id, "queued", 2),
        )
        self.assertEqual(resumed.headers["Idempotency-Replayed"], "false")
        self.assertNotIn(str(self.library), repr(resumed_job))
        resumed_replay = client.post(
            f"/v1/index/jobs/{self.job_id}/resume",
            data="{}",
            headers={**headers, "Idempotency-Key": "scan-resume-1"},
        )
        self.assertEqual(resumed_replay.status_code, 202)
        self.assertEqual(resumed_replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(resumed_replay.data, resumed.data)


if __name__ == "__main__":
    unittest.main()
