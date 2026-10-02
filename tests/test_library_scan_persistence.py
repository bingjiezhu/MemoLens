from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from core.db import ImageIndexRepository
from core.library_scan_persistence import validate_library_scan_checkpoint
from core.media_db import (
    LibraryScanConflictError,
    LibraryScanIntegrityError,
    LibraryScanValidationError,
    MediaRepository,
    canonical_json,
    utc_now_iso,
)


GENERATION_A = f"runtime_generation_{'a' * 64}"
GENERATION_B = f"runtime_generation_{'b' * 64}"


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )


class LibraryScanPersistenceTests(unittest.TestCase):
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
        now_ms = int(time.time() * 1000)
        descriptor = _open_directory(self.library)
        identity = os.fstat(descriptor)
        request_id = f"lb_{'a' * 64}"
        binding = self.repository.library_bootstrap_candidate_binding_sha256(
            canonical_root=self.library,
            expected_library_root_device=int(identity.st_dev),
            expected_library_root_inode=int(identity.st_ino),
        )
        try:
            self.repository.commit_library_bootstrap(
                request_id=request_id,
                intent_created_at_ms=now_ms - 1_000,
                intent_expires_at_ms=now_ms + 299_000,
                native_confirmed_at_ms=now_ms,
                candidate_binding_sha256=binding,
                library_root_path=self.library,
                library_root_fd=descriptor,
                expected_library_root_device=int(identity.st_dev),
                expected_library_root_inode=int(identity.st_ino),
            )
        finally:
            os.close(descriptor)
        self.initial = self.repository.list_recoverable_library_scans()[0]
        self.job_id = str(self.initial["job_id"])

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _claim(self, generation: str = GENERATION_A) -> dict[str, object]:
        descriptor = _open_directory(self.library)
        try:
            return self.repository.claim_library_scan(
                job_id=self.job_id,
                runtime_generation_id=generation,
                expected_attempt=int(self.initial["attempt"]),
                expected_checkpoint=self.initial["checkpoint"],
                library_root_fd=descriptor,
            )
        finally:
            os.close(descriptor)

    def _batch_callback(
        self,
        paths: tuple[str, ...],
        *,
        child_id: str = f"job_{'c' * 32}",
        same_bytes: bool = False,
    ):  # type: ignore[no-untyped-def]
        def apply(connection: sqlite3.Connection) -> dict[str, object]:
            database_uuid = str(self.initial["database_uuid"])
            root_id = str(self.initial["library_root_id"])
            now = utc_now_iso()
            first_asset_id = ""
            for index, relative_path in enumerate(paths):
                digest = hashlib.sha256(
                    ("same" if same_bytes else relative_path).encode()
                ).hexdigest()
                asset_id = f"asset_{digest[:24]}"
                first_asset_id = first_asset_id or asset_id
                connection.execute(
                    """INSERT INTO assets(
                         id,kind,sha256,mime_type,file_size,probe_status,
                         created_at,updated_at)
                       VALUES(?, 'video', ?, 'video/mp4', 1, 'pending', ?, ?)
                       ON CONFLICT(sha256) DO NOTHING""",
                    (asset_id, digest, now, now),
                )
                resolved = connection.execute(
                    "SELECT id FROM assets WHERE sha256=?",
                    (digest,),
                ).fetchone()
                assert resolved is not None
                asset_id = str(resolved["id"])
                source_id = MediaRepository.source_id(root_id, relative_path)
                connection.execute(
                    """INSERT INTO asset_sources(
                         id,asset_id,library_root_id,relative_path,display_filename,
                         observed_size,observed_mtime_ns,source_file_id,availability,
                         is_preferred,last_verified_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,1,1,?,'available',?,?,?,?)""",
                    (
                        source_id,
                        asset_id,
                        root_id,
                        relative_path,
                        Path(relative_path).name,
                        str(index + 1),
                        int(index == 0),
                        now,
                        now,
                        now,
                    ),
                )
            if same_bytes:
                resolved = connection.execute(
                    "SELECT asset_id FROM asset_sources WHERE library_root_id=? LIMIT 1",
                    (root_id,),
                ).fetchone()
                assert resolved is not None
                first_asset_id = str(resolved["asset_id"])
            connection.execute(
                """INSERT INTO media_jobs(
                     id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                     progress,attempt,cancel_requested,checkpoint_json,created_at)
                   VALUES(?,?,'video_index',?,NULL,'queued','queued',0,1,0,'{}',?)""",
                (child_id, database_uuid, first_asset_id, now),
            )
            return {
                "processed_count": len(paths),
                "imported_count": len(paths),
                "skipped_count": 0,
                "rejected_count": 0,
                "child_job_ids": [child_id],
            }

        return apply

    def test_context_is_exact_receipt_anchored_and_rogue_job_fails_closed(self) -> None:
        self.assertEqual(
            set(self.initial),
            {
                "object",
                "schema_version",
                "request_id",
                "job_id",
                "database_uuid",
                "library_root_id",
                "canonical_root",
                "expected_library_root_device",
                "expected_library_root_inode",
                "root_permission_fingerprint",
                "status",
                "stage",
                "attempt",
                "cancel_requested",
                "checkpoint",
                "error",
            },
        )
        self.assertEqual(
            (self.initial["status"], self.initial["stage"], self.initial["checkpoint"]),
            ("queued", "awaiting_scan_worker", {}),
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO media_jobs(
                     id,database_uuid,kind,status,stage,progress,attempt,
                     cancel_requested,checkpoint_json,created_at)
                   VALUES(?,?,'library_scan','queued','awaiting_scan_worker',0,1,0,'{}',?)""",
                (f"job_{'f' * 32}", self.initial["database_uuid"], utc_now_iso()),
            )
        with self.assertRaisesRegex(
            LibraryScanIntegrityError,
            "library_scan_receipt_binding_invalid",
        ):
            self.repository.get_library_scan_context(f"job_{'f' * 32}")

    def test_tampered_progress_and_error_causal_stage_fail_closed(self) -> None:
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE media_jobs SET progress=1 WHERE id=?",
                (self.job_id,),
            )
        with self.assertRaisesRegex(
            LibraryScanIntegrityError,
            "library_scan_state_invalid",
        ):
            self.repository.get_library_scan_context(self.job_id)

        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE media_jobs SET progress=0 WHERE id=?",
                (self.job_id,),
            )
        descriptor = _open_directory(self.library)
        try:
            cancelled = self.repository.request_library_scan_cancel(
                job_id=self.job_id,
                runtime_generation_id=None,
                expected_attempt=1,
                expected_checkpoint={},
                library_root_fd=descriptor,
            )
        finally:
            os.close(descriptor)
        tampered_error = dict(cancelled["error"])
        tampered_error["stage"] = "scanning"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE media_jobs SET error_json=? WHERE id=?",
                (canonical_json(tampered_error), self.job_id),
            )
        with self.assertRaisesRegex(
            LibraryScanIntegrityError,
            "library_scan_state_invalid",
        ):
            self.repository.get_library_scan_context(self.job_id)

    def test_checkpoint_rejects_unknown_types_overflow_and_noncanonical_path(self) -> None:
        claimed = self._claim()
        checkpoint = dict(claimed["checkpoint"])
        for mutate in (
            lambda value: value.update(extra=True),
            lambda value: value.update(processed_count=True),
            lambda value: value.update(processed_count=9_007_199_254_740_992),
            lambda value: value.update(last_relative_path="a/../b", processed_count=1, batch_count=1),
            lambda value: value.update(last_relative_path="\ud800", processed_count=1, batch_count=1),
        ):
            invalid = dict(checkpoint)
            mutate(invalid)
            with self.assertRaises(LibraryScanValidationError):
                validate_library_scan_checkpoint(invalid, allow_initial=False)

    def test_claim_batch_same_bytes_multi_source_and_complete_are_atomic(self) -> None:
        claimed = self._claim()
        descriptor = _open_directory(self.library)
        try:
            advanced = self.repository.commit_library_scan_batch(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=claimed["checkpoint"],
                library_root_fd=descriptor,
                relative_paths=("a.mp4", "b.mp4"),
                apply_batch=self._batch_callback(
                    ("a.mp4", "b.mp4"),
                    same_bytes=True,
                ),
            )
            completed = self.repository.complete_library_scan(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=advanced["scan"]["checkpoint"],
                library_root_fd=descriptor,
                no_supported_media=False,
            )
        finally:
            os.close(descriptor)
        self.assertEqual(advanced["child_job_ids"], [f"job_{'c' * 32}"])
        self.assertEqual(advanced["scan"]["checkpoint"]["processed_count"], 2)
        self.assertEqual((completed["status"], completed["stage"]), ("succeeded", "scan_complete"))

    def test_stale_checkpoint_and_callback_fault_roll_back_all_rows(self) -> None:
        claimed = self._claim()
        descriptor = _open_directory(self.library)

        def fault(connection: sqlite3.Connection) -> dict[str, object]:
            connection.execute(
                "INSERT INTO assets(id,kind,sha256,mime_type,file_size,probe_status,created_at,updated_at) "
                "VALUES('asset_fault','video',?,'video/mp4',1,'pending',?,?)",
                ("d" * 64, utc_now_iso(), utc_now_iso()),
            )
            raise RuntimeError("injected-batch-fault")

        try:
            with self.assertRaisesRegex(RuntimeError, "injected-batch-fault"):
                self.repository.commit_library_scan_batch(
                    job_id=self.job_id,
                    runtime_generation_id=GENERATION_A,
                    expected_attempt=1,
                    expected_checkpoint=claimed["checkpoint"],
                    library_root_fd=descriptor,
                    relative_paths=("fault.mp4",),
                    apply_batch=fault,
                )
            advanced = self.repository.commit_library_scan_batch(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=claimed["checkpoint"],
                library_root_fd=descriptor,
                relative_paths=("ok.mp4",),
                apply_batch=self._batch_callback(("ok.mp4",)),
            )
            with self.assertRaisesRegex(
                LibraryScanConflictError,
                "library_scan_checkpoint_conflict",
            ):
                self.repository.commit_library_scan_batch(
                    job_id=self.job_id,
                    runtime_generation_id=GENERATION_A,
                    expected_attempt=1,
                    expected_checkpoint=claimed["checkpoint"],
                    library_root_fd=descriptor,
                    relative_paths=("z.mp4",),
                    apply_batch=lambda _connection: {},
                )
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM assets WHERE id='asset_fault'").fetchone()[0],
                0,
            )
        self.assertEqual(advanced["scan"]["checkpoint"]["batch_count"], 1)

    def test_reused_queued_child_cannot_be_counted_as_new(self) -> None:
        claimed = self._claim()
        reused = f"job_{'e' * 32}"
        callback = self._batch_callback(("reused.mp4",), child_id=reused)
        with self.repository.transaction(immediate=True) as connection:
            callback(connection)
        descriptor = _open_directory(self.library)
        try:
            with self.assertRaisesRegex(
                LibraryScanIntegrityError,
                "library_scan_child_job_invalid",
            ):
                self.repository.commit_library_scan_batch(
                    job_id=self.job_id,
                    runtime_generation_id=GENERATION_A,
                    expected_attempt=1,
                    expected_checkpoint=claimed["checkpoint"],
                    library_root_fd=descriptor,
                    relative_paths=("reused.mp4",),
                    apply_batch=lambda _connection: {
                        "processed_count": 1,
                        "imported_count": 0,
                        "skipped_count": 1,
                        "rejected_count": 0,
                        "child_job_ids": [reused],
                    },
                )
        finally:
            os.close(descriptor)

    def test_concurrent_same_generation_claim_has_one_lease_owner(self) -> None:
        barrier = __import__("threading").Barrier(2)

        def claim() -> str:
            descriptor = _open_directory(self.library)
            try:
                barrier.wait(timeout=5)
                self.repository.claim_library_scan(
                    job_id=self.job_id,
                    runtime_generation_id=GENERATION_A,
                    expected_attempt=1,
                    expected_checkpoint={},
                    library_root_fd=descriptor,
                )
                return "claimed"
            except LibraryScanConflictError:
                return "conflict"
            finally:
                os.close(descriptor)

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: claim(), range(2)))
        self.assertCountEqual(outcomes, ["claimed", "conflict"])

    def test_interrupt_resume_cancel_preserve_same_job_and_checkpoint(self) -> None:
        claimed = self._claim()
        descriptor = _open_directory(self.library)
        try:
            interrupted = self.repository.interrupt_library_scan(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=claimed["checkpoint"],
                library_root_fd=descriptor,
            )
            resumed = self.repository.resume_library_scan(
                job_id=self.job_id,
                expected_attempt=1,
                expected_checkpoint=interrupted["checkpoint"],
                library_root_fd=descriptor,
            )
            reclaimed = self.repository.claim_library_scan(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_B,
                expected_attempt=2,
                expected_checkpoint=resumed["checkpoint"],
                library_root_fd=descriptor,
            )
            cancelling = self.repository.request_library_scan_cancel(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_B,
                expected_attempt=2,
                expected_checkpoint=reclaimed["checkpoint"],
                library_root_fd=descriptor,
            )
            cancelled = self.repository.finish_library_scan_cancel(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_B,
                expected_attempt=2,
                expected_checkpoint=cancelling["checkpoint"],
                library_root_fd=descriptor,
            )
        finally:
            os.close(descriptor)
        self.assertEqual(
            {interrupted["job_id"], resumed["job_id"], cancelled["job_id"]},
            {self.job_id},
        )
        self.assertEqual(resumed["attempt"], 2)
        self.assertEqual(cancelled["error"]["code"], "user_cancelled")

    def test_zero_media_completion_and_generic_mutators_are_closed(self) -> None:
        claimed = self._claim()
        for operation in (
            lambda: self.repository.update_media_job(self.job_id, status="succeeded"),
            lambda: self.repository.request_media_job_cancel(self.job_id),
            lambda: self.repository.reset_media_job_for_resume(self.job_id),
        ):
            with self.assertRaisesRegex(RuntimeError, "library_scan_requires_dedicated_api"):
                operation()
        descriptor = _open_directory(self.library)
        try:
            completed = self.repository.complete_library_scan(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=claimed["checkpoint"],
                library_root_fd=descriptor,
                no_supported_media=True,
            )
        finally:
            os.close(descriptor)
        self.assertEqual((completed["status"], completed["stage"]), ("succeeded", "no_supported_media"))

    def test_unknown_checkpoint_can_converge_to_nonretryable_failed(self) -> None:
        unknown = {"object": "unknown.checkpoint", "schema_version": "1"}
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE media_jobs SET checkpoint_json=? WHERE id=?",
                (canonical_json(unknown), self.job_id),
            )
        failed = self.repository.invalidate_library_scan(
            job_id=self.job_id,
            expected_attempt=1,
            expected_checkpoint=unknown,
            code="checkpoint_invalid",
            detail="unknown_checkpoint_shape",
        )
        self.assertEqual((failed["status"], failed["stage"]), ("failed", "failed"))
        self.assertEqual(failed["error"]["code"], "checkpoint_invalid")
        self.assertFalse(failed["error"]["retryable"])
        self.assertEqual(failed["checkpoint"]["processed_count"], 0)

        descriptor = _open_directory(self.library)
        try:
            with self.assertRaises(LibraryScanConflictError):
                self.repository.request_library_scan_cancel(
                    job_id=self.job_id,
                    runtime_generation_id=None,
                    expected_attempt=1,
                    expected_checkpoint=failed["checkpoint"],
                    library_root_fd=descriptor,
                )
        finally:
            os.close(descriptor)
        unchanged = self.repository.get_library_scan_context(self.job_id)
        self.assertEqual(unchanged["error"]["code"], "checkpoint_invalid")
        self.assertFalse(unchanged["error"]["retryable"])

    def test_startup_recovery_finishes_pending_cancel_with_typed_error(self) -> None:
        claimed = self._claim()
        descriptor = _open_directory(self.library)
        try:
            cancelling = self.repository.request_library_scan_cancel(
                job_id=self.job_id,
                runtime_generation_id=GENERATION_A,
                expected_attempt=1,
                expected_checkpoint=claimed["checkpoint"],
                library_root_fd=descriptor,
            )
        finally:
            os.close(descriptor)
        self.assertEqual(cancelling["status"], "cancelling")
        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        recovered = self.repository.get_library_scan_context(self.job_id)
        self.assertEqual((recovered["status"], recovered["stage"]), ("cancelled", "cancelled"))
        self.assertEqual(recovered["error"]["code"], "user_cancelled")


if __name__ == "__main__":
    unittest.main()
