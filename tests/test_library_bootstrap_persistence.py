from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from core.db import ImageIndexRepository
from core.media_db import (
    LibraryBootstrapConflictError,
    LibraryBootstrapIntegrityError,
    LibraryBootstrapValidationError,
    MediaRepository,
    library_bootstrap_candidate_binding_sha256,
)


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )


class LibraryBootstrapPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-bootstrap-core-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.other_library = self.root / "other-library"
        self.other_library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(None)
        self.now_ms = int(time.time() * 1000)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _arguments(
        self,
        path: Path | None = None,
        *,
        request_id: str | None = None,
    ) -> tuple[dict[str, object], int]:
        selected = path or self.library
        descriptor = _open_directory(selected)
        identity = os.fstat(descriptor)
        binding = self.repository.library_bootstrap_candidate_binding_sha256(
            canonical_root=selected,
            expected_library_root_device=int(identity.st_dev),
            expected_library_root_inode=int(identity.st_ino),
        )
        return (
            {
                "request_id": request_id or f"lb_{'a' * 64}",
                "intent_created_at_ms": self.now_ms - 1000,
                "intent_expires_at_ms": self.now_ms + 299_000,
                "native_confirmed_at_ms": self.now_ms,
                "candidate_binding_sha256": binding,
                "library_root_path": selected,
                "library_root_fd": descriptor,
                "expected_library_root_device": int(identity.st_dev),
                "expected_library_root_inode": int(identity.st_ino),
            },
            descriptor,
        )

    def _counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "library_roots",
                    "creative_projects",
                    "creative_briefs",
                    "media_jobs",
                    "creative_blueprint_operations",
                    "creative_blueprint_revisions",
                    "creative_blueprint_heads",
                    "blueprint_command_receipts",
                    "blueprint_desktop_receipt_integrity",
                    "library_bootstrap_receipts",
                )
            }

    def test_cross_language_binding_digest_fixed_vector(self) -> None:
        self.assertEqual(
            library_bootstrap_candidate_binding_sha256(
                canonical_root="/tmp/素材 库",
                database_path="/tmp/状态/MemoLens 数据.db",
                expected_library_root_device="42",
                expected_library_root_inode="9007199254740991",
            ),
            "7ae5b68db2ac7109d00072bd9585d7e4d00f3fbdb87f944313dcec144348145f",
        )

    def test_commit_is_one_path_free_authority_staging_transaction(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            committed = self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)

        self.assertEqual(committed["response_status"], 201)
        self.assertFalse(committed["replayed"])
        response = committed["response"]
        self.assertEqual(
            response,
            {
                "object": "memolens.library_bootstrap_result",
                "schema_version": "1",
                "request_id": f"lb_{'a' * 64}",
                "status": "library_authority_committed",
                "scan_started": False,
                "scan_stage": "awaiting_scan_worker",
                "editor_state": "awaiting_grounded_timeline",
                "blueprint_authority": "unverified",
                "timeline_edit_granted": False,
            },
        )
        forbidden = (str(self.library), str(self.db_path), "canonical_path", "device", "inode")
        serialized = str(response)
        for value in forbidden:
            self.assertNotIn(value, serialized)
        self.assertEqual(set(self._counts().values()), {1})

        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            job = connection.execute("SELECT * FROM media_jobs").fetchone()
            self.assertEqual(
                (job["kind"], job["status"], job["stage"], job["progress"]),
                ("library_scan", "queued", "awaiting_scan_worker", 0.0),
            )
            revision = connection.execute(
                "SELECT authority_state,revision FROM creative_blueprint_revisions"
            ).fetchone()
            self.assertEqual(tuple(revision), ("unverified", 1))

    def test_exact_replay_returns_identical_body_and_zero_domain_writes(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            first = self.repository.commit_library_bootstrap(**arguments)
            before = self._counts()
            second = self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(second["response"], first["response"])
        self.assertEqual(second["response_status"], 201)
        self.assertTrue(second["replayed"])
        self.assertEqual(self._counts(), before)

    def test_same_request_different_binding_is_permanent_conflict(self) -> None:
        first, first_fd = self._arguments()
        second, second_fd = self._arguments(self.other_library)
        try:
            self.repository.commit_library_bootstrap(**first)
            with self.assertRaisesRegex(
                LibraryBootstrapConflictError,
                "library_bootstrap_request_conflict",
            ):
                self.repository.commit_library_bootstrap(**second)
        finally:
            os.close(first_fd)
            os.close(second_fd)
        self.assertEqual(set(self._counts().values()), {1})

    def test_different_request_same_database_is_permanent_conflict(self) -> None:
        first, first_fd = self._arguments()
        second, second_fd = self._arguments(
            request_id=f"lb_{'b' * 64}",
        )
        try:
            self.repository.commit_library_bootstrap(**first)
            with self.assertRaisesRegex(
                LibraryBootstrapConflictError,
                "library_bootstrap_database_already_committed",
            ):
                self.repository.commit_library_bootstrap(**second)
        finally:
            os.close(first_fd)
            os.close(second_fd)
        self.assertEqual(set(self._counts().values()), {1})

    def test_concurrent_different_requests_create_only_one_database_authority(self) -> None:
        barrier = threading.Barrier(2)

        def commit(index: int) -> str:
            repository = MediaRepository(self.db_path)
            arguments, descriptor = self._arguments(
                request_id=f"lb_{('a' if index == 0 else 'b') * 64}",
            )
            try:
                barrier.wait(timeout=5)
                repository.commit_library_bootstrap(**arguments)
                return "committed"
            except LibraryBootstrapConflictError:
                return "conflict"
            finally:
                os.close(descriptor)
                repository.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(commit, range(2)))
        self.assertCountEqual(outcomes, ["committed", "conflict"])
        self.assertEqual(set(self._counts().values()), {1})

    def test_rename_is_identity_change_even_when_device_and_inode_match(self) -> None:
        first, first_fd = self._arguments()
        try:
            self.repository.commit_library_bootstrap(**first)
        finally:
            os.close(first_fd)
        renamed = self.root / "renamed-library"
        self.library.rename(renamed)
        replay, replay_fd = self._arguments(renamed)
        try:
            with self.assertRaisesRegex(
                LibraryBootstrapConflictError,
                "library_bootstrap_request_conflict",
            ):
                self.repository.commit_library_bootstrap(**replay)
        finally:
            os.close(replay_fd)

    def test_descriptor_or_binding_mismatch_writes_nothing(self) -> None:
        arguments, descriptor = self._arguments()
        arguments["expected_library_root_inode"] = int(
            arguments["expected_library_root_inode"]
        ) + 1
        try:
            with self.assertRaisesRegex(
                LibraryBootstrapValidationError,
                "library_root_identity_changed",
            ):
                self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(set(self._counts().values()), {0})

    def test_fault_before_v20_receipt_rolls_back_every_domain_write(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            with patch.object(
                MediaRepository,
                "_store_library_bootstrap_receipt",
                side_effect=RuntimeError("injected-bootstrap-receipt-fault"),
            ):
                with self.assertRaisesRegex(RuntimeError, "injected-bootstrap-receipt-fault"):
                    self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(set(self._counts().values()), {0})

    def test_receipt_is_immutable_and_tamper_fails_cold_replay(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE library_bootstrap_receipts SET response_status=200"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute("DELETE FROM library_bootstrap_receipts")

        self.repository.close()
        reopened = MediaRepository(self.db_path)
        reopened.ensure_schema(None)
        replay_arguments, replay_fd = self._arguments()
        try:
            replay = reopened.commit_library_bootstrap(**replay_arguments)
        finally:
            os.close(replay_fd)
            reopened.close()
        self.assertTrue(replay["replayed"])

    def test_trigger_bypass_root_path_tamper_fails_receipt_validation(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "DROP TRIGGER trg_library_bootstrap_roots_identity_immutable"
            )
            connection.execute(
                "UPDATE library_roots SET canonical_path='/tmp/changed-binding'"
            )
            connection.execute(
                MediaRepository._expected_v20_physical_schema()[
                    ("trigger", "trg_library_bootstrap_roots_identity_immutable")
                ]
            )
        replay_arguments, replay_fd = self._arguments()
        try:
            with self.assertRaisesRegex(
                LibraryBootstrapIntegrityError,
                "library_bootstrap_dependency_integrity_error",
            ):
                self.repository.commit_library_bootstrap(**replay_arguments)
        finally:
            os.close(replay_fd)

    def test_concurrent_same_request_converges_to_one_receipt(self) -> None:
        barrier = threading.Barrier(2)

        def commit() -> dict[str, object]:
            repository = MediaRepository(self.db_path)
            arguments, descriptor = self._arguments()
            try:
                barrier.wait(timeout=5)
                return repository.commit_library_bootstrap(**arguments)
            finally:
                os.close(descriptor)
                repository.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: commit(), range(2)))
        self.assertEqual({result["replayed"] for result in results}, {False, True})
        self.assertEqual(results[0]["response"], results[1]["response"])
        self.assertEqual(set(self._counts().values()), {1})

    def test_invalid_or_expired_request_is_rejected_before_writes(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            arguments["request_id"] = "lb_not-opaque"
            with self.assertRaisesRegex(
                LibraryBootstrapValidationError,
                "invalid_library_bootstrap_request",
            ):
                self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(set(self._counts().values()), {0})

    def test_confirmed_before_expiry_can_first_commit_after_ttl(self) -> None:
        arguments, descriptor = self._arguments()
        arguments["intent_created_at_ms"] = 1_000_000
        arguments["intent_expires_at_ms"] = 1_300_000
        arguments["native_confirmed_at_ms"] = 1_299_999
        try:
            committed = self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertFalse(committed["replayed"])
        self.assertEqual(committed["response"]["status"], "library_authority_committed")

    def test_confirmation_at_expiry_is_rejected_before_writes(self) -> None:
        arguments, descriptor = self._arguments()
        arguments["native_confirmed_at_ms"] = int(arguments["intent_expires_at_ms"])
        try:
            with self.assertRaisesRegex(
                LibraryBootstrapValidationError,
                "invalid_library_bootstrap_request",
            ):
                self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(set(self._counts().values()), {0})

    def test_activation_recovery_preserves_unclaimed_scan_but_interrupts_running(self) -> None:
        arguments, descriptor = self._arguments()
        try:
            self.repository.commit_library_bootstrap(**arguments)
        finally:
            os.close(descriptor)
        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status,stage FROM media_jobs"
                ).fetchone(),
                ("queued", "awaiting_scan_worker"),
            )
        initial = self.repository.list_recoverable_library_scans()[0]
        scan_descriptor = _open_directory(self.library)
        try:
            self.repository.claim_library_scan(
                job_id=str(initial["job_id"]),
                runtime_generation_id=f"runtime_generation_{'a' * 64}",
                expected_attempt=int(initial["attempt"]),
                expected_checkpoint=initial["checkpoint"],
                library_root_fd=scan_descriptor,
            )
        finally:
            os.close(scan_descriptor)
        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status,stage FROM media_jobs"
                ).fetchone(),
                ("interrupted", "interrupted"),
            )


if __name__ == "__main__":
    unittest.main()
