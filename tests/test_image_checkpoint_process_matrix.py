from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from PIL import Image

from backend.src.media.video import MediaJobRunner
from core.db import ImageIndexRepository
from core.image_analysis_job_contract import canonical_sha256
from core.media_db import MediaRepository


GENERATION_A = f"runtime_generation_{'a' * 64}"
GENERATION_B = f"runtime_generation_{'b' * 64}"
PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}
WORK_STAGES = (
    "source_admission",
    "metadata",
    "geocode",
    "vision",
    "embedding",
    "quality",
)
COMMIT_BOUNDARIES = (*WORK_STAGES, "publish", "shadow_projection")
SIDES = ("before", "after")
_WORKER_MODE = "MEMOLENS_IMAGE_PROCESS_MATRIX_MODE"
_FIXTURE_PATH = "MEMOLENS_IMAGE_PROCESS_MATRIX_FIXTURE"
_MARKER_PATH = "MEMOLENS_IMAGE_PROCESS_MATRIX_MARKER"
_RESULT_PATH = "MEMOLENS_IMAGE_PROCESS_MATRIX_RESULT"
_BOUNDARY = "MEMOLENS_IMAGE_PROCESS_MATRIX_BOUNDARY"
_SIDE = "MEMOLENS_IMAGE_PROCESS_MATRIX_SIDE"


def _atomic_json_write(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _json_value(value: object) -> object:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AssertionError("expected a persisted JSON string")
    return json.loads(value)


def _database_snapshot(db_path: Path) -> dict[str, object]:
    tables = (
        "analysis_runs",
        "media_jobs",
        "image_analysis_job_bindings",
        "image_analysis_attempt_authorities",
        "image_analysis_attempt_states",
        "image_analysis_results",
        "image_analysis_heads",
        "image_publish_receipts",
        "image_projection_changes",
        "image_projection_receipts",
        "image_projection_manifests",
        "image_projection_generations",
        "image_index",
    )
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in tables
        }
        job_row = connection.execute(
            """SELECT id,status,stage,attempt,checkpoint_json,error_json,
                      analysis_run_id,asset_id
                 FROM media_jobs WHERE kind='image_analysis'"""
        ).fetchone()
        binding_row = connection.execute(
            """SELECT job_id,analysis_run_id,intended_revision,request_sha256,
                      source_binding_sha256
                 FROM image_analysis_job_bindings"""
        ).fetchone()
        head_row = connection.execute(
            """SELECT asset_id,analysis_run_id,revision,content_sha256
                 FROM image_analysis_heads"""
        ).fetchone()
        result_row = connection.execute(
            """SELECT job_id,asset_id,analysis_run_id,revision,content_sha256
                 FROM image_analysis_results"""
        ).fetchone()
        authorities = [
            _json_value(row["authority_json"])
            for row in connection.execute(
                """SELECT authority_json
                     FROM image_analysis_attempt_authorities
                    ORDER BY attempt"""
            ).fetchall()
        ]
        states = [
            dict(row)
            for row in connection.execute(
                """SELECT attempt,authority_sha256,state,heartbeat_sequence
                     FROM image_analysis_attempt_states
                    ORDER BY attempt"""
            ).fetchall()
        ]
        publication_join_gaps = int(
            connection.execute(
                """SELECT COUNT(*)
                     FROM image_analysis_results result
                     LEFT JOIN image_analysis_heads head
                       ON head.asset_id=result.asset_id
                      AND head.analysis_run_id=result.analysis_run_id
                      AND head.revision=result.revision
                      AND head.content_sha256=result.content_sha256
                     LEFT JOIN image_publish_receipts receipt
                       ON receipt.job_id=result.job_id
                      AND receipt.analysis_run_id=result.analysis_run_id
                      AND receipt.revision=result.revision
                      AND receipt.content_sha256=result.content_sha256
                     LEFT JOIN image_projection_changes change
                       ON change.position=receipt.change_position
                      AND change.asset_id=result.asset_id
                      AND change.analysis_run_id=result.analysis_run_id
                      AND change.revision=result.revision
                      AND change.content_sha256=result.content_sha256
                    WHERE head.asset_id IS NULL OR receipt.job_id IS NULL
                       OR change.position IS NULL"""
            ).fetchone()[0]
        )
    job = dict(job_row) if job_row is not None else None
    if job is not None:
        job["checkpoint"] = _json_value(job.pop("checkpoint_json"))
        job["error"] = _json_value(job.pop("error_json"))
    return {
        "counts": counts,
        "job": job,
        "binding": dict(binding_row) if binding_row is not None else None,
        "head": dict(head_row) if head_row is not None else None,
        "result": dict(result_row) if result_row is not None else None,
        "authorities": authorities,
        "states": states,
        "publication_join_gaps": publication_join_gaps,
    }


