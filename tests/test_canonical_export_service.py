from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image

from backend.src.media.blueprint import BlueprintService, BlueprintServiceError
from backend.src.media.canonical_export import (
    CanonicalExportJobRunner,
    CanonicalExportService,
    CanonicalExportServiceError,
)
from backend.src.media.canonical_export_artifacts import (
    inspect_existing_package,
    usage_text_from_manifest,
)
from backend.src.media.coverage import CoverageService, CoverageServiceError
from backend.src.media.director import CreativeDirector
from backend.src.media.retrieval import MixedRetrievalService
from backend.src.media.residual_parent import ResidualParentResolutionError
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import (
    TimelineLoweringService,
    TimelineLoweringServiceError,
)
from backend.src.media.video import ffprobe, resolve_binary
from core.db import ImageIndexRepository
from core.media_db import (
    CanonicalExportIntegrityError,
    MediaRepository,
    canonical_json,
    utc_now_iso,
)
from tests.test_coverage_persistence import semantic
from tests import test_timeline_lowering_integration as timeline_integration


class _SimulatedProcessCrash(BaseException):
    pass


class CanonicalExportServiceIntegrationTests(unittest.TestCase):
    _build_project = timeline_integration.TimelineLoweringIntegrationTests._build_project
    _materialize = timeline_integration.TimelineLoweringIntegrationTests._materialize

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-export-service-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.output = self.root / "exports"
        self.output.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.runner = CanonicalExportJobRunner(
            self.repository,
            self.root / "state" / "media-cache",
        )
        self.service = CanonicalExportService(self.repository, self.runner)

    def tearDown(self) -> None:
        self.runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def _assert_current_blueprint_rejected_read_only(
        self,
        project_id: str,
        *,
        code: str,
    ) -> BlueprintServiceError:
        original_transaction = self.repository.transaction
        observed_total_changes: list[int] = []

        @contextmanager
        def audited_transaction(*, immediate: bool = False):
            with original_transaction(immediate=immediate) as connection:
                starting_total_changes = connection.total_changes
                try:
                    yield connection
                finally:
                    observed_total_changes.append(
                        connection.total_changes - starting_total_changes
                    )

        with (
            patch.object(
                self.repository,
                "transaction",
                side_effect=audited_transaction,
            ),
            self.assertRaises(BlueprintServiceError) as raised,
        ):
            self.blueprints.get(project_id)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(observed_total_changes, [0])
        return raised.exception

    def _assert_current_workspace_rejected_read_only(
        self,
        project_id: str,
        *,
        code: str,
    ) -> BlueprintServiceError:
        original_transaction = self.repository.transaction
        observed_total_changes: list[int] = []

        @contextmanager
        def audited_transaction(*, immediate: bool = False):
            with original_transaction(immediate=immediate) as connection:
                starting_total_changes = connection.total_changes
                try:
                    yield connection
                finally:
                    observed_total_changes.append(
                        connection.total_changes - starting_total_changes
                    )

        with (
            patch.object(
                self.repository,
                "transaction",
                side_effect=audited_transaction,
            ),
            self.assertRaises(BlueprintServiceError) as raised,
        ):
            self.blueprints.project_workspace(project_id)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(observed_total_changes, [0])
        return raised.exception

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        color = tuple(hashlib.sha256(content).digest()[:3])
        path = self.library / name
        Image.new("RGB", (96, 64), color=color).save(
            path,
            format="JPEG",
            quality=90,
        )
        exact = path.read_bytes()
        return timeline_integration.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            exact,
        )

    def _register_real_video_span(self) -> tuple[str, Path, str]:
        path = self.library / "ending-real.mp4"
        ffmpeg_binary = resolve_binary("ffmpeg")
        self.assertIsNotNone(ffmpeg_binary)
        completed = subprocess.run(
            [
                str(ffmpeg_binary),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x31577a:s=160x90:r=30:d=12",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        exact = path.read_bytes()
        digest = hashlib.sha256(exact).hexdigest()
        observed = path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=path.name,
            filename=path.name,
            kind="video",
            sha256=digest,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        self.repository.update_asset_probe(asset_id, ffprobe(path))
        run_id = f"arun_{digest[:32]}"
        span_id = f"seg_{asset_id.removeprefix('asset_')}_1_0"
        now = utc_now_iso()
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,
                     analysis_profile_json,input_asset_sha256,status,
                     transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','canonical-export-real-video','{}',
                          ?,'succeeded','available','ready',?)""",
                (run_id, asset_id, digest, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                     boundary_reason,semantic_json,combined_text,visual_status,
                     transcript_status,created_at)
                   VALUES(?,?,?,0,1000,11000,'canonical-export-real-video',
                          '{}','','ready','available',?)""",
                (span_id, asset_id, run_id, now),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                     asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, run_id, now),
            )
        return f"memolens://evidence/span/{span_id}", path, digest

    def _build_mixed_media_project(
        self,
    ) -> tuple[str, dict[str, object], Path, str]:
        opening = self._register_image("opening-real.jpg", b"real-video-opening")
        opening_ref = f"memolens://evidence/asset/{opening['id']}"
        span_ref, video_path, video_sha256 = self._register_real_video_span()
        proposed = semantic("canonical mixed export", evidence_ref=opening_ref)
        proposed["material_hints"].append(
            {
                "hint_id": "ending_video_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact verified video span for the ending.",
            }
        )
        project = self.repository.create_project(
            "Canonical mixed export",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "canonical-export-service-test"},
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
            idempotency_key="blueprint-mixed-export",
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
            idempotency_key="coverage-mixed-export",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        timeline_payload = {
            "expected_blueprint": blueprint_binding,
            "expected_coverage": {
                key: coverage[key]
                for key in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }
        return project_id, timeline_payload, video_path, video_sha256

    def _wait_terminal(self, job_id: str, *, timeout: float = 60.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.repository.get_canonical_export_job(job_id)
            assert job is not None
            if job["status"] in {"succeeded", "failed", "cancelled"}:
                return job
            time.sleep(0.05)
        self.fail("canonical export did not reach a terminal state")

    def _wait_status(
        self,
        job_id: str,
        status: str,
        *,
        timeout: float = 60.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.repository.get_canonical_export_job(job_id)
            assert job is not None
            if job["status"] == status:
                return job
            time.sleep(0.05)
        self.fail(f"canonical export did not reach {status}")

    def _wait_runner_release(self, job_id: str, *, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.runner._lock:
                if job_id not in self.runner._submitted:
                    return
            time.sleep(0.01)
        self.fail("canonical export worker did not release the job")

    def _start_export(self, *, package_basename: str) -> tuple[str, dict[str, object]]:
        project_id, _blueprint, _coverage, timeline_payload = self._build_project()
        self._materialize(project_id, timeline_payload, key="timeline-export-service")
        return project_id, self._submit_export(
            project_id,
            package_basename=package_basename,
            idempotency_key="canonical-export-service-one",
        )

    def _submit_export(
        self,
        project_id: str,
        *,
        package_basename: str,
        idempotency_key: str,
        native_gesture_nonce: str = "native-export-gesture-0001",
    ) -> dict[str, object]:
        presentation = self.service.presentation(project_id)
        selected = self.output.stat()
        result = self.service.start(
            project_id,
            {
                "presentation": presentation["presentation"],
                "presentation_sha256": presentation["presentation_sha256"],
                "native_gesture_nonce": native_gesture_nonce,
                "destination": {
                    "canonical_path": str(self.output),
                    "package_basename": package_basename,
                    "selection_identity": {
                        "device": str(selected.st_dev),
                        "inode": str(selected.st_ino),
                    },
                },
            },
            runtime_epoch="runtime-export-service",
            idempotency_key=idempotency_key,
        )
        return result.response["job"]

    def test_real_ffmpeg_package_then_export_and_usage_commit_exactly_once(self) -> None:
        project_id, submitted = self._start_export(package_basename="memory-film")
        original_hashes = {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self.library.iterdir()
        }
        job = self._wait_terminal(str(submitted["id"]))
        self.assertEqual(job["status"], "succeeded", job.get("error"))

        package = self.output / "memory-film"
        self.assertEqual(
            set(path.name for path in package.iterdir()),
            {
                "video.mp4",
                "script.txt",
                "manifest.json",
                "使用清单.txt",
                ".memolens-complete.json",
            },
        )
        manifest_bytes = (package / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest_bytes, canonical_json(manifest).encode("utf-8"))
        self.assertEqual(manifest["job_id"], job["id"])
        self.assertEqual(manifest["project_id"], project_id)
        self.assertEqual(manifest["package_roles"][1], "script")
        self.assertEqual(len(manifest["usage_occurrences"]), 2)
        self.assertIn("MemoLens 使用清单", (package / "使用清单.txt").read_text())

        revision_number = int(job["result"]["revision"])
        revision = self.repository.get_canonical_export_revision(
            project_id=project_id,
            revision=revision_number,
        )
        assert revision is not None
        self.assertEqual(revision["job_id"], job["id"])
        self.assertEqual(revision["package_manifest"], manifest)
        self.assertEqual(revision["usage_occurrences"], manifest["usage_occurrences"])
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=revision_number,
            ),
            manifest["usage_occurrences"],
        )
        safe_job = self.service.read_job(project_id, str(job["id"]))
        safe_revision = self.service.read_revision(project_id, revision_number)
        safe_json = canonical_json({"job": safe_job, "revision": safe_revision})
        for secret in (
            str(self.output),
            str(job["output_binding"]["output_root_id"]),
            str(job["output_binding"]["permission_fingerprint"]),
            "canonical_path",
        ):
            self.assertNotIn(secret, safe_json)
        self.assertEqual(
            safe_job["result"]["human_usage_sha256"],
            hashlib.sha256((package / "使用清单.txt").read_bytes()).hexdigest(),
        )
        self.assertEqual(
            safe_job["result"]["completion_marker_sha256"],
            hashlib.sha256(
                (package / ".memolens-complete.json").read_bytes()
            ).hexdigest(),
        )
        blueprint_binding = manifest["timeline_binding"]["blueprint_binding"]
        script = self.repository.build_canonical_export_script_projection(
            project_id=project_id,
            blueprint_binding=blueprint_binding,
        )
        self.assertEqual((package / "script.txt").read_text(), script["text"])
        self.assertEqual(
            original_hashes,
            {
                path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.library.iterdir()
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in (
                        "canonical_export_revisions",
                        "canonical_usage_occurrences",
                        "render_jobs",
                        "export_grants",
                    )
                ),
                (1, 2, 0, 0),
            )

    def test_real_ffmpeg_image_and_video_span_is_video_only_and_exactly_used(
        self,
    ) -> None:
        project_id, timeline_payload, source_video, source_sha256 = (
            self._build_mixed_media_project()
        )
        self._materialize(
            project_id,
            timeline_payload,
            key="timeline-mixed-export-service",
        )
        submitted = self._submit_export(
            project_id,
            package_basename="mixed-media-film",
            idempotency_key="canonical-export-service-mixed",
        )
        job = self._wait_terminal(str(submitted["id"]))
        self.assertEqual(job["status"], "succeeded", job.get("error"))

        package = self.output / "mixed-media-film"
        manifest = json.loads((package / "manifest.json").read_bytes())
        occurrences = manifest["usage_occurrences"]
        self.assertEqual([row["media_kind"] for row in occurrences], ["image", "video"])
        video_usage = occurrences[1]
        self.assertEqual(video_usage["source_start_ms"], 1_000)
        self.assertEqual(
            video_usage["source_end_ms"] - video_usage["source_start_ms"],
            video_usage["timeline_end_ms"] - video_usage["timeline_start_ms"],
        )
        self.assertLessEqual(video_usage["source_end_ms"], 11_000)
        self.assertEqual(
            hashlib.sha256(source_video.read_bytes()).hexdigest(),
            source_sha256,
        )

        output_probe = ffprobe(package / "video.mp4")
        self.assertEqual(
            (output_probe["width"], output_probe["height"]),
            (1_080, 1_920),
        )
        self.assertEqual(output_probe["duration_ms"], 8_000)
        self.assertEqual(output_probe["codec"]["avg_frame_rate"], "30/1")
        self.assertIsNone(output_probe["codec"]["audio_stream_index"])
        self.assertEqual(output_probe["codec"]["audio_streams"], [])
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=int(job["result"]["revision"]),
            ),
            occurrences,
        )

    def test_real_successful_export_rename_and_copy_are_excluded_by_content(
        self,
    ) -> None:
        project_id, submitted = self._start_export(
            package_basename="derivative-source-film"
        )
        job = self._wait_terminal(str(submitted["id"]))
        self.assertEqual(job["status"], "succeeded", job.get("error"))

        rendered = self.output / "derivative-source-film" / "video.mp4"
        rendered_bytes = rendered.read_bytes()
        rendered_sha256 = hashlib.sha256(rendered_bytes).hexdigest()
        self.assertEqual(job["result"]["video_sha256"], rendered_sha256)

        renamed = self.library / "renamed-canonical-render.mp4"
        copied = self.library / "copied-canonical-render.mp4"
        renamed.write_bytes(rendered_bytes)
        copied.write_bytes(rendered_bytes)
        root_id = str(self.repository.register_library_root(self.library)["id"])
        registered: list[dict[str, object]] = []
        for path in (renamed, copied):
            observed = path.stat()
            registered.append(
                self.repository.upsert_asset_source(
                    root_id=root_id,
                    relative_path=path.name,
                    filename=path.name,
                    kind="video",
                    sha256=rendered_sha256,
                    mime_type="video/mp4",
                    file_size=observed.st_size,
                    mtime_ns=observed.st_mtime_ns,
                    source_file_id=str(observed.st_ino),
                )
            )
        asset_id = str(registered[0]["id"])
        self.assertEqual(str(registered[1]["id"]), asset_id)
        self.assertNotEqual(
            str(registered[0]["asset_source_id"]),
            str(registered[1]["asset_source_id"]),
        )

        probe = ffprobe(copied)
        self.repository.update_asset_probe(asset_id, probe)
        duration_ms = int(probe["duration_ms"])
        run_id = f"arun_{rendered_sha256[:32]}"
        span_id = f"seg_{asset_id.removeprefix('asset_')}_1_0"
        second_span_id = f"seg_{asset_id.removeprefix('asset_')}_1_1"
        now = utc_now_iso()
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,
                     analysis_profile_json,input_asset_sha256,status,
                     transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','canonical-export-derivative',
                          '{}',?,'succeeded','available','ready',?)""",
                (run_id, asset_id, rendered_sha256, now),
            )
            connection.executemany(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                     boundary_reason,semantic_json,combined_text,visual_status,
                     transcript_status,created_at)
                   VALUES(?,?,?,?,?,?,'canonical-export-derivative',
                          '{}','canonical derivative render','ready',
                          'available',?)""",
                (
                    (span_id, asset_id, run_id, 0, 0, duration_ms, now),
                    (
                        second_span_id,
                        asset_id,
                        run_id,
                        1,
                        0,
                        duration_ms,
                        now,
                    ),
                ),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                     asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, run_id, now),
            )

        candidates, _heads = self.repository.mixed_candidates()
        self.assertIn(span_id, {str(item["id"]) for item in candidates})
        response = MixedRetrievalService(self.repository).search(
            {
                "query": "canonical derivative render",
                "types": ["video"],
                "top_k": 100,
            }
        )
        returned_ids = {
            str(item["id"])
            for item in [*response["results"], *response["audit_results"]]
        }
        self.assertNotIn(span_id, returned_ids)
        facts = self.repository.canonical_material_search_facts([asset_id])
        self.assertIn(
            rendered_sha256,
            {str(item["video_sha256"]) for item in facts["derivative_facts"]},
        )
        self.assertRegex(response["derivative_revision"], r"^[0-9a-f]{64}$")

        derivative_ref = f"memolens://evidence/span/{span_id}"
        second_derivative_ref = f"memolens://evidence/span/{second_span_id}"
        rejected_project = self.repository.create_project(
            "Derivative executable admission",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "canonical-export-service-test"},
        )
        rejected_project_id = str(rejected_project["id"])
        rejected_brief = self.repository.get_brief(rejected_project_id, 1)
        assert rejected_brief is not None
        derivative_semantic = semantic(
            "derivative executable admission",
            evidence_ref=derivative_ref,
        )
        derivative_semantic["material_hints"].append(
            {
                "hint_id": "derivative_ending",
                "evidence_ref": second_derivative_ref,
                "script_block_ids": ["ending"],
                "reason": "Exercise the same exact bytes on the ending Beat.",
            }
        )
        with self.assertRaises(BlueprintServiceError) as raised:
            self.blueprints.commit_proposal(
                rejected_project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": rejected_brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": derivative_semantic,
                },
                idempotency_key="derivative-executable-rejected",
            )
        self.assertEqual(
            raised.exception.code,
            "canonical_derivative_executable_material_forbidden",
        )
        with self.repository.transaction() as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (rejected_project_id,),
                    ).fetchone()[0]
                    for table in (
                        "creative_blueprint_operations",
                        "creative_blueprint_revisions",
                        "blueprint_command_receipts",
                    )
                ),
                (0, 0, 0),
            )

        reference_project = self.repository.create_project(
            "Derivative reference-only admission",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "canonical-export-service-test"},
        )
        reference_project_id = str(reference_project["id"])
        reference_brief = self.repository.get_brief(reference_project_id, 1)
        assert reference_brief is not None
        reference_semantic = semantic(
            "derivative reference-only admission",
            evidence_ref=derivative_ref,
        )
        reference_semantic["material_hints"] = []
        reference_semantic["reference_refs"].append(
            {
                "reference_id": "canonical_derivative_style_reference",
                "kind": "evidence",
                "locator": derivative_ref,
                "note": "Reference-only history; never executable material.",
            }
        )
        self.blueprints.commit_proposal(
            reference_project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": reference_brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": reference_semantic,
            },
            idempotency_key="derivative-reference-only-allowed",
        )
        reference_head = self.repository.get_blueprint_head(reference_project_id)
        assert reference_head is not None
        reference_manifest = reference_head["blueprint"]["evidence_manifest"]
        self.assertEqual(
            next(
                item["status"]
                for item in reference_manifest
                if item["evidence_ref"] == derivative_ref
            ),
            "verified",
        )
        current_reference = self.blueprints.get(reference_project_id).response
        self.assertEqual(
            current_reference["blueprint"]["evidence_manifest"],
            reference_manifest,
        )

        current_candidates, _heads = self.repository.mixed_candidates()
        current_asset_ids = sorted(
            {str(item["asset_id"]) for item in current_candidates}
        )
        pre_derivative_facts = dict(
            self.repository.canonical_material_search_facts(current_asset_ids)
        )
        pre_derivative_facts["derivative_facts"] = []
        late_project = self.repository.create_project(
            "Derivative late admission",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "canonical-export-service-test"},
        )
        late_project_id = str(late_project["id"])
        late_brief = self.repository.get_brief(late_project_id, 1)
        assert late_brief is not None
        with patch.object(
            self.repository,
            "canonical_material_search_facts_in_transaction",
            return_value=pre_derivative_facts,
        ):
            self.blueprints.commit_proposal(
                late_project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": late_brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": derivative_semantic,
                },
                idempotency_key="derivative-late-blueprint",
            )
        late_blueprint = self.repository.get_blueprint_head(late_project_id)
        assert late_blueprint is not None
        late_blueprint_expected = {
            field: late_blueprint[field]
            for field in ("revision", "content_sha256", "semantic_sha256")
        }
        exact_late_blueprint = self.blueprints.get(
            late_project_id,
            revision=int(late_blueprint["revision"]),
        ).response
        self.assertEqual(
            exact_late_blueprint["content_sha256"],
            late_blueprint["content_sha256"],
        )
        late_history = self.blueprints.history(late_project_id, limit=50).response
        self.assertEqual(len(late_history["operations"]), 1)
        self._assert_current_blueprint_rejected_read_only(
            late_project_id,
            code="canonical_derivative_executable_material_forbidden",
        )
        self._assert_current_workspace_rejected_read_only(
            late_project_id,
            code="canonical_derivative_executable_material_forbidden",
        )
        with self.repository.transaction() as connection:
            blueprint_ledger_before = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (late_project_id,),
                ).fetchone()[0]
                for table in (
                    "creative_blueprint_operations",
                    "creative_blueprint_revisions",
                    "blueprint_command_receipts",
                )
            )
        with self.assertRaises(BlueprintServiceError) as no_change_raised:
            self.blueprints.commit_proposal(
                late_project_id,
                {
                    "expected_head": {
                        "revision": late_blueprint["revision"],
                        "content_sha256": late_blueprint["content_sha256"],
                    },
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": derivative_semantic,
                },
                idempotency_key="derivative-late-no-change-rejected",
            )
        self.assertEqual(
            no_change_raised.exception.code,
            "canonical_derivative_executable_material_forbidden",
        )
        self.assertEqual(no_change_raised.exception.status, 409)
        with self.repository.transaction() as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (late_project_id,),
                    ).fetchone()[0]
                    for table in (
                        "creative_blueprint_operations",
                        "creative_blueprint_revisions",
                        "blueprint_command_receipts",
                    )
                ),
                blueprint_ledger_before,
            )
        with self.assertRaises(CoverageServiceError) as coverage_raised:
            self.coverage.materialize_baseline(
                late_project_id,
                {
                    "expected_blueprint": late_blueprint_expected,
                    "expected_plan_head": None,
                },
                idempotency_key="derivative-late-coverage",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(
            coverage_raised.exception.code,
            "canonical_derivative_executable_material_forbidden",
        )
        with self.repository.transaction() as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (late_project_id,),
                    ).fetchone()[0]
                    for table in (
                        "coverage_plan_operations",
                        "coverage_plan_revisions",
                        "coverage_plan_receipts",
                    )
                ),
                (0, 0, 0),
            )

        with patch.object(
            self.repository,
            "canonical_material_search_facts_in_transaction",
            return_value=pre_derivative_facts,
        ):
            self.coverage.materialize_baseline(
                late_project_id,
                {
                    "expected_blueprint": late_blueprint_expected,
                    "expected_plan_head": None,
                },
                idempotency_key="derivative-late-coverage",
                expected_database_uuid=self.repository.database_uuid,
            )
        late_coverage = self.repository.get_coverage_head(late_project_id)
        assert late_coverage is not None
        with self.assertRaises(TimelineLoweringServiceError) as timeline_raised:
            self.timelines.materialize_first_cut(
                late_project_id,
                {
                    "expected_blueprint": {
                        **late_blueprint_expected,
                        "operation_id": late_blueprint["operation_id"],
                    },
                    "expected_coverage": {
                        field: late_coverage[field]
                        for field in (
                            "revision",
                            "content_sha256",
                            "evidence_manifest_sha256",
                            "operation_id",
                        )
                    },
                    "expected_timeline_head": None,
                },
                idempotency_key="derivative-late-timeline",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(timeline_raised.exception.code, "timeline_source_unavailable")
        with self.repository.transaction() as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (late_project_id,),
                    ).fetchone()[0]
                    for table in (
                        "canonical_timeline_operations",
                        "canonical_timeline_revisions",
                        "canonical_timeline_receipts",
                    )
                ),
                (0, 0, 0),
            )

    def test_real_partial_usage_residual_reexports_and_advances_next_search(
        self,
    ) -> None:
        usage_project, timeline_payload, _source_video, _source_sha256 = (
            self._build_mixed_media_project()
        )
        self._materialize(
            usage_project,
            timeline_payload,
            key="real-residual-initial-timeline",
        )
        initial_submission = self._submit_export(
            usage_project,
            package_basename="real-residual-initial",
            idempotency_key="real-residual-initial-export",
        )
        initial_job = self._wait_terminal(str(initial_submission["id"]))
        self.assertEqual(initial_job["status"], "succeeded", initial_job.get("error"))
        initial_revision = self.repository.get_canonical_export_revision(
            project_id=usage_project,
            revision=int(initial_job["result"]["revision"]),
        )
        assert initial_revision is not None

        retrieval = MixedRetrievalService(self.repository)
        searched = retrieval.search(
            {
                "query": "ending real",
                "types": ["video"],
                "top_k": 100,
                "filters": {"unused_only": True},
            }
        )
        selected = searched["results"][0]
        self.assertTrue(str(selected["id"]).startswith("rseg_"))
        residual_binding = selected["residual_binding"]
        selection_candidate = {
            field: selected[field]
            for field in (
                "id",
                "asset_id",
                "asset_source_id",
                "result_type",
                "analysis_run_id",
                "analysis_revision",
                "start_ms",
                "end_ms",
                "usage",
                "residual_binding",
            )
        }
        director = CreativeDirector(self.repository, retrieval)
        brief_request = {
            "goal": "Use the exact remaining ending real span",
            "duration_ms": 8_000,
            "candidate_refs": [selected["id"]],
            "usage_selection": {
                "policy": "unused_only",
                "usage_revision": searched["usage_revision"],
                "derivative_revision": searched["derivative_revision"],
                "candidates": [selection_candidate],
            },
        }
        project, _search = director.create_brief(brief_request)
        legacy_project, _legacy_search = director.create_brief(
            {
                **brief_request,
                "goal": "Keep a legacy-only stale residual selection",
            }
        )
        project_id = str(project["id"])
        legacy_project_id = str(legacy_project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        proposed = semantic(
            "real-residual-export",
            evidence_ref=f"memolens://evidence/span/{selected['id']}",
        )
        proposed["material_hints"][0]["proof"] = {
            "kind": "residual_span",
            "residual_binding": residual_binding,
        }
        ending_asset_id = next(
            str(item["asset_id"])
            for item in initial_revision["usage_occurrences"]
            if item["media_kind"] == "image"
        )
        proposed["material_hints"].append(
            {
                "hint_id": "real_residual_ending",
                "evidence_ref": f"memolens://evidence/asset/{ending_asset_id}",
                "script_block_ids": ["ending"],
                "reason": "Close on the exact verified image.",
            }
        )
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
            idempotency_key="real-residual-blueprint",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            field: blueprint[field]
            for field in (
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
                    field: blueprint_binding[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="real-residual-coverage",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        self.timelines.materialize_first_cut(
            project_id,
            {
                "expected_blueprint": blueprint_binding,
                "expected_coverage": {
                    field: coverage[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    )
                },
                "expected_timeline_head": None,
            },
            idempotency_key="real-residual-timeline",
            expected_database_uuid=self.repository.database_uuid,
        )
        workspace = self.timelines.read(project_id)
        self.assertEqual(workspace["freshness"], {"state": "current", "reasons": []})
        residual_clip = workspace["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(
            residual_clip["span_id"],
            residual_binding["parent_segment_id"],
        )
        self.assertEqual(residual_clip["residual_id"], selected["id"])
        self.assertEqual(residual_clip["residual_binding"], residual_binding)
        self.assertGreaterEqual(
            residual_clip["source_in_ms"],
            residual_binding["source_in_ms"],
        )
        self.assertLessEqual(
            residual_clip["source_out_ms"],
            residual_binding["source_out_ms"],
        )

        residual_submission = self._submit_export(
            project_id,
            package_basename="real-residual-reexport",
            idempotency_key="real-residual-reexport",
            native_gesture_nonce="native-export-gesture-0002",
        )
        residual_job = self._wait_terminal(str(residual_submission["id"]))
        self.assertEqual(residual_job["status"], "succeeded", residual_job.get("error"))
        residual_revision = self.repository.get_canonical_export_revision(
            project_id=project_id,
            revision=int(residual_job["result"]["revision"]),
        )
        assert residual_revision is not None
        residual_usage = next(
            item
            for item in residual_revision["usage_occurrences"]
            if item["asset_id"] == selected["asset_id"]
        )
        self.assertEqual(
            (
                residual_usage["source_start_ms"],
                residual_usage["source_end_ms"],
            ),
            (
                residual_clip["source_in_ms"],
                residual_clip["source_out_ms"],
            ),
        )

        next_search = retrieval.search(
            {
                "query": "ending real",
                "types": ["video"],
                "top_k": 100,
                "filters": {"unused_only": True},
            }
        )
        self.assertNotEqual(next_search["usage_revision"], searched["usage_revision"])
        self.assertNotEqual(
            next_search["derivative_revision"],
            searched["derivative_revision"],
        )
        self.assertNotIn(
            selected["id"],
            {
                str(item["id"])
                for item in [*next_search["results"], *next_search["audit_results"]]
            },
        )
        exact_residual_blueprint = self.blueprints.get(
            project_id,
            revision=int(blueprint["revision"]),
        ).response
        self.assertEqual(exact_residual_blueprint["content_sha256"], blueprint["content_sha256"])
        self._assert_current_blueprint_rejected_read_only(
            project_id,
            code="blueprint_residual_evidence_stale",
        )
        with self.repository.transaction() as connection:
            residual_blueprint_ledger_before = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                for table in (
                    "creative_blueprint_operations",
                    "creative_blueprint_revisions",
                    "blueprint_command_receipts",
                )
            )
        with self.assertRaises(BlueprintServiceError) as residual_no_change_raised:
            self.blueprints.commit_proposal(
                project_id,
                {
                    "expected_head": {
                        "revision": blueprint["revision"],
                        "content_sha256": blueprint["content_sha256"],
                    },
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": proposed,
                },
                idempotency_key="real-residual-no-change-rejected",
            )
        self.assertEqual(
            residual_no_change_raised.exception.code,
            "blueprint_residual_evidence_stale",
        )
        self.assertEqual(residual_no_change_raised.exception.status, 409)
        with self.repository.transaction() as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "creative_blueprint_operations",
                        "creative_blueprint_revisions",
                        "blueprint_command_receipts",
                    )
                ),
                residual_blueprint_ledger_before,
            )
        with self.assertRaises(ResidualParentResolutionError) as legacy_raised:
            TimelineService(self.repository).create_from_project(legacy_project_id)
        self.assertEqual(legacy_raised.exception.code, "residual_usage_stale")
        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM video_segments WHERE id LIKE 'rseg_%'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM timelines WHERE project_id=?",
                    (legacy_project_id,),
                ).fetchone()[0],
                0,
            )

    def test_no_overwrite_failure_writes_no_export_revision_or_usage(self) -> None:
        occupied = self.output / "occupied"
        occupied.mkdir()
        (occupied / "user.txt").write_text("preserve", encoding="utf-8")
        _project_id, submitted = self._start_export(package_basename="occupied")
        job = self._wait_terminal(str(submitted["id"]))
        self.assertEqual(job["status"], "failed")
        self.assertEqual((occupied / "user.txt").read_text(), "preserve")
        with closing(sqlite3.connect(self.db_path)) as connection:
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

    def test_terminal_write_failure_marks_runner_unhealthy_instead_of_silent(self) -> None:
        occupied = self.output / "terminal-write-failure"
        occupied.mkdir()
        (occupied / "user.txt").write_text("preserve", encoding="utf-8")
        original_update = self.repository.update_canonical_export_job

        def fail_terminal_update(**kwargs):
            if kwargs.get("status") in {"failed", "cancelled", "interrupted"}:
                raise sqlite3.OperationalError("injected terminal transition failure")
            return original_update(**kwargs)

        with patch.object(
            self.repository,
            "update_canonical_export_job",
            side_effect=fail_terminal_update,
        ):
            project_id, submitted = self._start_export(
                package_basename="terminal-write-failure"
            )
            job_id = str(submitted["id"])
            self._wait_runner_release(job_id)

        stuck = self.repository.get_canonical_export_job(job_id)
        assert stuck is not None
        self.assertEqual(stuck["status"], "commit_pending")
        self.assertIsNone(stuck["error"])
        self.assertEqual((occupied / "user.txt").read_text(), "preserve")
        with self.assertRaises(CanonicalExportIntegrityError):
            self.runner.assert_healthy()
        with self.assertRaises(CanonicalExportIntegrityError):
            self.service.presentation(project_id)

        preserved = self.output / "terminal-write-failure-user-preserved"
        occupied.rename(preserved)
        self.runner.reconcile_interrupted_storage()
        self.runner.assert_healthy()
        recovered = self.repository.get_canonical_export_job(job_id)
        assert recovered is not None
        self.assertEqual(recovered["status"], "failed")
        self.assertEqual((preserved / "user.txt").read_text(), "preserve")

    def test_post_publish_core_rejection_quarantines_bytes_and_writes_no_usage(self) -> None:
        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=CanonicalExportIntegrityError("injected_completion_rejection"),
        ):
            _project_id, submitted = self._start_export(
                package_basename="quarantined-film"
            )
            job = self._wait_terminal(str(submitted["id"]))

        self.assertEqual(job["status"], "failed")
        self.assertFalse((self.output / "quarantined-film").exists())
        quarantines = list(self.output.glob(".memolens-quarantine-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(
            set(path.name for path in quarantines[0].iterdir()),
            {
                "video.mp4",
                "script.txt",
                "manifest.json",
                "使用清单.txt",
                ".memolens-complete.json",
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
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

    def test_core_commit_then_wrapper_failure_preserves_successful_package(self) -> None:
        original_complete = self.repository.complete_canonical_export_job

        def commit_then_raise(**kwargs):
            original_complete(**kwargs)
            raise sqlite3.OperationalError("injected post-commit wrapper failure")

        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=commit_then_raise,
        ):
            project_id, submitted = self._start_export(
                package_basename="committed-before-wrapper-failure"
            )
            job_id = str(submitted["id"])
            self._wait_terminal(job_id)
            self._wait_runner_release(job_id)

        committed = self.repository.get_canonical_export_job(job_id)
        assert committed is not None
        self.assertEqual(committed["status"], "succeeded")
        self.assertTrue(
            (self.output / "committed-before-wrapper-failure").is_dir()
        )
        self.assertEqual(list(self.output.glob(".memolens-quarantine-*")), [])
        self.assertEqual(
            len(
                self.repository.list_canonical_usage_occurrences(
                    project_id=project_id,
                    export_revision=int(committed["result"]["revision"]),
                )
            ),
            2,
        )
        self.runner.assert_healthy()

    def test_reconcile_clears_unknown_outcome_after_durable_success(self) -> None:
        original_complete = self.repository.complete_canonical_export_job
        original_get = self.repository.get_canonical_export_job
        committed = threading.Event()
        failed_read = threading.Event()

        def commit_then_raise(**kwargs):
            original_complete(**kwargs)
            committed.set()
            raise sqlite3.OperationalError("injected lost commit response")

        def fail_first_post_commit_read(job_id):
            if committed.is_set() and not failed_read.is_set():
                failed_read.set()
                raise sqlite3.OperationalError("injected transient trusted read failure")
            return original_get(job_id)

        with (
            patch.object(
                self.repository,
                "complete_canonical_export_job",
                side_effect=commit_then_raise,
            ),
            patch.object(
                self.repository,
                "get_canonical_export_job",
                side_effect=fail_first_post_commit_read,
            ),
        ):
            _project_id, submitted = self._start_export(
                package_basename="durable-success-unknown-return"
            )
            job_id = str(submitted["id"])
            self._wait_runner_release(job_id)

        durable = self.repository.get_canonical_export_job(job_id)
        assert durable is not None
        self.assertEqual(durable["status"], "succeeded")
        self.assertTrue(failed_read.is_set())
        self.assertTrue(
            (self.output / "durable-success-unknown-return").is_dir()
        )
        with self.assertRaises(CanonicalExportIntegrityError):
            self.runner.assert_healthy()

        self.runner.reconcile_interrupted_storage()
        self.runner.assert_healthy()

    def test_unknown_post_publish_db_failure_stays_recoverable_without_quarantine(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=sqlite3.OperationalError("injected unknown commit outcome"),
        ):
            project_id, submitted = self._start_export(
                package_basename="unknown-commit-outcome"
            )
            job_id = str(submitted["id"])
            interrupted = self._wait_status(job_id, "interrupted")
            self._wait_runner_release(job_id)

        self.assertEqual(
            interrupted["error"]["code"],
            "canonical_export_published_outcome_unknown",
        )
        self.assertTrue((self.output / "unknown-commit-outcome").is_dir())
        self.assertEqual(list(self.output.glob(".memolens-quarantine-*")), [])
        with self.assertRaises(CanonicalExportIntegrityError):
            self.runner.assert_healthy()

        self.runner.reconcile_interrupted_storage()
        self.runner.assert_healthy()
        recovered = self.repository.get_canonical_export_job(job_id)
        assert recovered is not None
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(
            len(
                self.repository.list_canonical_usage_occurrences(
                    project_id=project_id,
                    export_revision=int(recovered["result"]["revision"]),
                )
            ),
            2,
        )

    def test_complete_package_is_reconciled_after_crash_before_core_commit(self) -> None:
        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=_SimulatedProcessCrash("crash_after_publish"),
        ):
            project_id, submitted = self._start_export(
                package_basename="recovered-film"
            )
            job_id = str(submitted["id"])
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                interrupted = self.repository.get_canonical_export_job(job_id)
                assert interrupted is not None
                if (
                    interrupted["status"] == "commit_pending"
                    and (self.output / "recovered-film").is_dir()
                ):
                    break
                time.sleep(0.05)
            else:
                self.fail("simulated crash did not leave a complete commit_pending package")
            self._wait_runner_release(job_id)

        attestation = self.repository.get_canonical_export_commit_attestation(job_id)
        self.assertIsNotNone(attestation)
        self.assertEqual(len(self.repository.mark_canonical_export_jobs_interrupted()), 1)
        self.runner.reconcile_interrupted_storage()
        recovered = self.repository.get_canonical_export_job(job_id)
        assert recovered is not None
        self.assertEqual(recovered["status"], "succeeded", recovered.get("error"))
        self.assertTrue((self.output / "recovered-film").is_dir())
        revision = self.repository.get_canonical_export_revision(
            project_id=project_id,
            revision=int(recovered["result"]["revision"]),
        )
        assert revision is not None
        self.assertEqual(revision["job_id"], job_id)
        self.assertEqual(len(revision["usage_occurrences"]), 2)
        self.assertEqual(list(self.output.glob(".memolens-quarantine-*")), [])

    def test_reconcile_commit_then_wrapper_failure_preserves_successful_package(
        self,
    ) -> None:
        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=_SimulatedProcessCrash("crash_after_publish"),
        ):
            project_id, submitted = self._start_export(
                package_basename="reconcile-committed-film"
            )
            job_id = str(submitted["id"])
            deadline = time.monotonic() + 60
            package = self.output / "reconcile-committed-film"
            while time.monotonic() < deadline:
                interrupted = self.repository.get_canonical_export_job(job_id)
                assert interrupted is not None
                if (
                    interrupted["status"] == "commit_pending"
                    and (package / ".memolens-complete.json").is_file()
                ):
                    break
                time.sleep(0.05)
            else:
                self.fail("simulated crash did not leave a complete package")
            self._wait_runner_release(job_id)

        self.repository.mark_canonical_export_jobs_interrupted()
        original_reconcile = (
            self.repository.reconcile_canonical_export_commit_pending
        )

        def reconcile_then_raise(**kwargs):
            original_reconcile(**kwargs)
            raise sqlite3.OperationalError("injected lost reconcile response")

        with patch.object(
            self.repository,
            "reconcile_canonical_export_commit_pending",
            side_effect=reconcile_then_raise,
        ):
            self.runner.reconcile_interrupted_storage()

        recovered = self.repository.get_canonical_export_job(job_id)
        assert recovered is not None
        self.assertEqual(recovered["status"], "succeeded")
        self.assertTrue(package.is_dir())
        self.assertEqual(list(self.output.glob(".memolens-quarantine-*")), [])
        self.assertEqual(
            len(
                self.repository.list_canonical_usage_occurrences(
                    project_id=project_id,
                    export_revision=int(recovered["result"]["revision"]),
                )
            ),
            2,
        )
        self.runner.assert_healthy()

    def test_executor_submission_failure_does_not_leave_requested_job_stuck(self) -> None:
        with patch.object(
            self.runner._executor,
            "submit",
            side_effect=RuntimeError("executor unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "executor unavailable"):
                self._start_export(package_basename="submit-failure")

        jobs = self.repository.list_canonical_export_jobs(limit=10)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "interrupted")
        self.assertEqual(
            jobs[0]["error"]["code"],
            "canonical_export_submission_failed",
        )
        self.assertEqual(self.runner._submitted, set())

    def test_pre_execute_validation_failure_discards_native_root_authority(self) -> None:
        with patch.object(
            self.repository,
            "build_canonical_export_envelope",
            side_effect=ValueError("injected pre-execute rejection"),
        ):
            with self.assertRaises(CanonicalExportServiceError):
                self._start_export(package_basename="pre-execute-rejection")

        self.assertEqual(self.repository._prepared_export_roots, {})
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM output_roots").fetchone()[0],
                0,
            )

    def test_native_selection_inode_swap_is_rejected_before_job_creation(self) -> None:
        project_id, _blueprint, _coverage, timeline_payload = self._build_project()
        self._materialize(
            project_id,
            timeline_payload,
            key="timeline-selection-swap",
        )
        presentation = self.service.presentation(project_id)
        selected = self.output.stat()
        intended = self.root / "exports-selected-original"
        self.output.rename(intended)
        self.output.mkdir()

        with self.assertRaises(CanonicalExportServiceError):
            self.service.start(
                project_id,
                {
                    "presentation": presentation["presentation"],
                    "presentation_sha256": presentation["presentation_sha256"],
                    "native_gesture_nonce": "native-export-selection-swap",
                    "destination": {
                        "canonical_path": str(self.output),
                        "package_basename": "selection-swap",
                        "selection_identity": {
                            "device": str(selected.st_dev),
                            "inode": str(selected.st_ino),
                        },
                    },
                },
                runtime_epoch="runtime-export-service",
                idempotency_key="canonical-export-selection-swap",
            )

        self.assertEqual(self.repository._prepared_export_roots, {})
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(list(intended.iterdir()), [])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM output_roots").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_export_jobs"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM canonical_export_jobs"
                ).fetchone()[0],
                0,
            )

    def test_first_worker_read_fault_becomes_recoverable_not_silent_requested(self) -> None:
        original_get = self.repository.get_canonical_export_job
        fault_observed = threading.Event()

        def flaky_get(job_id: str):
            if (
                threading.current_thread().name.startswith(
                    "memolens-canonical-export"
                )
                and not fault_observed.is_set()
            ):
                fault_observed.set()
                raise sqlite3.OperationalError("injected first read fault")
            return original_get(job_id)

        with patch.object(
            self.repository,
            "get_canonical_export_job",
            side_effect=flaky_get,
        ):
            _project_id, submitted = self._start_export(
                package_basename="worker-read-fault"
            )
            self.assertTrue(fault_observed.wait(timeout=10))
            job_id = str(submitted["id"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                job = original_get(job_id)
                assert job is not None
                if job["status"] == "interrupted":
                    break
                time.sleep(0.01)
            else:
                self.fail("worker read fault left the job active")

        self.assertEqual(job["error"]["code"], "canonical_export_worker_interrupted")

    def test_quarantine_failure_keeps_commit_intent_recoverable(self) -> None:
        with (
            patch.object(
                self.repository,
                "complete_canonical_export_job",
                side_effect=CanonicalExportIntegrityError(
                    "injected_completion_rejection"
                ),
            ),
            patch(
                "backend.src.media.canonical_export.quarantine_published_package",
                side_effect=OSError("quarantine unavailable"),
            ),
        ):
            project_id, submitted = self._start_export(
                package_basename="recoverable-quarantine"
            )
            job_id = str(submitted["id"])
            interrupted = self._wait_status(job_id, "interrupted")

        self.assertEqual(
            interrupted["error"]["code"],
            "canonical_export_quarantine_pending",
        )
        self.assertTrue((self.output / "recoverable-quarantine").is_dir())
        self.assertIsNotNone(
            self.repository.get_canonical_export_commit_attestation(job_id)
        )
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=1,
            ),
            [],
        )

        self.runner.reconcile_interrupted_storage()
        recovered = self.repository.get_canonical_export_job(job_id)
        assert recovered is not None
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(
            len(
                self.repository.list_canonical_usage_occurrences(
                    project_id=project_id,
                    export_revision=1,
                )
            ),
            2,
        )

    def test_crash_before_attestation_cannot_admit_any_package_or_usage(self) -> None:
        with patch.object(
            self.repository,
            "stage_canonical_export_commit",
            side_effect=_SimulatedProcessCrash("crash_before_attestation"),
        ):
            project_id, submitted = self._start_export(
                package_basename="pre-attestation-crash"
            )
            job_id = str(submitted["id"])
            self._wait_status(job_id, "packaging")
            self._wait_runner_release(job_id)

        self.assertIsNone(
            self.repository.get_canonical_export_commit_attestation(job_id)
        )
        self.assertFalse((self.output / "pre-attestation-crash").exists())
        self.assertEqual(len(self.repository.mark_canonical_export_jobs_interrupted()), 1)
        self.runner.reconcile_interrupted_storage()
        failed = self.repository.get_canonical_export_job(job_id)
        assert failed is not None
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=1,
            ),
            [],
        )

    def test_self_consistent_replacement_package_cannot_replace_db_attestation(self) -> None:
        with patch.object(
            self.repository,
            "complete_canonical_export_job",
            side_effect=_SimulatedProcessCrash("crash_before_core_commit"),
        ):
            project_id, submitted = self._start_export(
                package_basename="attested-film"
            )
            job_id = str(submitted["id"])
            deadline = time.monotonic() + 60
            package = self.output / "attested-film"
            while time.monotonic() < deadline:
                job = self.repository.get_canonical_export_job(job_id)
                assert job is not None
                if (
                    job["status"] == "commit_pending"
                    and (package / ".memolens-complete.json").is_file()
                ):
                    break
                time.sleep(0.05)
            else:
                self.fail("crash did not leave a complete attested package")
            self._wait_runner_release(job_id)

        video = package / "video.mp4"
        video.write_bytes(video.read_bytes() + b"replacement")
        manifest = json.loads((package / "manifest.json").read_bytes())
        manifest["video"]["sha256"] = hashlib.sha256(video.read_bytes()).hexdigest()
        manifest["video"]["size_bytes"] = video.stat().st_size
        manifest_bytes = canonical_json(manifest).encode("utf-8")
        (package / "manifest.json").write_bytes(manifest_bytes)
        usage_text = usage_text_from_manifest(manifest)
        (package / "使用清单.txt").write_text(usage_text, encoding="utf-8")
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        marker = {
            "object": "memolens.lightweight_export_package_completion",
            "schema_version": "1",
            "completion_state": "complete",
            "package_basename": "attested-film",
            "package_manifest_sha256": manifest_sha,
            "required_file_sha256": {
                "video.mp4": manifest["video"]["sha256"],
                "script.txt": manifest["script"]["sha256"],
                "manifest.json": manifest_sha,
                "使用清单.txt": hashlib.sha256(
                    usage_text.encode("utf-8")
                ).hexdigest(),
            },
        }
        (package / ".memolens-complete.json").write_bytes(
            canonical_json(marker).encode("utf-8")
        )
        self.assertEqual(
            inspect_existing_package(
                output_root=self.output,
                package_basename="attested-film",
            )["state"],
            "complete",
        )

        self.assertEqual(len(self.repository.mark_canonical_export_jobs_interrupted()), 1)
        with self.assertRaises(CanonicalExportIntegrityError):
            self.runner.reconcile_interrupted_storage()
        unresolved = self.repository.get_canonical_export_job(job_id)
        assert unresolved is not None
        self.assertEqual(unresolved["status"], "interrupted")
        self.assertTrue(package.is_dir())
        with self.assertRaises(CanonicalExportIntegrityError):
            self.runner.assert_healthy()
        self.assertEqual(
            self.repository.list_canonical_usage_occurrences(
                project_id=project_id,
                export_revision=1,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
