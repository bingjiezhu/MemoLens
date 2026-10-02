from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src.media.importing import MediaImportService
from backend.src.media.render import RENDER_JOB_IDENTITY_FIELDS, RenderJobRunner
from backend.src.media.render_plan import RENDER_PROFILE_REGISTRY
from backend.src.media.video import (
    MEDIA_JOB_IDENTITY_FIELDS,
    MEDIA_JOB_KIND_REGISTRY,
    MediaJobRunner,
)
from backend.src.media.worker_admission import job_identity
from core.db import ImageIndexRepository
from core.media_db import MediaRepository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = PROJECT_ROOT / "core" / "production_surface_inventory.v1.json"


def _worker_actions() -> set[str]:
    return {
        *(f"python_worker.media.{kind}" for kind in MEDIA_JOB_KIND_REGISTRY),
        *(f"render_profile.{profile}" for profile in RENDER_PROFILE_REGISTRY),
    }


def _assert_exact_worker_action_set(test: unittest.TestCase) -> None:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    declared = {
        str(action["id"])
        for action in inventory["actions"]
        if str(action["id"]).startswith("python_worker.media.")
        or str(action["id"]).startswith("render_profile.")
    }
    test.assertEqual(declared, _worker_actions())
    by_id = {str(action["id"]): action for action in inventory["actions"]}
    for action_id in declared:
        test.assertIn("worker-internal", by_id[action_id]["authority"])
        test.assertIn("source-identity", by_id[action_id]["authority"])


class _NoEffectMediaRepository:
    database_uuid = "db-media"

    def __init__(self, jobs: dict[str, dict[str, object]]) -> None:
        self.jobs = jobs
        self.effects: list[tuple[str, object]] = []

    def get_media_job(self, job_id: str) -> dict[str, object] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job is not None else None

    def update_media_job(self, job_id: str, **values: object) -> None:
        self.effects.append(("update_media_job", (job_id, values)))

    def request_media_job_cancel(self, job_id: str) -> bool:
        self.effects.append(("cancel", job_id))
        return True


class _NoEffectRenderRepository:
    database_uuid = "db-render"

    def __init__(self, jobs: dict[str, dict[str, object]]) -> None:
        self.jobs = jobs
        self.effects: list[tuple[str, object]] = []

    def get_render_job(self, job_id: str) -> dict[str, object] | None:
        job = self.jobs.get(job_id)
        return dict(job) if job is not None else None

    def update_render_job(self, job_id: str, **values: object) -> None:
        self.effects.append(("update_render_job", (job_id, values)))

    def request_render_cancel(self, job_id: str) -> bool:
        self.effects.append(("cancel", job_id))
        return True


def _media_job(job_id: str, *, status: str = "queued") -> dict[str, object]:
    return {
        "id": job_id,
        "database_uuid": "db-media",
        "kind": "video_index",
        "asset_id": f"asset-{job_id}",
        "analysis_run_id": f"run-{job_id}",
        "attempt": 1,
        "created_at": "2026-08-29T00:00:00Z",
        "status": status,
        "cancel_requested": False,
    }


def _render_job(job_id: str, profile: str, *, status: str = "queued") -> dict[str, object]:
    return {
        "id": job_id,
        "database_uuid": "db-render",
        "timeline_id": f"timeline-{job_id}",
        "timeline_revision": 1,
        "timeline_content_sha256": "a" * 64,
        "profile": profile,
        "output_root_id": "preview-root",
        "output_relative_path": f"{job_id}.mp4",
        "attempt": 1,
        "created_at": "2026-08-29T00:00:00Z",
        "status": status,
        "cancel_requested": False,
    }