class _CommitBarrierRepository(MediaRepository):
    """Pause an external process immediately before or after a real commit."""

    def __init__(
        self,
        db_path: Path,
        *,
        boundary: str,
        side: str,
        marker_path: Path,
    ) -> None:
        super().__init__(db_path)
        self._process_matrix_boundary = boundary
        self._process_matrix_side = side
        self._process_matrix_marker_path = marker_path
        self._process_matrix_active_operation: str | None = None

    def _block_at_barrier(self, operation: str, side: str) -> None:
        if (
            operation != self._process_matrix_boundary
            or side != self._process_matrix_side
        ):
            return
        _atomic_json_write(
            self._process_matrix_marker_path,
            {
                "object": "memolens.image_process_commit_barrier",
                "operation": operation,
                "side": side,
                "pid": os.getpid(),
            },
        )
        threading.Event().wait()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        operation = self._process_matrix_active_operation
        with super().transaction(immediate=immediate) as connection:
            yield connection
            if immediate and operation is not None:
                self._block_at_barrier(operation, "before")
        if immediate and operation is not None:
            self._block_at_barrier(operation, "after")

    @contextmanager
    def _operation(self, operation: str) -> Iterator[None]:
        if self._process_matrix_active_operation is not None:
            raise AssertionError("process matrix operations must not nest")
        self._process_matrix_active_operation = operation
        try:
            yield
        finally:
            self._process_matrix_active_operation = None

    def enqueue_image_analysis(self, **kwargs: object) -> dict[str, object]:
        with self._operation("enqueue"):
            return super().enqueue_image_analysis(**kwargs)  # type: ignore[arg-type]

    def checkpoint_image_analysis_stage(
        self,
        *args: object,
        **kwargs: object,
    ) -> int:
        stage = kwargs.get("stage")
        if not isinstance(stage, str):
            raise AssertionError("checkpoint barrier requires an exact stage")
        with self._operation(stage):
            return super().checkpoint_image_analysis_stage(  # type: ignore[arg-type]
                *args,
                **kwargs,
            )

    def publish_image_analysis(
        self,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        with self._operation("publish"):
            return super().publish_image_analysis(  # type: ignore[arg-type]
                *args,
                **kwargs,
            )

    def project_image_analysis_change(
        self,
        *args: object,
        **kwargs: object,
    ) -> dict[str, object]:
        with self._operation("shadow_projection"):
            return super().project_image_analysis_change(  # type: ignore[arg-type]
                *args,
                **kwargs,
            )


def _load_fixture() -> tuple[dict[str, object], Path]:
    fixture_path = Path(os.environ[_FIXTURE_PATH]).resolve(strict=True)
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("invalid process matrix fixture")
    return payload, fixture_path.parent


def _enqueue(
    repository: MediaRepository,
    fixture: dict[str, object],
    *,
    runtime_generation: str,
) -> dict[str, object]:
    database = os.stat(Path(str(fixture["db_path"])), follow_symlinks=False)
    return repository.enqueue_image_analysis(
        runtime_generation=runtime_generation,
        database_file_identity=(int(database.st_dev), int(database.st_ino)),
        asset_id=str(fixture["asset_id"]),
        source_id=str(fixture["source_id"]),
        analysis_profile=PROFILE,
        expected_head=None,
        enqueue_scope="process-matrix/image-analysis",
        idempotency_key="process-matrix-one",
    )


def _crash_worker() -> None:
    fixture, fixture_root = _load_fixture()
    boundary = os.environ[_BOUNDARY]
    side = os.environ[_SIDE]
    repository = _CommitBarrierRepository(
        Path(str(fixture["db_path"])),
        boundary=boundary,
        side=side,
        marker_path=Path(os.environ[_MARKER_PATH]),
    )
    repository.bind_image_database_identity()
    if boundary == "enqueue":
        _enqueue(repository, fixture, runtime_generation=GENERATION_A)
        raise AssertionError("enqueue crash barrier was not reached")

    job_id = str(fixture["job_id"])
    runner = MediaJobRunner(
        repository,
        fixture_root / "crash-cache",
        image_embedding_service=SimpleNamespace(
            backend="semantic_hash",
            semantic_vector_dimensions=8,
        ),
    )
    runner.activate_runtime_generation(
        GENERATION_A,
        active_library_root_id=str(fixture["root_id"]),
    )
    runner.submit(job_id)
    deadline = time.monotonic() + 15
    marker_path = Path(os.environ[_MARKER_PATH])
    while time.monotonic() < deadline:
        if marker_path.exists():
            threading.Event().wait()
        observed = repository.get_media_job(job_id)
        if observed is not None and observed.get("status") in {
            "cancelled",
            "failed",
            "interrupted",
            "partial",
            "succeeded",
        }:
            raise AssertionError(
                f"worker became terminal before {boundary}:{side}: {observed!r}"
            )
        time.sleep(0.01)
    raise TimeoutError(f"commit barrier was not reached: {boundary}:{side}")


def _recovery_worker() -> None:
    fixture, fixture_root = _load_fixture()
    db_path = Path(str(fixture["db_path"]))
    repository = MediaRepository(db_path)
    repository.bind_image_database_identity()
    repository.mark_running_jobs_interrupted()
    runner = MediaJobRunner(
        repository,
        fixture_root / "recovery-cache",
        image_embedding_service=SimpleNamespace(
            backend="semantic_hash",
            semantic_vector_dimensions=8,
        ),
    )
    runner.activate_runtime_generation(
        GENERATION_B,
        active_library_root_id=str(fixture["root_id"]),
    )
    runner.recover()
    job_id = str(fixture.get("job_id") or "")
    if not job_id:
        job = _enqueue(repository, fixture, runtime_generation=GENERATION_B)
        job_id = str(job["id"])
        runner.submit(job_id)

    deadline = time.monotonic() + 20
    terminal: dict[str, object] | None = None
    while time.monotonic() < deadline:
        observed = repository.get_media_job(job_id)
        if observed is not None and observed.get("status") in {
            "cancelled",
            "failed",
            "interrupted",
            "partial",
            "succeeded",
        }:
            terminal = observed
            break
        time.sleep(0.01)
    if terminal is None:
        terminal = repository.get_media_job(job_id)
    runner.shutdown()
    repository.close()
    result_path = Path(os.environ[_RESULT_PATH])
    _atomic_json_write(
        result_path,
        {
            "terminal": terminal,
            "snapshot": _database_snapshot(db_path),
        },
    )


def _worker_main() -> None:
    mode = os.environ.get(_WORKER_MODE)
    if mode == "crash":
        _crash_worker()
        return
    if mode == "recover":
        _recovery_worker()
        return
    raise AssertionError(f"unknown process matrix worker mode: {mode!r}")


class ImageCheckpointProcessMatrixTests(unittest.TestCase):
    maxDiff = None

    def _fixture(self, root: Path, *, enqueue: bool) -> dict[str, object]:
        library = root / "library"
        library.mkdir()
        db_path = root / "state" / "media.db"
        db_path.parent.mkdir()
        ImageIndexRepository(db_path).ensure_schema()
        repository = MediaRepository(db_path)
        repository.ensure_schema(library)
        repository.bind_image_database_identity()
        root_id = str(repository.library_roots()[0]["id"])
        image_path = library / "still.png"
        Image.new("RGB", (24, 12), "purple").save(image_path)
        payload = image_path.read_bytes()
        observed = image_path.stat()
        asset = repository.upsert_asset_source(
            root_id=root_id,
            relative_path=image_path.name,
            filename=image_path.name,
            kind="image",
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        fixture: dict[str, object] = {
            "db_path": str(db_path),
            "root_id": root_id,
            "asset_id": str(asset["id"]),
            "source_id": str(asset["asset_source_id"]),
            "job_id": None,
        }
        if enqueue:
            fixture["job_id"] = str(
                _enqueue(repository, fixture, runtime_generation=GENERATION_A)["id"]
            )
        repository.close()
        _atomic_json_write(root / "fixture.json", fixture)
        return fixture

    def _run_subprocess(
        self,
        root: Path,
        *,
        mode: str,
        boundary: str,
        side: str,
    ) -> subprocess.Popen[str]:
        environment = os.environ.copy()
        environment.update(
            {
                _WORKER_MODE: mode,
                _FIXTURE_PATH: str(root / "fixture.json"),
                _MARKER_PATH: str(root / "barrier.json"),
                _RESULT_PATH: str(root / "result.json"),
                _BOUNDARY: boundary,
                _SIDE: side,
                "PYTHONUNBUFFERED": "1",
            }
        )
        return subprocess.Popen(
            [sys.executable, "-m", "tests.test_image_checkpoint_process_matrix"],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def _kill_at_commit_barrier(
        self,
        root: Path,
        *,
        boundary: str,
        side: str,
    ) -> dict[str, object]:
        process = self._run_subprocess(
            root,
            mode="crash",
            boundary=boundary,
            side=side,
        )
        marker_path = root / "barrier.json"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not marker_path.exists():
            return_code = process.poll()
            if return_code is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"crash worker exited before {boundary}:{side}; "
                    f"rc={return_code}\nstdout={stdout}\nstderr={stderr}"
                )
            time.sleep(0.01)
        if not marker_path.exists():
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
            self.fail(
                f"timed out waiting for {boundary}:{side}\n"
                f"stdout={stdout}\nstderr={stderr}"
            )
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(
            (marker["operation"], marker["side"]),
            (boundary, side),
        )
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        self.assertLess(
            process.returncode or 0,
            0,
            f"worker was not hard-killed\nstdout={stdout}\nstderr={stderr}",
        )
        return _database_snapshot(Path(str(self._read_fixture(root)["db_path"])))

    @staticmethod
    def _read_fixture(root: Path) -> dict[str, object]:
        payload = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
        assert isinstance(payload, dict)
        return payload

    def _recover(
        self,
        root: Path,
        *,
        boundary: str,
        side: str,
    ) -> dict[str, object]:
        process = self._run_subprocess(
            root,
            mode="recover",
            boundary=boundary,
            side=side,
        )
        stdout, stderr = process.communicate(timeout=30)
        self.assertEqual(
            process.returncode,
            0,
            f"recovery worker failed for {boundary}:{side}\n"
            f"stdout={stdout}\nstderr={stderr}",
        )
        result_path = root / "result.json"
        self.assertTrue(result_path.is_file())
        result = json.loads(result_path.read_text(encoding="utf-8"))
        assert isinstance(result, dict)
        return result

    @staticmethod
    def _expected_crash_checkpoint(
        boundary: str,
        side: str,
    ) -> tuple[list[str], str]:
        if boundary in WORK_STAGES:
            index = WORK_STAGES.index(boundary)
            completed_count = index + (1 if side == "after" else 0)
            completed = list(WORK_STAGES[:completed_count])
            current = (
                WORK_STAGES[index + 1]
                if side == "after" and index + 1 < len(WORK_STAGES)
                else "publish"
                if side == "after"
                else boundary
            )
            return completed, current
        if boundary == "publish":
            return (
                [*WORK_STAGES, "publish"] if side == "after" else list(WORK_STAGES),
                "shadow_projection" if side == "after" else "publish",
            )
        if boundary == "shadow_projection":
            return (
                [*WORK_STAGES, "publish", "shadow_projection"]
                if side == "after"
                else [*WORK_STAGES, "publish"],
                "completed" if side == "after" else "shadow_projection",
            )
        raise AssertionError(f"unexpected boundary: {boundary}")

    def _assert_crash_snapshot(
        self,
        snapshot: dict[str, object],
        *,
        boundary: str,
        side: str,
    ) -> None:
        counts = snapshot["counts"]
        assert isinstance(counts, dict)
        job = snapshot["job"]
        if boundary == "enqueue" and side == "before":
            self.assertIsNone(job)
            self.assertEqual(counts["media_jobs"], 0)
            self.assertEqual(counts["image_analysis_attempt_authorities"], 0)
            return
        assert isinstance(job, dict)
        checkpoint = job["checkpoint"]
        assert isinstance(checkpoint, dict)
        if boundary == "enqueue":
            self.assertEqual((job["status"], job["stage"]), ("queued", "queued"))
            self.assertEqual(checkpoint["completed_stages"], [])
            self.assertEqual(checkpoint["current_stage"], "queued")
        else:
            completed, current = self._expected_crash_checkpoint(boundary, side)
            self.assertEqual(checkpoint["completed_stages"], completed)
            self.assertEqual(checkpoint["current_stage"], current)
            self.assertEqual(job["stage"], current)
            self.assertEqual(
                job["status"],
                "succeeded"
                if boundary == "shadow_projection" and side == "after"
                else "running",
            )

        publication_counts = tuple(
            int(counts[name])
            for name in (
                "image_analysis_results",
                "image_analysis_heads",
                "image_publish_receipts",
                "image_projection_changes",
                "image_projection_receipts",
                "image_index",
            )
        )
        if boundary in WORK_STAGES or boundary == "enqueue" or (
            boundary == "publish" and side == "before"
        ):
            self.assertEqual(publication_counts, (0, 0, 0, 0, 0, 0))
        elif boundary == "shadow_projection" and side == "after":
            self.assertEqual(publication_counts, (1, 1, 1, 1, 1, 1))
        else:
            self.assertEqual(publication_counts, (1, 1, 1, 1, 0, 0))
        self.assertEqual(snapshot["publication_join_gaps"], 0)

    def _assert_recovered(
        self,
        result: dict[str, object],
        crash_snapshot: dict[str, object],
        *,
        boundary: str,
        side: str,
    ) -> None:
        terminal = result["terminal"]
        snapshot = result["snapshot"]
        assert isinstance(terminal, dict)
        assert isinstance(snapshot, dict)
        counts = snapshot["counts"]
        assert isinstance(counts, dict)
        expected_attempt = (
            1
            if (boundary == "enqueue" and side == "before")
            or (boundary == "shadow_projection" and side == "after")
            else 2
        )
        self.assertEqual(
            (terminal["status"], terminal["stage"], terminal["attempt"]),
            ("succeeded", "completed", expected_attempt),
        )
        self.assertEqual(counts["media_jobs"], 1)
        self.assertEqual(counts["analysis_runs"], 1)
        self.assertEqual(counts["image_analysis_job_bindings"], 1)
        self.assertEqual(counts["image_analysis_attempt_authorities"], expected_attempt)
        self.assertEqual(counts["image_analysis_attempt_states"], expected_attempt)
        for table in (
            "image_analysis_results",
            "image_analysis_heads",
            "image_publish_receipts",
            "image_projection_changes",
            "image_projection_receipts",
            "image_projection_manifests",
            "image_projection_generations",
            "image_index",
        ):
            self.assertEqual(counts[table], 1, table)
        self.assertEqual(snapshot["publication_join_gaps"], 0)

        job = snapshot["job"]
        binding = snapshot["binding"]
        head = snapshot["head"]
        published_result = snapshot["result"]
        authorities = snapshot["authorities"]
        states = snapshot["states"]
        assert isinstance(job, dict)
        assert isinstance(binding, dict)
        assert isinstance(head, dict)
        assert isinstance(published_result, dict)
        assert isinstance(authorities, list)
        assert isinstance(states, list)
        checkpoint = job["checkpoint"]
        assert isinstance(checkpoint, dict)
        self.assertEqual(checkpoint["attempt"], expected_attempt)
        self.assertEqual(
            checkpoint["completed_stages"],
            [*WORK_STAGES, "publish", "shadow_projection"],
        )
        self.assertEqual(binding["intended_revision"], 1)
        self.assertEqual(head["revision"], 1)
        self.assertEqual(
            (head["asset_id"], head["analysis_run_id"], head["content_sha256"]),
            (
                published_result["asset_id"],
                published_result["analysis_run_id"],
                published_result["content_sha256"],
            ),
        )
        self.assertEqual(binding["analysis_run_id"], head["analysis_run_id"])
        self.assertEqual(states[-1]["state"], "finished")

        if expected_attempt == 2:
            crashed_job = crash_snapshot["job"]
            crashed_authorities = crash_snapshot["authorities"]
            assert isinstance(crashed_job, dict)
            assert isinstance(crashed_authorities, list)
            crashed_checkpoint = crashed_job["checkpoint"]
            first_authority = crashed_authorities[0]
            second_authority = authorities[1]
            assert isinstance(crashed_checkpoint, dict)
            assert isinstance(first_authority, dict)
            assert isinstance(second_authority, dict)
            self.assertEqual(
                second_authority["predecessor"],
                {
                    "attempt": 1,
                    "status": "interrupted",
                    "stage": crashed_job["stage"],
                    "checkpoint_sha256": canonical_sha256(crashed_checkpoint),
                    "authority_sha256": first_authority["authority_sha256"],
                },
            )

    def test_external_kill_restart_commit_matrix_converges_exactly_once(self) -> None:
        matrix = (
            (("enqueue", side) for side in SIDES),
            (
                (boundary, side)
                for boundary in COMMIT_BOUNDARIES
                for side in SIDES
            ),
        )
        cases = tuple(item for group in matrix for item in group)
        self.assertEqual(len(cases), 18)
        self.assertEqual(
            set(cases),
            {
                (boundary, side)
                for boundary in ("enqueue", *COMMIT_BOUNDARIES)
                for side in SIDES
            },
        )
        for boundary, side in cases:
            with self.subTest(boundary=boundary, side=side):
                with tempfile.TemporaryDirectory(
                    prefix=f"memolens-image-process-{boundary}-{side}-"
                ) as temporary:
                    root = Path(temporary).resolve()
                    self._fixture(root, enqueue=boundary != "enqueue")
                    crash_snapshot = self._kill_at_commit_barrier(
                        root,
                        boundary=boundary,
                        side=side,
                    )
                    self._assert_crash_snapshot(
                        crash_snapshot,
                        boundary=boundary,
                        side=side,
                    )
                    result = self._recover(
                        root,
                        boundary=boundary,
                        side=side,
                    )
                    self._assert_recovered(
                        result,
                        crash_snapshot,
                        boundary=boundary,
                        side=side,
                    )


if __name__ == "__main__":
    if os.environ.get(_WORKER_MODE):
        _worker_main()
    else:
        unittest.main()
