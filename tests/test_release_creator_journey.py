"""One synthetic Library through the production Core to a real export.

This is a service integration test, not a real user's native approval or an
Agent/model journey. It deliberately uses no private library or provider.
"""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest

from PIL import Image

from backend.src.media.blueprint import BlueprintService
from backend.src.media.canonical_export import CanonicalExportJobRunner, CanonicalExportService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import TimelineLoweringService
from backend.src.media.video import MediaJobRunner, ffprobe, resolve_binary
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
from indexing.embeddings import EmbeddingService
from tests.test_coverage_persistence import semantic


class ReleaseCreatorJourneyTests(unittest.TestCase):
    def test_bootstrap_scan_edit_export_and_reopen_preserve_one_project(self) -> None:
        self.assertIsNotNone(resolve_binary("ffmpeg"), "Install FFmpeg before the release gate")
        with tempfile.TemporaryDirectory(prefix="memolens-release-journey-") as directory:
            root = Path(directory).resolve()
            library = root / "library"
            library.mkdir()
            output = root / "output"
            output.mkdir()
            for name, color in (("opening.jpg", "#426b5a"), ("ending.jpg", "#d4ad71")):
                Image.new("RGB", (160, 90), color=color).save(library / name)
            original_hashes = self._hashes(library)
            database = root / "state" / "media.db"
            database.parent.mkdir()
            ImageIndexRepository(database).ensure_schema()
            repository = MediaRepository(database)
            repository.ensure_schema(None)
            embedding = EmbeddingService(SimpleNamespace(
                embedding_backend="semantic_hash", semantic_vector_dimensions=8,
                embedding_device=None,
            ))
            runner = MediaJobRunner(repository, root / "cache", image_embedding_service=embedding)
            export_runner = CanonicalExportJobRunner(repository, root / "exports-cache")
            try:
                self._bootstrap(repository, library)
                context = repository.list_recoverable_library_scans()[0]
                runner.activate_runtime_generation(
                    "runtime_generation_" + "c" * 64,
                    active_library_root_id=str(context["library_root_id"]),
                )
                runner.recover()
                self._wait_scan(repository, str(context["job_id"]))
                with closing(sqlite3.connect(database)) as connection:
                    project_id = connection.execute(
                        "SELECT project_id FROM library_bootstrap_receipts"
                    ).fetchone()[0]
                    asset_ids = [row[0] for row in connection.execute(
                        "SELECT id FROM assets WHERE kind='image' ORDER BY id"
                    )]
                self.assertEqual(len(asset_ids), 2)
                for asset_id in asset_ids:
                    current = repository.get_current_image_analysis(asset_id)
                    self.assertIsNotNone(current)
                    self.assertEqual(current["projection"]["status"], "current")

                blueprint = repository.get_blueprint_head(project_id)
                proposal = semantic("release journey", evidence_ref=f"memolens://evidence/asset/{asset_ids[0]}")
                proposal["material_hints"].append({
                    "hint_id": "ending_image",
                    "evidence_ref": f"memolens://evidence/asset/{asset_ids[1]}",
                    "script_block_ids": ["ending"],
                    "reason": "Use the second locally scanned synthetic image.",
                })
                BlueprintService(repository).commit_proposal(project_id, {
                    "expected_head": self._pick(blueprint, "revision", "content_sha256"),
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": proposal,
                }, idempotency_key="release-blueprint")
                blueprint = repository.get_blueprint_head(project_id)
                CoverageService(repository).materialize_baseline(project_id, {
                    "expected_blueprint": self._pick(blueprint, "revision", "content_sha256", "semantic_sha256"),
                    "expected_plan_head": None,
                }, idempotency_key="release-coverage", expected_database_uuid=repository.database_uuid)
                coverage = repository.get_coverage_head(project_id)
                command = {
                    "expected_blueprint": self._pick(blueprint, "revision", "content_sha256", "semantic_sha256", "operation_id"),
                    "expected_coverage": self._pick(coverage, "revision", "content_sha256", "evidence_manifest_sha256", "operation_id"),
                    "expected_timeline_head": None,
                }
                timelines = TimelineLoweringService(repository)
                timelines.materialize_first_cut(project_id, command, idempotency_key="release-first-cut", expected_database_uuid=repository.database_uuid)
                before = timelines.read(project_id)
                clips = before["timeline"]["tracks"][0]["clips"]
                self.assertEqual(len(clips), 2)
                edit = {**command, "expected_timeline_head": before["head"], "edit": {
                    "op": "set_clip_duration", "clip_id": clips[0]["clip_id"], "duration_ms": 1250,
                }}
                saved = timelines.apply_edit(project_id, edit, idempotency_key="release-edit", expected_database_uuid=repository.database_uuid)
                replay = timelines.apply_edit(project_id, edit, idempotency_key="release-edit", expected_database_uuid=repository.database_uuid)
                self.assertTrue(replay.replayed)
                self.assertEqual(saved.response, replay.response)
                edited = timelines.read(project_id)
                self.assertEqual(edited["head"]["revision"], 2)

                exporter = CanonicalExportService(repository, export_runner)
                presentation = exporter.presentation(project_id)
                identity = output.stat()
                exported = exporter.start(project_id, {
                    "presentation": presentation["presentation"],
                    "presentation_sha256": presentation["presentation_sha256"],
                    "native_gesture_nonce": "synthetic-release-export-gesture",
                    "destination": {"canonical_path": str(output), "package_basename": "first-cut", "selection_identity": {
                        "device": str(identity.st_dev), "inode": str(identity.st_ino),
                    }},
                }, runtime_epoch="synthetic-release-runtime", idempotency_key="release-export")
                job_id = str(exported.response["job"]["id"])
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    job = repository.get_canonical_export_job(job_id)
                    if job["status"] in {"succeeded", "failed", "cancelled"}:
                        break
                    time.sleep(0.05)
                self.assertEqual(job["status"], "succeeded", job.get("error"))
                package = output / "first-cut"
                manifest = json.loads((package / "manifest.json").read_text())
                self.assertEqual(manifest["project_id"], project_id)
                for key in ("revision", "revision_sha256", "timeline_id", "timeline_content_sha256", "blueprint_binding", "coverage_binding"):
                    self.assertEqual(manifest["timeline_binding"][key], edited["head"][key])
                self.assertEqual(len(manifest["usage_occurrences"]), 2)
                probe = ffprobe(package / "video.mp4")
                self.assertEqual(probe["codec"]["audio_streams"], [])
                self.assertEqual((probe["width"], probe["height"]), (1080, 1920))
                self.assertLessEqual(abs(probe["duration_ms"] - edited["timeline"]["output"]["duration_ms"]), 34)
                self.assertTrue((package / "script.txt").is_file())
                self.assertTrue((package / "使用清单.txt").is_file())
                self.assertEqual(self._hashes(library), original_hashes)
                uuid = repository.database_uuid
            finally:
                export_runner.shutdown()
                runner.shutdown()
                repository.close()
            reopened = MediaRepository(database)
            try:
                reopened.ensure_schema(library)
                self.assertEqual(reopened.database_uuid, uuid)
                self.assertEqual(reopened.get_canonical_timeline_head(project_id)["revision"], 2)
                self.assertEqual(len(reopened.list_canonical_usage_occurrences(project_id=project_id, export_revision=1)), 2)
            finally:
                reopened.close()

    @staticmethod
    def _pick(value, *keys):
        return {key: value[key] for key in keys}

    @staticmethod
    def _hashes(library):
        return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in library.iterdir()}

    def _bootstrap(self, repository, library):
        descriptor = os.open(library, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        identity = os.fstat(descriptor)
        now = int(time.time() * 1000)
        try:
            result = repository.commit_library_bootstrap(
                request_id="lb_" + "f" * 64,
                intent_created_at_ms=now - 1000,
                intent_expires_at_ms=now + 299000,
                native_confirmed_at_ms=now,
                candidate_binding_sha256=repository.library_bootstrap_candidate_binding_sha256(
                    canonical_root=library,
                    expected_library_root_device=identity.st_dev,
                    expected_library_root_inode=identity.st_ino,
                ),
                library_root_path=library, library_root_fd=descriptor,
                expected_library_root_device=identity.st_dev,
                expected_library_root_inode=identity.st_ino,
            )
            self.assertEqual(result["response_status"], 201)
        finally:
            os.close(descriptor)

    def _wait_scan(self, repository, job_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = repository.get_media_job(job_id)
            if job["status"] in {"failed", "cancelled"}:
                self.fail(str(job))
            jobs = repository.list_media_jobs(active=True, limit=20)
            if job["status"] == "succeeded" and not jobs:
                return
            time.sleep(0.05)
        self.fail("Synthetic Library scan and child analysis did not complete")