class BackendWorkerSourceIdentityInventoryTests(unittest.TestCase):
    def test_media_and_render_workers_reject_missing_forged_cross_job_and_replayed_admission_before_effects(
        self,
    ) -> None:
        _assert_exact_worker_action_set(self)
        with tempfile.TemporaryDirectory(prefix="memolens-worker-admission-") as directory:
            root = Path(directory)
            media_jobs = {
                "media-one": _media_job("media-one"),
                "media-two": _media_job("media-two"),
            }
            media_repository = _NoEffectMediaRepository(media_jobs)
            media = MediaJobRunner(media_repository, root / "media-cache")  # type: ignore[arg-type]
            try:
                media._run("media-one")
                media._run("media-one", admission_ticket="forged")
                cross = media._admission_authority.issue(
                    "media-one",
                    job_identity(media_jobs["media-one"], fields=MEDIA_JOB_IDENTITY_FIELDS),
                )
                media._run("media-two", admission_ticket=cross)
                tampered = media._admission_authority.issue(
                    "media-one",
                    job_identity(media_jobs["media-one"], fields=MEDIA_JOB_IDENTITY_FIELDS),
                )
                original_run_id = media_jobs["media-one"]["analysis_run_id"]
                media_jobs["media-one"]["analysis_run_id"] = "run-rebound"
                media._run("media-one", admission_ticket=tampered)
                media_jobs["media-one"]["analysis_run_id"] = original_run_id
                cross_database = media._admission_authority.issue(
                    "media-one",
                    job_identity(
                        {
                            **media_jobs["media-one"],
                            "database_uuid": "db-other",
                        },
                        fields=MEDIA_JOB_IDENTITY_FIELDS,
                    ),
                )
                media_jobs["media-one"]["database_uuid"] = "db-other"
                media._run("media-one", admission_ticket=cross_database)
                media_jobs["media-one"]["database_uuid"] = "db-media"
                replay = media._admission_authority.issue(
                    "media-one",
                    job_identity(media_jobs["media-one"], fields=MEDIA_JOB_IDENTITY_FIELDS),
                )
                media_jobs["media-one"]["status"] = "cancelled"
                media._run("media-one", admission_ticket=replay)
                media_jobs["media-one"]["status"] = "queued"
                media._run("media-one", admission_ticket=replay)
                self.assertEqual(media_repository.effects, [])
            finally:
                media.shutdown()

            for profile in sorted(RENDER_PROFILE_REGISTRY):
                with self.subTest(profile=profile):
                    render_jobs = {
                        "render-one": _render_job("render-one", profile),
                        "render-two": _render_job("render-two", profile),
                    }
                    render_repository = _NoEffectRenderRepository(render_jobs)
                    render = RenderJobRunner(
                        render_repository,  # type: ignore[arg-type]
                        root / f"render-{profile}",
                        reconcile_on_start=False,
                    )
                    try:
                        render._run("render-one")
                        render._run("render-one", admission_ticket="forged")
                        cross = render._admission_authority.issue(
                            "render-one",
                            job_identity(
                                render_jobs["render-one"],
                                fields=RENDER_JOB_IDENTITY_FIELDS,
                            ),
                        )
                        render._run("render-two", admission_ticket=cross)
                        tampered = render._admission_authority.issue(
                            "render-one",
                            job_identity(
                                render_jobs["render-one"],
                                fields=RENDER_JOB_IDENTITY_FIELDS,
                            ),
                        )
                        original_output = render_jobs["render-one"]["output_relative_path"]
                        render_jobs["render-one"]["output_relative_path"] = "rebound.mp4"
                        render._run("render-one", admission_ticket=tampered)
                        render_jobs["render-one"]["output_relative_path"] = original_output
                        replay = render._admission_authority.issue(
                            "render-one",
                            job_identity(
                                render_jobs["render-one"],
                                fields=RENDER_JOB_IDENTITY_FIELDS,
                            ),
                        )
                        render_jobs["render-one"]["status"] = "cancelled"
                        render._run("render-one", admission_ticket=replay)
                        render_jobs["render-one"]["status"] = "queued"
                        render._run("render-one", admission_ticket=replay)
                        self.assertEqual(render_repository.effects, [])
                    finally:
                        render.shutdown()

    def test_import_rejects_root_or_component_replacement_before_domain_write(self) -> None:
        for replacement in ("preopen-root", "root", "component"):
            with self.subTest(replacement=replacement), tempfile.TemporaryDirectory(
                prefix=f"memolens-import-{replacement}-"
            ) as directory:
                root = Path(directory)
                library = root / "library"
                nested = library / "nested"
                nested.mkdir(parents=True)
                source = nested / "still.jpg"
                Image.new("RGB", (24, 12), "blue").save(source)
                db_path = root / "state" / "media.db"
                db_path.parent.mkdir()
                ImageIndexRepository(db_path).ensure_schema()
                repository = MediaRepository(db_path)
                repository.ensure_schema(library)
                root_id = str(repository.library_roots()[0]["id"])
                service = MediaImportService(repository, object())  # type: ignore[arg-type]
                if replacement == "preopen-root":
                    displaced = root / "displaced-library"
                    library.rename(displaced)
                    (library / "nested").mkdir(parents=True)
                    Image.new("RGB", (24, 12), "red").save(library / "nested" / "still.jpg")
                    with self.assertRaisesRegex(ValueError, "persisted filesystem identity"):
                        service.prepare_import(
                            root_id=root_id,
                            root=library,
                            payload={
                                "relative_paths": ["nested/still.jpg"],
                                "recursive": False,
                            },
                        )
                    with repository.transaction() as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
                        self.assertEqual(
                            connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0],
                            0,
                        )
                    repository.close()
                    continue
                plan = service.prepare_import(
                    root_id=root_id,
                    root=library,
                    payload={"relative_paths": ["nested/still.jpg"], "recursive": False},
                )

                if replacement == "root":
                    displaced = root / "displaced-library"
                    library.rename(displaced)
                    (library / "nested").mkdir(parents=True)
                    Image.new("RGB", (24, 12), "red").save(library / "nested" / "still.jpg")
                else:
                    displaced = library / "displaced-nested"
                    nested.rename(displaced)
                    nested.mkdir()
                    Image.new("RGB", (24, 12), "red").save(nested / "still.jpg")

                with self.assertRaises((OSError, ValueError)):
                    with repository.transaction(immediate=True) as connection:
                        service.apply_prepared(connection, root_id=root_id, plan=plan)
                with repository.transaction() as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 0)
                    self.assertEqual(
                        connection.execute("SELECT COUNT(*) FROM asset_sources").fetchone()[0],
                        0,
                    )
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM media_jobs").fetchone()[0], 0)
                repository.close()

    def test_media_and_render_source_identity_mismatch_precedes_probe_process_and_canonical_write(
        self,
    ) -> None:
        _assert_exact_worker_action_set(self)
        with tempfile.TemporaryDirectory(prefix="memolens-worker-source-") as directory:
            root = Path(directory)
            library = root / "library"
            library.mkdir()
            original = library / "clip.mp4"
            original.write_bytes(b"original-media-source")
            prior = original.stat()
            digest = hashlib.sha256(original.read_bytes()).hexdigest()
            displaced = library / "old.mp4"
            original.rename(displaced)
            original.write_bytes(displaced.read_bytes())
            source_record = {
                "id": "source-one",
                "asset_id": "asset-one",
                "availability": "available",
                "root_path": str(library),
                "relative_path": original.name,
                "source_file_id": str(prior.st_ino),
                "observed_size": prior.st_size,
                "observed_mtime_ns": prior.st_mtime_ns,
                "sha256": digest,
                "kind": "video",
                "codec": {"video_stream_index": 0, "audio_stream_index": None},
                "rotation_degrees": 0,
            }

            class MediaRepositoryFixture(_NoEffectMediaRepository):
                def __init__(self) -> None:
                    super().__init__({"media-one": _media_job("media-one")})
                    self.probe_writes = 0
                    self.analysis_commits = 0

                @staticmethod
                def get_asset(asset_id: str) -> dict[str, object] | None:
                    if asset_id != "asset-media-one":
                        return None
                    return {
                        "id": asset_id,
                        "asset_source_id": "source-one",
                        "sha256": digest,
                        "filename": original.name,
                    }

                @staticmethod
                def get_asset_source(source_id: str) -> dict[str, object] | None:
                    return dict(source_record) if source_id == "source-one" else None

                def mark_asset_failed(self, asset_id: str, code: str) -> None:
                    self.effects.append(("mark_asset_failed", (asset_id, code)))

                def update_asset_probe(self, _asset_id: str, _probe: object) -> None:
                    self.probe_writes += 1

                def commit_video_analysis(self, **_values: object) -> None:
                    self.analysis_commits += 1

            media_repository = MediaRepositoryFixture()
            media = MediaJobRunner(media_repository, root / "media-cache")  # type: ignore[arg-type]
            media_ticket = media._admission_authority.issue(
                "media-one",
                job_identity(media_repository.jobs["media-one"], fields=MEDIA_JOB_IDENTITY_FIELDS),
            )
            with (
                patch("backend.src.media.video.ffprobe") as probe,
                patch("backend.src.media.video.scan_video_frames") as scan,
                patch("backend.src.media.video.subprocess.Popen") as process,
            ):
                media._run("media-one", admission_ticket=media_ticket)
            self.assertEqual(media_repository.probe_writes, 0)
            self.assertEqual(media_repository.analysis_commits, 0)
            probe.assert_not_called()
            scan.assert_not_called()
            process.assert_not_called()
            media.shutdown()

            timeline = {
                "format": {
                    "width": 1920,
                    "height": 1080,
                    "fps": 30,
                    "duration_ms": 1_000,
                    "background_color": "#000000",
                },
                "tracks": [
                    {
                        "role": "primary",
                        "clips": [
                            {
                                "kind": "video",
                                "asset_source_id": "source-one",
                                "source_in_ms": 0,
                                "timeline_duration_ms": 1_000,
                                "audio_enabled": False,
                                "volume_db": 0.0,
                                "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                                "fit": "contain",
                            }
                        ],
                    }
                ],
                "transitions": [],
            }

            class RenderRepositoryFixture(_NoEffectRenderRepository):
                def __init__(self, profile: str) -> None:
                    super().__init__({"render-one": _render_job("render-one", profile)})
                    self.marked: list[tuple[str, str]] = []

                @staticmethod
                def get_asset_source(source_id: str) -> dict[str, object] | None:
                    return dict(source_record) if source_id == "source-one" else None

                def mark_source_availability(self, source_id: str, availability: str) -> None:
                    self.marked.append((source_id, availability))

            for profile in sorted(RENDER_PROFILE_REGISTRY):
                with self.subTest(source_profile=profile):
                    render_repository = RenderRepositoryFixture(profile)
                    render = RenderJobRunner(
                        render_repository,  # type: ignore[arg-type]
                        root / f"render-cache-{profile}",
                        reconcile_on_start=False,
                    )
                    ticket = render._admission_authority.issue(
                        "render-one",
                        job_identity(
                            render_repository.jobs["render-one"],
                            fields=RENDER_JOB_IDENTITY_FIELDS,
                        ),
                    )
                    with (
                        patch.object(render, "_load_validated_timeline", return_value=timeline),
                        patch("backend.src.media.render.ffmpeg_encode_capability") as capability,
                        patch("backend.src.media.render.subprocess.Popen") as process,
                        patch("backend.src.media.render.publish_render") as publish,
                    ):
                        render._run("render-one", admission_ticket=ticket)
                    capability.assert_not_called()
                    process.assert_not_called()
                    publish.assert_not_called()
                    self.assertEqual(render_repository.marked, [("source-one", "changed")])
                    terminal = [effect for effect in render_repository.effects if effect[0] == "update_render_job"]
                    self.assertEqual(len(terminal), 1)
                    self.assertEqual(terminal[0][1][1]["error"]["code"], "source_changed")
                    render.shutdown()


if __name__ == "__main__":
    unittest.main()
