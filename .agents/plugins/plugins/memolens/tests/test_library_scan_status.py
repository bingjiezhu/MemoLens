from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import MediaRepository  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_core import MemoLensGateway  # noqa: E402
from memolens_read_store import ReadOnlyMemoLensStore  # noqa: E402


_TIMESTAMP = "2026-08-30T12:00:00+00:00"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class LibraryScanStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-plugin-library-scan-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "素材 Library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "MemoLens.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        repository = MediaRepository(self.db_path)
        repository.ensure_schema(None)
        descriptor = os.open(
            self.library,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        identity = os.fstat(descriptor)
        binding = repository.library_bootstrap_candidate_binding_sha256(
            canonical_root=str(self.library),
            expected_library_root_device=identity.st_dev,
            expected_library_root_inode=identity.st_ino,
        )
        try:
            repository.commit_library_bootstrap(
                request_id=f"lb_{'a' * 64}",
                intent_created_at_ms=1_777_777_700_000,
                intent_expires_at_ms=1_777_778_000_000,
                native_confirmed_at_ms=1_777_777_701_000,
                candidate_binding_sha256=binding,
                library_root_path=str(self.library),
                library_root_fd=descriptor,
                expected_library_root_device=identity.st_dev,
                expected_library_root_inode=identity.st_ino,
            )
        finally:
            os.close(descriptor)
            repository.close()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            receipt = connection.execute(
                "SELECT database_uuid,scan_job_id,library_root_fingerprint "
                "FROM library_bootstrap_receipts"
            ).fetchone()
        assert receipt is not None
        self.database_uuid = str(receipt["database_uuid"])
        self.scan_job_id = str(receipt["scan_job_id"])
        self.root_fingerprint = str(receipt["library_root_fingerprint"])

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_codex_docs_switch_from_commit_receipt_to_exact_scan_status(self) -> None:
        for relative in ("README.md", "skills/use-memolens/SKILL.md"):
            with self.subTest(relative=relative):
                text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("library_authority_committed", text)
                self.assertIn("memolens_status", text)
                self.assertIn("database.library_scan", text)
                self.assertIn("receipt-bound", text)
                self.assertIn("no_supported_media", text)
                self.assertNotIn("Success means only `library_authority_staged`", text)

        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        interface = manifest["interface"]
        self.assertEqual(len(interface["defaultPrompt"]), 3)
        description = interface["longDescription"]
        self.assertIn("use memolens_status", description)
        self.assertIn("database.library_scan", description)
        self.assertIn("Scan completion still does not make the editor ready", description)

        readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("controlled and local", readme)
        self.assertIn("does not stand in for a fresh Codex or DeepSeek model call", readme)

    def test_legacy_two_column_database_meta_has_no_v20_scan_projection(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE database_meta("
                "database_uuid TEXT NOT NULL,schema_version INTEGER NOT NULL)"
            )
            connection.execute(
                "INSERT INTO database_meta VALUES(?,?)",
                ("db_legacy_fixture", 4),
            )
            store = ReadOnlyMemoLensStore(self.db_path, self.library)
            self.assertIsNone(store._library_scan_status(connection))

    def test_v20_database_meta_shape_tamper_fails_closed(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "ALTER TABLE database_meta ADD COLUMN tamper_marker TEXT"
            )
        with self.assertRaises(MemoLensError) as captured:
            ReadOnlyMemoLensStore(self.db_path, self.library).status()
        self.assertEqual(
            captured.exception.code,
            "library_scan_status_integrity_error",
        )

    def test_v20_namespace_cannot_downgrade_through_legacy_meta_shape(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.executescript(
                """
                CREATE TABLE database_meta(
                    database_uuid TEXT NOT NULL,
                    schema_version INTEGER NOT NULL
                );
                INSERT INTO database_meta VALUES('db_tampered_v20',4);
                CREATE TABLE library_bootstrap_receipts(request_id TEXT);
                """
            )
            store = ReadOnlyMemoLensStore(self.db_path, self.library)
            with self.assertRaises(MemoLensError) as captured:
                store._library_scan_status(connection)
        self.assertEqual(
            captured.exception.code,
            "library_scan_status_integrity_error",
        )

    def test_v20_exact_meta_shape_cannot_claim_legacy_version(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                "DROP TRIGGER trg_database_meta_no_schema_downgrade"
            )
            connection.execute(
                "UPDATE database_meta SET schema_version=19 WHERE singleton=1"
            )
        with self.assertRaises(MemoLensError) as captured:
            ReadOnlyMemoLensStore(self.db_path, self.library).status()
        self.assertEqual(
            captured.exception.code,
            "library_scan_status_integrity_error",
        )

    def test_v20_namespace_without_database_meta_fails_closed(self) -> None:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute(
                "CREATE TABLE library_bootstrap_receipts(request_id TEXT)"
            )
            store = ReadOnlyMemoLensStore(self.db_path, self.library)
            with self.assertRaises(MemoLensError) as captured:
                store._library_scan_status(connection)
        self.assertEqual(
            captured.exception.code,
            "library_scan_status_integrity_error",
        )

    def _checkpoint(
        self,
        *,
        cursor: str | None = "旅行/片段 001.mov",
        processed: int = 7,
        imported: int = 4,
        skipped: int = 2,
        rejected: int = 1,
        child_jobs: int = 4,
        batches: int = 2,
    ) -> dict[str, object]:
        return {
            "object": "memolens.library_scan_checkpoint",
            "schema_version": "1",
            "root_permission_fingerprint": self.root_fingerprint,
            "last_relative_path": cursor,
            "processed_count": processed,
            "imported_count": imported,
            "skipped_count": skipped,
            "rejected_count": rejected,
            "child_job_count": child_jobs,
            "batch_count": batches,
        }

    @staticmethod
    def _error(code: str, *, stage: str) -> str:
        retryable = code in {
            "worker_interrupted",
            "user_cancelled",
            "batch_failed",
        }
        return _canonical_json(
            {
                "object": "memolens.library_scan_error",
                "schema_version": "1",
                "code": code,
                "stage": stage,
                "retryable": retryable,
                "detail_sha256": hashlib.sha256(code.encode()).hexdigest(),
            }
        )

    def _update_job(
        self,
        *,
        status: str,
        stage: str,
        progress: float,
        checkpoint_json: str,
        cancel_requested: int = 0,
        error_json: str | None = None,
        started_at: str | None = _TIMESTAMP,
        heartbeat_at: str | None = _TIMESTAMP,
        finished_at: str | None = None,
    ) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """UPDATE media_jobs
                      SET status=?,stage=?,progress=?,cancel_requested=?,
                          checkpoint_json=?,error_json=?,started_at=?,heartbeat_at=?,
                          finished_at=?
                    WHERE id=?""",
                (
                    status,
                    stage,
                    progress,
                    cancel_requested,
                    checkpoint_json,
                    error_json,
                    started_at,
                    heartbeat_at,
                    finished_at,
                    self.scan_job_id,
                ),
            )

    def _status(self) -> dict[str, object]:
        payload = ReadOnlyMemoLensStore(self.db_path, self.library).status()
        scan = payload.get("library_scan")
        self.assertIsInstance(scan, dict)
        return scan  # type: ignore[return-value]

    def test_initial_v20_projection_is_receipt_anchored_and_path_free(self) -> None:
        scan = self._status()
        self.assertEqual(
            scan,
            {
                "object": "memolens.library_scan_job",
                "schema_version": "1",
                "id": self.scan_job_id,
                "kind": "library_scan",
                "status": "queued",
                "stage": "awaiting_scan_worker",
                "progress": 0.0,
                "attempt": 1,
                "cancel_requested": False,
                "resumable": False,
                "created_at": scan["created_at"],
                "started_at": None,
                "heartbeat_at": None,
                "finished_at": None,
                "counts": {
                    "processed": 0,
                    "imported": 0,
                    "skipped": 0,
                    "rejected": 0,
                    "child_jobs": 0,
                    "batches": 0,
                },
                "no_supported_media": False,
                "eta_seconds": None,
                "error": None,
            },
        )
        serialized = _canonical_json(scan)
        for forbidden in (
            str(self.root),
            self.root_fingerprint,
            "last_relative_path",
            "checkpoint",
            "database_uuid",
            "request_id",
        ):
            self.assertNotIn(forbidden, serialized)

        public_status = MemoLensGateway(
            db_path=self.db_path,
            library_dir=self.library,
        ).status()
        self.assertEqual(public_status["status"], "ok")
        self.assertEqual(public_status["source"], "sqlite_read_only")
        self.assertEqual(public_status["database"]["library_scan"], scan)

    def test_rogue_newer_library_scan_job_cannot_replace_receipt_truth(self) -> None:
        self._update_job(
            status="running",
            stage="scanning",
            progress=0.5,
            checkpoint_json=_canonical_json(self._checkpoint()),
        )
        rogue_id = f"job_{'e' * 32}"
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO media_jobs(
                       id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                       progress,attempt,cancel_requested,checkpoint_json,error_json,
                       created_at,started_at,heartbeat_at,finished_at)
                     VALUES(?,?,'library_scan',NULL,NULL,'succeeded','scan_complete',
                            1,1,0,?,NULL,?,?,?,?)""",
                (
                    rogue_id,
                    self.database_uuid,
                    _canonical_json(
                        self._checkpoint(
                            cursor="rogue.mov",
                            processed=999,
                            imported=999,
                            skipped=0,
                            rejected=0,
                            child_jobs=999,
                            batches=1,
                        )
                    ),
                    _TIMESTAMP,
                    _TIMESTAMP,
                    _TIMESTAMP,
                    _TIMESTAMP,
                ),
            )

        scan = self._status()
        self.assertEqual(scan["id"], self.scan_job_id)
        self.assertEqual(scan["status"], "running")
        self.assertEqual(
            scan["counts"],
            {
                "processed": 7,
                "imported": 4,
                "skipped": 2,
                "rejected": 1,
                "child_jobs": 4,
                "batches": 2,
            },
        )
        self.assertNotIn("rogue.mov", _canonical_json(scan))

    def test_no_supported_media_is_terminal_zero_count_truth(self) -> None:
        self._update_job(
            status="succeeded",
            stage="no_supported_media",
            progress=1,
            checkpoint_json=_canonical_json(
                self._checkpoint(
                    cursor=None,
                    processed=0,
                    imported=0,
                    skipped=0,
                    rejected=0,
                    child_jobs=0,
                    batches=0,
                )
            ),
            finished_at=_TIMESTAMP,
        )
        scan = self._status()
        self.assertTrue(scan["no_supported_media"])
        self.assertEqual(scan["counts"], {key: 0 for key in scan["counts"]})
        self.assertIsNone(scan["eta_seconds"])
        self.assertFalse(scan["resumable"])

    def test_private_decomposed_unicode_cursor_is_admitted_but_never_projected(self) -> None:
        decomposed_cursor = "Cafe\u0301/片段.mov"
        self._update_job(
            status="running",
            stage="scanning",
            progress=0.25,
            checkpoint_json=_canonical_json(
                self._checkpoint(cursor=decomposed_cursor)
            ),
        )
        scan = self._status()
        self.assertEqual(scan["status"], "running")
        self.assertNotIn(decomposed_cursor, _canonical_json(scan))

    def test_retryable_failure_projects_only_closed_error_summary(self) -> None:
        error = {
            "object": "memolens.library_scan_error",
            "schema_version": "1",
            "code": "batch_failed",
            "stage": "scanning",
            "retryable": True,
            "detail_sha256": hashlib.sha256(
                f"private path: {self.library}".encode()
            ).hexdigest(),
        }
        self._update_job(
            status="failed",
            stage="failed",
            progress=0.5,
            checkpoint_json=_canonical_json(self._checkpoint()),
            error_json=_canonical_json(error),
            finished_at=_TIMESTAMP,
        )
        scan = self._status()
        self.assertEqual(
            scan["error"],
            {"code": "batch_failed", "retryable": True},
        )
        self.assertTrue(scan["resumable"])
        serialized = _canonical_json(scan)
        self.assertNotIn("detail_sha256", serialized)
        self.assertNotIn(str(self.library), serialized)

    def test_closed_status_stage_error_and_cancel_matrix(self) -> None:
        checkpoint = _canonical_json(self._checkpoint())
        cases = (
            ("running", "scanning", 0, None, False, _TIMESTAMP),
            ("cancelling", "scanning", 1, None, False, _TIMESTAMP),
            (
                "interrupted",
                "interrupted",
                0,
                self._error("worker_interrupted", stage="scanning"),
                True,
                _TIMESTAMP,
            ),
            (
                "cancelled",
                "cancelled",
                1,
                self._error("user_cancelled", stage="awaiting_scan_worker"),
                True,
                None,
            ),
            (
                "failed",
                "failed",
                0,
                self._error("root_binding_changed", stage="awaiting_scan_worker"),
                False,
                None,
            ),
            ("succeeded", "scan_complete", 0, None, False, _TIMESTAMP),
        )
        for status, stage, cancel, error, resumable, started_at in cases:
            with self.subTest(status=status, stage=stage):
                terminal = status in {
                    "interrupted",
                    "cancelled",
                    "failed",
                    "succeeded",
                }
                self._update_job(
                    status=status,
                    stage=stage,
                    progress=1 if status == "succeeded" else 0.5,
                    checkpoint_json=checkpoint,
                    cancel_requested=cancel,
                    error_json=error,
                    started_at=started_at,
                    finished_at=_TIMESTAMP if terminal else None,
                )
                scan = self._status()
                self.assertEqual((scan["status"], scan["stage"]), (status, stage))
                self.assertEqual(scan["cancel_requested"], bool(cancel))
                self.assertEqual(scan["resumable"], resumable)

        invalid_cases = (
            ("running", "scanning", 1, None, checkpoint),
            ("cancelling", "scanning", 0, None, checkpoint),
            ("interrupted", "interrupted", 0, None, checkpoint),
            (
                "failed",
                "failed",
                0,
                self._error("worker_interrupted", stage="scanning"),
                checkpoint,
            ),
            ("running", "scanning", 0, None, "{}"),
            ("partial", "scan_complete", 0, None, checkpoint),
            (
                "succeeded",
                "scan_complete",
                0,
                None,
                _canonical_json(
                    self._checkpoint(
                        cursor=None,
                        processed=0,
                        imported=0,
                        skipped=0,
                        rejected=0,
                        child_jobs=0,
                        batches=0,
                    )
                ),
            ),
        )
        for status, stage, cancel, error, raw_checkpoint in invalid_cases:
            with self.subTest(invalid_status=status, invalid_stage=stage):
                self._update_job(
                    status=status,
                    stage=stage,
                    progress=0.5,
                    checkpoint_json=raw_checkpoint,
                    cancel_requested=cancel,
                    error_json=error,
                    finished_at=(
                        _TIMESTAMP
                        if status
                        in {"interrupted", "cancelled", "failed", "succeeded"}
                        else None
                    ),
                )
                with self.assertRaises(MemoLensError) as captured:
                    ReadOnlyMemoLensStore(self.db_path, self.library).status()
                self.assertEqual(
                    captured.exception.code,
                    "library_scan_status_integrity_error",
                )

    def test_progress_and_error_causal_stage_match_core(self) -> None:
        checkpoint = _canonical_json(self._checkpoint())
        cases = (
            {
                "status": "running",
                "stage": "scanning",
                "progress": 1,
                "error_json": None,
                "started_at": _TIMESTAMP,
                "finished_at": None,
            },
            {
                "status": "succeeded",
                "stage": "scan_complete",
                "progress": 0.5,
                "error_json": None,
                "started_at": _TIMESTAMP,
                "finished_at": _TIMESTAMP,
            },
            {
                "status": "cancelled",
                "stage": "cancelled",
                "progress": 0,
                "cancel_requested": 1,
                "error_json": self._error("user_cancelled", stage="scanning"),
                "started_at": None,
                "finished_at": _TIMESTAMP,
            },
            {
                "status": "failed",
                "stage": "failed",
                "progress": 0.5,
                "error_json": self._error(
                    "root_binding_changed", stage="awaiting_scan_worker"
                ),
                "started_at": _TIMESTAMP,
                "finished_at": _TIMESTAMP,
            },
        )
        for case in cases:
            with self.subTest(case=case):
                self._update_job(
                    checkpoint_json=checkpoint,
                    heartbeat_at=_TIMESTAMP,
                    **case,
                )
                with self.assertRaises(MemoLensError) as captured:
                    self._status()
                self.assertEqual(
                    captured.exception.code,
                    "library_scan_status_integrity_error",
                )

    def test_timestamp_presence_and_order_fail_closed(self) -> None:
        checkpoint = _canonical_json(self._checkpoint())
        cases = (
            {
                "status": "queued",
                "stage": "awaiting_scan_worker",
                "started_at": _TIMESTAMP,
                "heartbeat_at": None,
                "finished_at": None,
            },
            {
                "status": "running",
                "stage": "scanning",
                "started_at": None,
                "heartbeat_at": _TIMESTAMP,
                "finished_at": None,
            },
            {
                "status": "succeeded",
                "stage": "scan_complete",
                "started_at": _TIMESTAMP,
                "heartbeat_at": _TIMESTAMP,
                "finished_at": None,
            },
            {
                "status": "running",
                "stage": "scanning",
                "started_at": _TIMESTAMP,
                "heartbeat_at": "2020-01-01T00:00:00+00:00",
                "finished_at": None,
            },
            {
                "status": "running",
                "stage": "scanning",
                "started_at": _TIMESTAMP,
                "heartbeat_at": "2026-08-30T12:00:00.000000+00:00",
                "finished_at": None,
            },
        )
        for case in cases:
            with self.subTest(case=case):
                self._update_job(
                    status=case["status"],
                    stage=case["stage"],
                    progress=0.5,
                    checkpoint_json=(
                        "{}" if case["status"] == "queued" else checkpoint
                    ),
                    started_at=case["started_at"],
                    heartbeat_at=case["heartbeat_at"],
                    finished_at=case["finished_at"],
                )
                with self.assertRaises(MemoLensError) as captured:
                    ReadOnlyMemoLensStore(self.db_path, self.library).status()
                self.assertEqual(
                    captured.exception.code,
                    "library_scan_status_integrity_error",
                )

    def test_corrupt_or_unknown_checkpoint_fails_closed(self) -> None:
        valid = self._checkpoint()
        cases = {
            "duplicate_decoded_key": _canonical_json(valid)[:-1]
            + ',"processed_count":8}',
            "escaped_duplicate_decoded_key": _canonical_json(valid)[:-1]
            + ',"processed\\u005fcount":8}',
            "unknown_key": _canonical_json({**valid, "root_path": str(self.library)}),
            "wrong_root_binding": _canonical_json(
                {**valid, "root_permission_fingerprint": "f" * 64}
            ),
            "invalid_counts": _canonical_json(
                {**valid, "processed_count": 1, "imported_count": 2}
            ),
            "positive_without_cursor": _canonical_json(
                {**valid, "last_relative_path": None}
            ),
            "positive_without_batch": _canonical_json(
                {**valid, "batch_count": 0}
            ),
            "noncanonical_cursor": _canonical_json(
                {**valid, "last_relative_path": "旅行/../escape.mov"}
            ),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                self._update_job(
                    status="running",
                    stage="scanning",
                    progress=0.25,
                    checkpoint_json=raw,
                )
                with self.assertRaises(MemoLensError) as captured:
                    ReadOnlyMemoLensStore(self.db_path, self.library).status()
                self.assertEqual(
                    captured.exception.code,
                    "library_scan_status_integrity_error",
                )
                self.assertNotIn(str(self.library), str(captured.exception))

    def test_corrupt_or_unknown_error_fails_closed(self) -> None:
        valid = {
            "object": "memolens.library_scan_error",
            "schema_version": "1",
            "code": "batch_failed",
            "stage": "scanning",
            "retryable": True,
            "detail_sha256": "d" * 64,
        }
        cases = {
            "duplicate_decoded_key": _canonical_json(valid)[:-1]
            + ',"code":"authority_invalid"}',
            "escaped_duplicate_decoded_key": _canonical_json(valid)[:-1]
            + ',"c\\u006fde":"authority_invalid"}',
            "unknown_key": _canonical_json(
                {**valid, "message": f"failed at {self.library}"}
            ),
            "wrong_retryability": _canonical_json(
                {**valid, "retryable": False}
            ),
            "unknown_code": _canonical_json({**valid, "code": "private_failure"}),
        }
        for label, raw in cases.items():
            with self.subTest(label=label):
                self._update_job(
                    status="failed",
                    stage="failed",
                    progress=0.5,
                    checkpoint_json=_canonical_json(self._checkpoint()),
                    error_json=raw,
                    finished_at=_TIMESTAMP,
                )
                with self.assertRaises(MemoLensError) as captured:
                    ReadOnlyMemoLensStore(self.db_path, self.library).status()
                self.assertEqual(
                    captured.exception.code,
                    "library_scan_status_integrity_error",
                )
                self.assertNotIn(str(self.library), str(captured.exception))

    def test_missing_receipt_anchored_job_fails_closed_without_fallback(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DELETE FROM media_jobs WHERE id=?",
                (self.scan_job_id,),
            )
            connection.execute(
                """INSERT INTO media_jobs(
                       id,database_uuid,kind,asset_id,analysis_run_id,status,stage,
                       progress,attempt,cancel_requested,checkpoint_json,error_json,
                       created_at,started_at,heartbeat_at,finished_at)
                     VALUES(?,?,'library_scan',NULL,NULL,'queued',
                            'awaiting_scan_worker',0,1,0,'{}',NULL,?,NULL,NULL,NULL)""",
                (f"job_{'b' * 32}", self.database_uuid, _TIMESTAMP),
            )
        with self.assertRaises(MemoLensError) as captured:
            ReadOnlyMemoLensStore(self.db_path, self.library).status()
        self.assertEqual(
            captured.exception.code,
            "library_scan_status_integrity_error",
        )

    def test_gateway_turns_private_scan_corruption_into_path_free_unavailable(self) -> None:
        private_cursor = f"{self.library}/do-not-disclose.mov"
        self._update_job(
            status="running",
            stage="scanning",
            progress=0.25,
            checkpoint_json=_canonical_json(
                {**self._checkpoint(), "last_relative_path": private_cursor}
            ),
        )
        status = MemoLensGateway(
            db_path=self.db_path,
            library_dir=self.library,
        ).status()
        self.assertEqual(status["status"], "unavailable")
        self.assertEqual(status["source"], "none")
        self.assertEqual(
            status["database"]["error_code"],
            "library_scan_status_integrity_error",
        )
        serialized = _canonical_json(status)
        self.assertNotIn(private_cursor, serialized)
        self.assertNotIn(str(self.library), serialized)
        self.assertNotIn("last_relative_path", serialized)


if __name__ == "__main__":
    unittest.main()
