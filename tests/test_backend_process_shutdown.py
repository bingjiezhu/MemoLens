from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import selectors
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

from PIL import Image

from backend.src.media.blueprint import BlueprintService
from backend.src.media.canonical_export import (
    CanonicalExportJobRunner,
    CanonicalExportService,
)
from backend.src.media.coverage import CoverageService
from backend.src.media.image_analysis import ImageAnalysisJobProcessor
from backend.src.media.timeline_lowering import TimelineLoweringService
from backend.src.process_lifecycle import run_managed_backend
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
from tests.test_coverage_persistence import semantic


_CHILD_ARGUMENT = "--managed-canonical-export-child"
_LONG_RENDER_DURATION_MS = 120_000
_IMAGE_RUNTIME_GENERATION = f"runtime_generation_{'d' * 64}"
_IMAGE_ANALYSIS_PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}
_IMAGE_EMBEDDING_DIMENSIONS = 8


class _CanonicalProjectFixture:
    def __init__(self, repository: MediaRepository, library: Path) -> None:
        self.repository = repository
        self.library = library
        self.blueprints = BlueprintService(repository)
        self.coverage = CoverageService(repository)
        self.timelines = TimelineLoweringService(repository)

    def _register_image(self, name: str, seed: bytes) -> dict[str, object]:
        path = self.library / name
        color = tuple(hashlib.sha256(seed).digest()[:3])
        with Image.new("RGB", (96, 64), color=color) as image:
            image.save(path, format="JPEG", quality=90)
        content = path.read_bytes()
        observed = path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="image",
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="image/jpeg",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        source_id = str(asset["asset_source_id"])
        database = os.stat(self.repository.db_path, follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=_IMAGE_RUNTIME_GENERATION,
            database_file_identity=(int(database.st_dev), int(database.st_ino)),
            asset_id=asset_id,
            source_id=source_id,
            analysis_profile=_IMAGE_ANALYSIS_PROFILE,
            expected_head=None,
            enqueue_scope="backend-process-shutdown/image-analysis",
            idempotency_key=f"backend-process-shutdown-image-{asset_id}",
        )
        ImageAnalysisJobProcessor(
            self.repository,
            embedding_service=SimpleNamespace(
                backend="semantic_hash",
                semantic_vector_dimensions=_IMAGE_EMBEDDING_DIMENSIONS,
            ),
        ).run(
            str(job["id"]),
            runtime_generation=_IMAGE_RUNTIME_GENERATION,
            expected_attempt=int(job["attempt"]),
            cancel_requested=lambda: False,
        )
        current = self.repository.get_current_image_analysis(asset_id)
        result = current.get("result") if isinstance(current, dict) else None
        stages = result.get("stages") if isinstance(result, dict) else None
        embedding = stages.get("embedding") if isinstance(stages, dict) else None
        embedding_output = (
            embedding.get("output") if isinstance(embedding, dict) else None
        )
        vectors = (
            embedding_output.get("vectors")
            if isinstance(embedding_output, dict)
            else None
        )
        if (
            current is None
            or current.get("source")
            != {"source_id": source_id, "availability": "available"}
            or not isinstance(current.get("analysis_binding"), dict)
            or not isinstance(result, dict)
            or result.get("analysis_profile")
            != {
                "id": _IMAGE_ANALYSIS_PROFILE["profile_id"],
                "version": _IMAGE_ANALYSIS_PROFILE["profile_version"],
                "content_sha256": _IMAGE_ANALYSIS_PROFILE["profile_sha256"],
            }
            or not isinstance(vectors, list)
            or len(vectors) != 1
            or not isinstance(vectors[0], dict)
            or vectors[0].get("model_id") != "semantic_hash"
            or not isinstance(current.get("projection"), dict)
            or current["projection"].get("status") != "current"
            or current["projection"].get("outcome") != "applied"
        ):
            raise RuntimeError(
                "shutdown fixture image did not reach the current canonical projection"
            )
        return asset

    def build(self) -> str:
        first = self._register_image("opening.jpg", b"shutdown-opening")
        second = self._register_image("ending.jpg", b"shutdown-ending")
        first_ref = f"memolens://evidence/asset/{first['id']}"
        proposed = semantic("shutdown", evidence_ref=first_ref)
        proposed["output"]["duration_target_ms"] = _LONG_RENDER_DURATION_MS
        proposed["material_hints"].append(
            {
                "hint_id": "ending_image",
                "evidence_ref": f"memolens://evidence/asset/{second['id']}",
                "script_block_ids": ["ending"],
                "reason": "Use the second exact image for the ending.",
            }
        )

        project = self.repository.create_project(
            "Managed shutdown oracle",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "backend-process-shutdown-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key="blueprint-shutdown-oracle",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            key: blueprint[key]
            for key in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint_binding[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="coverage-shutdown-oracle",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        coverage_binding = {
            key: coverage[key]
            for key in (
                "revision",
                "content_sha256",
                "evidence_manifest_sha256",
                "operation_id",
            )
        }
        self.timelines.materialize_first_cut(
            project_id,
            {
                "expected_blueprint": blueprint_binding,
                "expected_coverage": coverage_binding,
                "expected_timeline_head": None,
            },
            idempotency_key="timeline-shutdown-oracle",
            expected_database_uuid=self.repository.database_uuid,
        )
        return project_id


class _ManagedChildApp:
    def __init__(
        self,
        *,
        repository: MediaRepository,
        runner: CanonicalExportJobRunner,
        metadata: dict[str, object],
    ) -> None:
        self.extensions: dict[str, object] = {
            "canonical_export_job_runner": runner,
            "media_repository": repository,
        }
        self.metadata = metadata

    def run(self, **_kwargs: object) -> None:
        sys.stdout.write(json.dumps(self.metadata, sort_keys=True) + "\n")
        sys.stdout.flush()
        while True:
            signal.pause()


def _run_managed_child(root: Path) -> int:
    library = root / "library"
    output = root / "exports"
    state = root / "state"
    library.mkdir(parents=True)
    output.mkdir()
    state.mkdir()
    database = state / "media.db"
    ImageIndexRepository(database).ensure_schema()
    repository = MediaRepository(database)
    runner: CanonicalExportJobRunner | None = None
    managed = False
    try:
        repository.ensure_schema(library)
        fixture = _CanonicalProjectFixture(repository, library)
        project_id = fixture.build()
        runner = CanonicalExportJobRunner(repository, state / "media-cache")
        service = CanonicalExportService(repository, runner)
        presentation = service.presentation(project_id)
        selected_output = output.stat()
        submitted = service.start(
            project_id,
            {
                "presentation": presentation["presentation"],
                "presentation_sha256": presentation["presentation_sha256"],
                "native_gesture_nonce": "shutdown-oracle-native-gesture",
                "destination": {
                    "canonical_path": str(output),
                    "package_basename": "interrupted-export",
                    "selection_identity": {
                        "device": str(selected_output.st_dev),
                        "inode": str(selected_output.st_ino),
                    },
                },
            },
            runtime_epoch="runtime-shutdown-oracle",
            idempotency_key="canonical-export-shutdown-oracle",
        )
        job = submitted.response["job"]
        app = _ManagedChildApp(
            repository=repository,
            runner=runner,
            metadata={
                "database": str(database),
                "job_id": job["id"],
                "output": str(output),
                "cache": str(state / "media-cache" / "canonical-exports"),
                "python": [sys.version_info.major, sys.version_info.minor],
                "sqlite": sqlite3.sqlite_version,
            },
        )
        managed = True
        run_managed_backend(app, host="127.0.0.1", port=0, debug=False)
        (state / "managed-shutdown-complete").write_text("complete\n", encoding="utf-8")
        return 0
    finally:
        if not managed:
            if runner is not None:
                runner.shutdown()
            repository.close()


def _read_child_metadata(
    process: subprocess.Popen[str],
    *,
    timeout: float,
) -> dict[str, object]:
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        events = selector.select(timeout)
        if not events:
            raise AssertionError("managed backend child did not become ready")
        line = process.stdout.readline()
    finally:
        selector.close()
    if not line:
        raise AssertionError("managed backend child exited before readiness")
    value = json.loads(line)
    if type(value) is not dict:
        raise AssertionError("managed backend child metadata was not an object")
    return value


def _posix_processes() -> list[tuple[int, int, int, str]]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,comm="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    rows: list[tuple[int, int, int, str]] = []
    for line in completed.stdout.splitlines():
        columns = line.strip().split(None, 3)
        if len(columns) == 4:
            rows.append((int(columns[0]), int(columns[1]), int(columns[2]), columns[3]))
    return rows


def _wait_for_render_process(
    backend_pid: int,
    *,
    timeout: float,
) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for pid, parent_pid, process_group, command in _posix_processes():
            if (
                parent_pid == backend_pid
                and process_group == pid
                and Path(command).name == "ffmpeg"
            ):
                return pid, process_group
        time.sleep(0.05)
    raise AssertionError("canonical export did not start a session-isolated FFmpeg process")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@unittest.skipUnless(os.name == "posix", "process-group shutdown oracle is POSIX-only")
class BackendProcessShutdownIntegrationTests(unittest.TestCase):
    def test_sigterm_cancels_real_canonical_export_and_reaps_ffmpeg_group(self) -> None:
        repository_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="memolens-managed-shutdown-") as directory:
            root = Path(directory).resolve()
            environment = os.environ.copy()
            environment["PYTHONUNBUFFERED"] = "1"
            command = [
                sys.executable,
                "-W",
                "error::ResourceWarning",
                "-m",
                "tests.test_backend_process_shutdown",
                _CHILD_ARGUMENT,
                str(root),
            ]
            process = subprocess.Popen(
                command,
                cwd=repository_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            stderr = ""
            ffmpeg_group: int | None = None
            try:
                metadata = _read_child_metadata(process, timeout=30)
                self.assertEqual(metadata["python"], [3, 14])
                self.assertGreaterEqual(
                    tuple(int(part) for part in str(metadata["sqlite"]).split(".")[:2]),
                    (3, 50),
                )
                ffmpeg_pid, ffmpeg_group = _wait_for_render_process(
                    process.pid,
                    timeout=30,
                )
                self.assertEqual(ffmpeg_pid, ffmpeg_group)
                self.assertTrue(_process_group_exists(ffmpeg_group))

                os.kill(process.pid, signal.SIGTERM)
                _stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertFalse(_process_group_exists(ffmpeg_group))

                database = Path(str(metadata["database"]))
                with closing(sqlite3.connect(database)) as connection:
                    row = connection.execute(
                        """SELECT status,stage,cancel_requested,finished_at
                             FROM canonical_export_jobs WHERE id=?""",
                        (metadata["job_id"],),
                    ).fetchone()
                    self.assertIsNotNone(row)
                    assert row is not None
                    self.assertEqual(row[:3], ("cancelled", "cancelled", 1))
                    self.assertIsNotNone(row[3])
                    self.assertEqual(
                        tuple(
                            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                            for table in (
                                "canonical_export_revisions",
                                "canonical_usage_occurrences",
                            )
                        ),
                        (0, 0),
                    )
                self.assertTrue((root / "state" / "managed-shutdown-complete").is_file())
                self.assertFalse((Path(str(metadata["output"])) / "interrupted-export").exists())
                cache = Path(str(metadata["cache"]))
                self.assertEqual(list(cache.iterdir()) if cache.exists() else [], [])
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    _stdout, cleanup_stderr = process.communicate(timeout=5)
                    stderr += cleanup_stderr
                else:
                    process.communicate()
                if ffmpeg_group is not None and _process_group_exists(ffmpeg_group):
                    try:
                        os.killpg(ffmpeg_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == _CHILD_ARGUMENT:
        raise SystemExit(_run_managed_child(Path(sys.argv[2]).resolve()))
    unittest.main()
