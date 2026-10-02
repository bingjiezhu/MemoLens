from __future__ import annotations

import hashlib
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image

from backend.src.api.routes import api_blueprint
from backend.src.media.image_analysis import (
    ImageAnalysisJobProcessor,
    _probe_image_metadata,
)
from backend.src.media.video import MEDIA_JOB_KIND_REGISTRY, MediaJobRunner
from backend.src.runtime import RuntimeBundle, RuntimeManager
from core.db import ImageIndexRepository
from core.image_analysis_job_contract import canonical_sha256
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import MediaRepository


GENERATION_A = f"runtime_generation_{'a' * 64}"
GENERATION_B = f"runtime_generation_{'b' * 64}"
PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}


class _InjectedCheckpointCrash(BaseException):
    """Model process loss after SQLite commit, outside worker exception handling."""


class ImageAnalysisRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-image-runner-")
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
        database = os.stat(self.db_path, follow_symlinks=False)
        self.database_identity = (database.st_dev, database.st_ino)
        self.runners: list[MediaJobRunner] = []

    def tearDown(self) -> None:
        for runner in reversed(self.runners):
            runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def _enqueue(
        self,
        *,
        generation: str = GENERATION_A,
        key: str = "image-runner-1",
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

    def _enqueue_distinct_image(
        self,
        *,
        ordinal: int,
        key: str,
    ) -> tuple[str, dict[str, object]]:
        image_path = self.library / f"stage-{ordinal}.png"
        Image.new(
            "RGB",
            (24 + ordinal, 12 + ordinal),
            ((ordinal * 31) % 255, (ordinal * 47) % 255, (ordinal * 61) % 255),
        ).save(image_path)
        payload = image_path.read_bytes()
        observed = image_path.stat()
        asset = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path=image_path.name,
            filename=image_path.name,
            kind="image",
            sha256=hashlib.sha256(payload).hexdigest(),
            mime_type="image/png",
            file_size=len(payload),
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        job = self.repository.enqueue_image_analysis(
            runtime_generation=GENERATION_A,
            database_file_identity=self.database_identity,
            asset_id=asset_id,
            source_id=str(asset["asset_source_id"]),
            analysis_profile=PROFILE,
            expected_head=None,
            enqueue_scope="media-import/image-analysis",
            idempotency_key=key,
        )
        return asset_id, job

    def _publication_counts(
        self,
        *,
        asset_id: str,
        job_id: str,
    ) -> tuple[int, int, int, int]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return (
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_analysis_results WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0]
                ),
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_analysis_heads WHERE asset_id=?",
                        (asset_id,),
                    ).fetchone()[0]
                ),
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_publish_receipts WHERE job_id=?",
                        (job_id,),
                    ).fetchone()[0]
                ),
                int(
                    connection.execute(
                        "SELECT COUNT(*) FROM image_projection_changes WHERE asset_id=?",
                        (asset_id,),
                    ).fetchone()[0]
                ),
            )

    def _runner(
        self,
        generation: str,
        *,
        image_embedding_service: object | None = None,
        image_provider_vision_egress: object | None = None,
    ) -> MediaJobRunner:
        runner = MediaJobRunner(
            self.repository,
            self.root / f"cache-{len(self.runners)}",
            image_embedding_service=image_embedding_service,
            image_provider_vision_egress=image_provider_vision_egress,  # type: ignore[arg-type]
        )
        runner.activate_runtime_generation(
            generation,
            active_library_root_id=self.root_id,
        )
        self.runners.append(runner)
        return runner

    def _wait_terminal(self, job_id: str, timeout: float = 10.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.repository.get_media_job(job_id)
            assert job is not None
            if job["status"] in {"cancelled", "failed", "partial", "succeeded"}:
                return job
            time.sleep(0.01)
        self.fail(f"image job did not become terminal: {self.repository.get_media_job(job_id)!r}")

    def _table_count(self, table: str) -> int:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def test_closed_dispatch_publishes_metadata_and_honestly_blocks_projection(self) -> None:
        self.assertEqual(
            MEDIA_JOB_KIND_REGISTRY,
            frozenset({"image_analysis", "library_scan", "video_index"}),
        )
        job = self._enqueue()
        job_id = str(job["id"])

        self._runner(GENERATION_A).submit(job_id)
        terminal = self._wait_terminal(job_id)

        self.assertEqual(
            (terminal["status"], terminal["stage"], terminal["error"]["code"]),
            ("failed", "shadow_projection", "projection_failed"),
        )
        current = self.repository.get_current_image_analysis(self.asset_id)
        assert current is not None
        self.assertEqual(current["result"]["stages"]["metadata"]["status"], "succeeded")
        for stage in ("geocode", "vision", "embedding", "quality"):
            self.assertEqual(current["result"]["stages"][stage]["status"], "disabled")
        self.assertEqual(current["projection"]["status"], "blocked")
        self.assertEqual(self._table_count("image_analysis_results"), 1)
        self.assertEqual(self._table_count("image_analysis_heads"), 1)
        self.assertEqual(self._table_count("image_projection_receipts"), 1)
        self.assertEqual(self._table_count("image_index"), 0)

    def test_configured_semantic_hash_projects_but_remains_text_derived(self) -> None:
        job = self._enqueue(key="image-runner-semantic-hash")
        service = SimpleNamespace(
            backend="semantic_hash",
            semantic_vector_dimensions=8,
        )

        self._runner(
            GENERATION_A,
            image_embedding_service=service,
        ).submit(str(job["id"]))
        terminal = self._wait_terminal(str(job["id"]))

        self.assertEqual(
            (terminal["status"], terminal["stage"]),
            ("succeeded", "completed"),
            terminal,
        )
        self.assertIsNone(terminal["error"])
        current = self.repository.get_current_image_analysis(self.asset_id)
        assert current is not None
        embedding = current["result"]["stages"]["embedding"]
        self.assertEqual(embedding["status"], "succeeded")
        self.assertEqual(
            embedding["output"]["vectors"],
            [
                {
                    "artifact_sha256": hashlib.sha256(bytes(8 * 4)).hexdigest(),
                    "dimensions": 8,
                    "model_id": "semantic_hash",
                    "purpose": "image_search",
                    "signal": "text_derived",
                }
            ],
        )
        self.assertEqual(
            (current["projection"]["status"], current["projection"]["outcome"]),
            ("current", "applied"),
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT embedding_backend,length(embedding) FROM image_index WHERE id=?",
                (self.asset_id,),
            ).fetchone()
        self.assertEqual(row, ("text_derived:semantic_hash", 32))

    def test_provider_vision_output_is_published_and_binds_projected_text(self) -> None:
        job = self._enqueue(key="image-runner-provider-vision")
        output = {
            "description": "A violet studio wall.",
            "tags": ["studio", "violet"],
            "location_hint": "Los Angeles",
        }
        output_bytes = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        output_sha256 = hashlib.sha256(output_bytes).hexdigest()

        class _ControlledProviderVision:
            calls = 0

            def run(self, source, binding, runtime_generation, expected_attempt):  # type: ignore[no-untyped-def]
                self.calls += 1
                self.last_binding = dict(binding)
                self.last_runtime_generation = runtime_generation
                self.last_attempt = expected_attempt
                self.last_source_sha256 = source.binding["input_asset_sha256"]
                return (
                    {
                        "status": "succeeded",
                        "provenance": {
                            "producer_id": "memolens.provider.controlled-test",
                            "producer_version": "1",
                            "model_id": "controlled-vision-v1",
                            "model_version": "2026-08-29",
                            "rule_id": "memolens.provider-image-vision/v1",
                            "rule_version": "1",
                        },
                        "output": output,
                        "artifact_sha256": output_sha256,
                        "reason_code": None,
                    },
                    (
                        {
                            "stage": "vision",
                            "name": "stage_output",
                            "media_type": "application/json",
                            "signal": None,
                            "model_id": None,
                            "dimensions": None,
                            "artifact_sha256": output_sha256,
                            "bytes": output_bytes,
                        },
                    ),
                )

        provider = _ControlledProviderVision()
        embedding = SimpleNamespace(
            backend="semantic_hash",
            semantic_vector_dimensions=8,
        )
        self._runner(
            GENERATION_A,
            image_embedding_service=embedding,
            image_provider_vision_egress=provider,
        ).submit(str(job["id"]))
        terminal = self._wait_terminal(str(job["id"]))

        self.assertEqual(
            (terminal["status"], terminal["stage"]),
            ("succeeded", "completed"),
            terminal,
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(provider.last_runtime_generation, GENERATION_A)
        self.assertEqual(provider.last_attempt, 1)
        self.assertEqual(provider.last_source_sha256, self.asset_sha256)
        current = self.repository.get_current_image_analysis(self.asset_id)
        assert current is not None
        self.assertEqual(current["result"]["stages"]["vision"]["output"], output)
        self.assertEqual(
            current["result"]["stages"]["vision"]["artifact_sha256"],
            output_sha256,
        )
        self.assertEqual(current["projection"]["status"], "current")
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            artifact = connection.execute(
                """SELECT media_type,model_id,artifact_sha256,artifact_blob
                     FROM image_analysis_artifacts
                    WHERE asset_id=? AND analysis_run_id=?
                      AND stage='vision' AND name='stage_output'""",
                (
                    self.asset_id,
                    current["analysis_binding"]["analysis_run_id"],
                ),
            ).fetchone()
            projected = connection.execute(
                "SELECT description,tags_json,combined_text FROM image_index WHERE id=?",
                (self.asset_id,),
            ).fetchone()
        assert artifact is not None
        self.assertEqual(
            tuple(artifact),
            (
                "application/json",
                None,
                output_sha256,
                output_bytes,
            ),
        )
        assert projected is not None
        self.assertEqual(projected["description"], output["description"])
        self.assertEqual(json.loads(projected["tags_json"]), output["tags"])
        for expected_text in (
            "A violet studio wall.",
            "studio",
            "violet",
            "Los Angeles",
        ):
            self.assertIn(expected_text, projected["combined_text"])

    def test_production_import_route_dispatches_to_exact_bundle_runner(self) -> None:
        route_image = self.library / "route-import.png"
        Image.new("RGB", (30, 16), "teal").save(route_image)
        runner = MediaJobRunner(self.repository, self.root / "route-cache")
        settings = SimpleNamespace(
            db_path=self.db_path,
            image_library_dir=self.library,
        )
        bundle = RuntimeBundle.freeze(
            settings,
            {
                "media_repository": self.repository,
                "media_job_runner": runner,
            },
        )
        runner.activate_runtime_generation(
            bundle.generation_id,
            active_library_root_id=self.root_id,
        )
        self.runners.append(runner)
        manager = RuntimeManager(lambda _retired: None)
        manager.swap(bundle)
        app = Flask(__name__)
        app.config.update(
            DESKTOP_SESSION_TOKEN="desktop-token",
            SETTINGS=settings,
            TESTING=True,
        )
        app.extensions["runtime_manager"] = manager
        app.register_blueprint(api_blueprint)

        response = app.test_client().post(
            "/v1/assets/import",
            json={
                "db_path": str(self.db_path),
                "library_root_id": self.root_id,
                "relative_paths": [route_image.name],
                "recursive": False,
                "dry_run": False,
                "kinds": ["image"],
            },
            headers={
                "Idempotency-Key": "production-image-runner-route",
                "X-MemoLens-Desktop-Token": "desktop-token",
            },
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(len(response.json["jobs"]), 1)
        job_id = str(response.json["jobs"][0]["id"])
        terminal = self._wait_terminal(job_id)
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        self.assertEqual(binding["runtime_generation"], bundle.generation_id)
        self.assertEqual(
            (terminal["kind"], terminal["status"], terminal["error"]["code"]),
            ("image_analysis", "failed", "projection_failed"),
        )
        self.assertEqual(self._table_count("image_analysis_results"), 1)

    def test_restart_appends_generation_bound_attempt_and_replay_is_exact(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        runner = self._runner(GENERATION_B)

        runner.recover()
        terminal = self._wait_terminal(job_id)
        before_counts = tuple(
            self._table_count(table)
            for table in (
                "image_analysis_results",
                "image_analysis_heads",
                "image_publish_receipts",
                "image_projection_receipts",
            )
        )
        runner.submit(job_id)
        time.sleep(0.05)

        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        self.assertEqual((terminal["status"], terminal["attempt"]), ("failed", 2))
        self.assertEqual(binding["runtime_generation"], GENERATION_B)
        self.assertEqual(before_counts, (1, 1, 1, 1))
        self.assertEqual(
            tuple(
                self._table_count(table)
                for table in (
                    "image_analysis_results",
                    "image_analysis_heads",
                    "image_publish_receipts",
                    "image_projection_receipts",
                )
            ),
            before_counts,
        )
        self.assertEqual(self._table_count("image_analysis_attempt_authorities"), 2)

    def test_runtime_mismatch_is_rejected_without_claim_or_publication(self) -> None:
        job = self._enqueue(generation=GENERATION_A)
        job_id = str(job["id"])

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_runtime_generation_changed$",
        ):
            self._runner(GENERATION_B).submit(job_id)
        observed = self.repository.get_media_job(job_id)
        assert observed is not None

        self.assertEqual((observed["status"], observed["stage"], observed["attempt"]), ("queued", "queued", 1))
        self.assertEqual(self._table_count("image_analysis_results"), 0)
        with closing(sqlite3.connect(self.db_path)) as connection:
            attempt_state = connection.execute(
                "SELECT state,heartbeat_sequence FROM image_analysis_attempt_states WHERE job_id=?",
                (job_id,),
            ).fetchone()
        self.assertEqual(attempt_state, ("queued", 0))

    def test_active_library_switch_rejects_submit_and_recovery_without_job_write(
        self,
    ) -> None:
        job = self._enqueue(generation=GENERATION_A, key="active-library-switch")
        job_id = str(job["id"])
        second_library = self.root / "second-library"
        second_library.mkdir()
        second_root_id = str(
            self.repository.register_library_root(second_library)["id"]
        )
        switched_runner = MediaJobRunner(
            self.repository,
            self.root / "active-library-switch-cache",
        )
        switched_runner.activate_runtime_generation(
            GENERATION_A,
            active_library_root_id=second_root_id,
        )
        self.runners.append(switched_runner)
        before_submit = self.repository.get_media_job(job_id)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "^image_library_scope_changed$",
        ):
            switched_runner.submit(job_id)

        self.assertEqual(self.repository.get_media_job(job_id), before_submit)
        self.assertEqual(self._table_count("image_analysis_attempt_authorities"), 1)

        self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
        before_recovery = self.repository.get_media_job(job_id)
        recovery_runner = MediaJobRunner(
            self.repository,
            self.root / "active-library-recovery-cache",
        )
        recovery_runner.activate_runtime_generation(
            GENERATION_B,
            active_library_root_id=second_root_id,
        )
        self.runners.append(recovery_runner)
        recovery_runner.recover()

        self.assertEqual(self.repository.get_media_job(job_id), before_recovery)
        self.assertEqual(self._table_count("image_analysis_attempt_authorities"), 1)

    def test_same_path_source_replacement_fails_before_publish(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        replacement = self.library / "replacement.png"
        Image.new("RGB", (24, 12), "orange").save(replacement)
        os.replace(replacement, self.image_path)

        self._runner(GENERATION_A).submit(job_id)
        terminal = self._wait_terminal(job_id)

        self.assertEqual(
            (terminal["status"], terminal["error"]["code"]),
            ("failed", "source_changed"),
        )
        self.assertEqual(self._table_count("image_analysis_results"), 0)
        self.assertEqual(self._table_count("image_analysis_heads"), 0)

    def test_running_cancel_finishes_exact_attempt_without_publishing(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        entered_probe = threading.Event()
        continue_probe = threading.Event()

        def blocked_probe(source):
            entered_probe.set()
            if not continue_probe.wait(timeout=5):
                raise RuntimeError("cancel test barrier timed out")
            return _probe_image_metadata(source)

        runner = self._runner(GENERATION_A)
        with patch(
            "backend.src.media.image_analysis._probe_image_metadata",
            side_effect=blocked_probe,
        ):
            runner.submit(job_id)
            self.assertTrue(entered_probe.wait(timeout=5))
            self.assertTrue(runner.cancel(job_id))
            continue_probe.set()
            terminal = self._wait_terminal(job_id)

        self.assertEqual(
            (terminal["status"], terminal["stage"], terminal["error"]["code"]),
            ("cancelled", "metadata", "cancelled"),
        )
        self.assertEqual(self._table_count("image_analysis_results"), 0)
        self.assertEqual(self._table_count("image_analysis_heads"), 0)

    def test_every_stage_checkpoint_survives_crash_and_recomputes_on_resume(
        self,
    ) -> None:
        stages = (
            "source_admission",
            "metadata",
            "geocode",
            "vision",
            "embedding",
            "quality",
        )
        processor = ImageAnalysisJobProcessor(self.repository)
        for ordinal, crash_stage in enumerate(stages, start=1):
            with self.subTest(stage=crash_stage):
                asset_id, job = self._enqueue_distinct_image(
                    ordinal=ordinal,
                    key=f"checkpoint-crash-{crash_stage}",
                )
                job_id = str(job["id"])
                initial_binding = self.repository.get_image_analysis_job_binding(
                    job_id
                )
                assert initial_binding is not None
                initial_revision = int(initial_binding["intended_revision"])
                original_checkpoint = (
                    self.repository.checkpoint_image_analysis_stage
                )

                def crash_after_commit(*args, **kwargs):
                    next_sequence = original_checkpoint(*args, **kwargs)
                    if kwargs["stage"] == crash_stage:
                        raise _InjectedCheckpointCrash(crash_stage)
                    return next_sequence

                with patch.object(
                    self.repository,
                    "checkpoint_image_analysis_stage",
                    side_effect=crash_after_commit,
                ):
                    with self.assertRaises(_InjectedCheckpointCrash):
                        processor.run(
                            job_id,
                            runtime_generation=GENERATION_A,
                            expected_attempt=1,
                            cancel_requested=lambda: False,
                        )

                crashed = self.repository.get_media_job(job_id)
                assert crashed is not None
                checkpoint = crashed["checkpoint"]
                completed_count = stages.index(crash_stage) + 1
                self.assertEqual(crashed["status"], "running")
                self.assertEqual(
                    checkpoint["completed_stages"],
                    list(stages[:completed_count]),
                )
                checkpoint_text = json.dumps(checkpoint, sort_keys=True)
                self.assertNotIn(str(self.library), checkpoint_text)
                self.assertNotIn(f"stage-{ordinal}.png", checkpoint_text)
                self.assertNotIn("credential", checkpoint_text.casefold())
                self.assertNotIn("provider_payload", checkpoint_text.casefold())
                first_checkpoint_sha256 = canonical_sha256(checkpoint)
                self.assertEqual(
                    self._publication_counts(asset_id=asset_id, job_id=job_id),
                    (0, 0, 0, 0),
                )
                with closing(sqlite3.connect(self.db_path)) as connection:
                    heartbeat_sequence = int(
                        connection.execute(
                            """SELECT heartbeat_sequence
                                 FROM image_analysis_attempt_states
                                WHERE job_id=? AND attempt=1""",
                            (job_id,),
                        ).fetchone()[0]
                    )
                self.assertEqual(heartbeat_sequence, 1 + completed_count)

                self.assertEqual(self.repository.mark_running_jobs_interrupted(), 1)
                resumed = self.repository.resume_image_analysis_job(
                    job_id,
                    runtime_generation=GENERATION_B,
                    database_file_identity=self.database_identity,
                    reason="process_recovery",
                )
                self.assertEqual(
                    resumed["attempt_authority"]["predecessor"][
                        "checkpoint_sha256"
                    ],
                    first_checkpoint_sha256,
                )
                resumed_binding = self.repository.get_image_analysis_job_binding(
                    job_id
                )
                assert resumed_binding is not None
                self.assertEqual(
                    int(resumed_binding["intended_revision"]),
                    initial_revision,
                )
                self.assertEqual(
                    resumed["checkpoint"]["completed_stages"],
                    [],
                )

                processor.run(
                    job_id,
                    runtime_generation=GENERATION_B,
                    expected_attempt=2,
                    cancel_requested=lambda: False,
                )
                terminal = self.repository.get_media_job(job_id)
                assert terminal is not None
                self.assertIn(terminal["status"], {"failed", "interrupted"})
                self.assertEqual(
                    (terminal["stage"], terminal["attempt"]),
                    ("shadow_projection", 2),
                )
                self.assertEqual(
                    self._publication_counts(asset_id=asset_id, job_id=job_id),
                    (1, 1, 1, 1),
                )

    def test_cancel_is_reliable_after_every_checkpoint_boundary(self) -> None:
        stages = (
            "source_admission",
            "metadata",
            "geocode",
            "vision",
            "embedding",
            "quality",
        )
        next_stages = (
            "metadata",
            "geocode",
            "vision",
            "embedding",
            "quality",
            "publish",
        )
        processor = ImageAnalysisJobProcessor(self.repository)
        for ordinal, (completed_stage, next_stage) in enumerate(
            zip(stages, next_stages, strict=True),
            start=20,
        ):
            with self.subTest(stage=completed_stage):
                asset_id, job = self._enqueue_distinct_image(
                    ordinal=ordinal,
                    key=f"checkpoint-cancel-{completed_stage}",
                )
                job_id = str(job["id"])
                cancel_issued = False

                def cancel_at_boundary() -> bool:
                    nonlocal cancel_issued
                    observed = self.repository.get_media_job(job_id)
                    if (
                        observed is not None
                        and observed["stage"] == next_stage
                        and not cancel_issued
                    ):
                        cancel_issued = self.repository.request_media_job_cancel(
                            job_id
                        )
                    observed = self.repository.get_media_job(job_id)
                    return observed is None or bool(observed["cancel_requested"])

                processor.run(
                    job_id,
                    runtime_generation=GENERATION_A,
                    expected_attempt=1,
                    cancel_requested=cancel_at_boundary,
                )
                terminal = self.repository.get_media_job(job_id)
                assert terminal is not None
                self.assertTrue(cancel_issued)
                self.assertEqual(
                    (
                        terminal["status"],
                        terminal["stage"],
                        terminal["error"]["code"],
                    ),
                    ("cancelled", next_stage, "cancelled"),
                )
                completed_count = stages.index(completed_stage) + 1
                self.assertEqual(
                    terminal["checkpoint"]["completed_stages"],
                    list(stages[:completed_count]),
                )
                self.assertEqual(
                    self._publication_counts(asset_id=asset_id, job_id=job_id),
                    (0, 0, 0, 0),
                )
                with closing(sqlite3.connect(self.db_path)) as connection:
                    state = connection.execute(
                        """SELECT state,heartbeat_sequence
                             FROM image_analysis_attempt_states
                            WHERE job_id=? AND attempt=1""",
                        (job_id,),
                    ).fetchone()
                self.assertEqual(state, ("cancelled", 1 + completed_count))

    def test_unexpected_worker_exception_finishes_with_closed_internal_error(self) -> None:
        job = self._enqueue()
        job_id = str(job["id"])
        runner = self._runner(GENERATION_A)

        with patch(
            "backend.src.media.image_analysis._probe_image_metadata",
            side_effect=RuntimeError("injected worker failure"),
        ):
            runner.submit(job_id)
            terminal = self._wait_terminal(job_id)

        self.assertEqual(
            (
                terminal["status"],
                terminal["stage"],
                terminal["error"]["code"],
                terminal["error"]["retryable"],
            ),
            ("failed", "metadata", "internal_error", True),
        )
        self.assertEqual(self._table_count("image_analysis_results"), 0)
        self.assertEqual(self._table_count("image_analysis_heads"), 0)


if __name__ == "__main__":
    unittest.main()
