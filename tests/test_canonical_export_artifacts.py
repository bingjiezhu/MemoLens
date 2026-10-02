from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from backend.src.media import canonical_export_artifacts as canonical_artifacts
from backend.src.media.canonical_export_artifacts import (
    COMPLETION_MARKER_FILENAME,
    MANIFEST_FILENAME,
    SCRIPT_FILENAME,
    USAGE_FILENAME,
    VIDEO_FILENAME,
    CanonicalExportArtifactError,
    build_canonical_render_plan,
    build_lightweight_package,
    derive_usage_occurrences,
    inspect_existing_package,
    inspect_package_directory,
    project_script_text,
    quarantine_published_package,
    render_canonical_master,
    script_artifact_proof,
    usage_text_from_manifest,
    verify_existing_package,
)
from core.blueprint_contract import (
    blueprint_content_sha256,
    blueprint_semantic_sha256,
    canonical_sha256 as blueprint_canonical_sha256,
    compile_blueprint_document,
)
from core.coverage_contract import (
    canonical_sha256 as coverage_canonical_sha256,
    compile_baseline_coverage_plan,
    coverage_plan_content_sha256,
)
from backend.src.media.video import MediaCancelled, binary_capability
from core.residual_identity_contract import build_residual_binding
from core.timeline_lowering_contract import (
    canonical_json,
    canonical_sha256,
    canonical_timeline_content_sha256,
    compile_canonical_timeline,
    timeline_source_bindings_sha256,
)
from core.media_db import MediaRepository
from tests.test_timeline_lowering_contract import (
    ANALYSIS_RUN_ID,
    IMAGE_ID,
    IMAGE_REF,
    IMAGE_SHA,
    IMAGE_SOURCE_ID,
    SECOND_SPAN_ID,
    SECOND_SPAN_REF,
    SPAN_ID,
    SPAN_REF,
    VIDEO_ID,
    VIDEO_SHA,
    VIDEO_SOURCE_ID,
    blueprint_binding,
    compile_result,
    compile_proposed_blueprint,
    coverage_binding,
    evidence_rows,
    make_blueprint,
    make_plan,
    semantic,
    source_rows,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _residual_compile_result() -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
]:
    residual_binding = build_residual_binding(
        parent_segment_id=SPAN_ID,
        asset_id=VIDEO_ID,
        asset_sha256=VIDEO_SHA,
        asset_source_id=VIDEO_SOURCE_ID,
        source_binding_sha256="9" * 64,
        analysis_run_id=ANALYSIS_RUN_ID,
        analysis_revision=1,
        input_asset_sha256=VIDEO_SHA,
        parent_start_ms=1_000,
        parent_end_ms=7_000,
        source_in_ms=2_000,
        source_out_ms=7_000,
        usage_revision="8" * 64,
        parent_usage_projection_sha256="7" * 64,
    )
    residual_ref = f"memolens://evidence/span/{residual_binding['residual_id']}"
    proposed = semantic()
    proposed["material_hints"][1]["evidence_ref"] = residual_ref
    residual_proof = {
        "kind": "residual_span",
        "residual_binding": copy.deepcopy(residual_binding),
    }
    proposed["material_hints"][1]["proof"] = copy.deepcopy(residual_proof)
    evidence_manifest = []
    for evidence_ref in sorted(
        str(item["evidence_ref"]) for item in proposed["material_hints"]
    ):
        evidence_manifest.append(
            {
                "evidence_ref": evidence_ref,
                "status": "verified",
                "proof_sha256": (
                    blueprint_canonical_sha256(residual_proof)
                    if evidence_ref == residual_ref
                    else hashlib.sha256(evidence_ref.encode()).hexdigest()
                ),
            }
        )
    blueprint = compile_blueprint_document(
        project_id="project_timeline",
        revision=1,
        parent=None,
        created_by_operation_id="op_blueprint_one",
        semantic=proposed,
        source_candidate_sha256="a" * 64,
        initial_legacy_brief={"revision": 1, "content_sha256": "b" * 64},
        evidence_manifest=evidence_manifest,
    )
    current_blueprint = {
        "revision": 1,
        "content_sha256": blueprint_content_sha256(blueprint),
        "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
    }
    rows = evidence_rows(with_span=False)
    rows.append(
        {
            "evidence_ref": residual_ref,
            "proof": copy.deepcopy(residual_proof),
        }
    )
    coverage = compile_baseline_coverage_plan(
        project_id="project_timeline",
        revision=1,
        parent=None,
        blueprint=blueprint,
        blueprint_binding=current_blueprint,
        evidence_rows=rows,
    )
    resolved_sources = [
        {
            "evidence_ref": IMAGE_REF,
            "asset_id": IMAGE_ID,
            "asset_sha256": IMAGE_SHA,
            "asset_source_id": IMAGE_SOURCE_ID,
            **{
                field: source_rows()[0][field]
                for field in (
                    "analysis_run_id",
                    "analysis_revision",
                    "analysis_content_sha256",
                    "source_binding_sha256",
                )
            },
        },
        {
            "evidence_ref": residual_ref,
            "asset_id": VIDEO_ID,
            "asset_sha256": VIDEO_SHA,
            "asset_source_id": VIDEO_SOURCE_ID,
            "residual_binding": copy.deepcopy(residual_binding),
        },
    ]
    result = compile_canonical_timeline(
        project_id="project_timeline",
        revision=1,
        parent=None,
        blueprint=blueprint,
        blueprint_binding={
            **current_blueprint,
            "operation_id": "op_blueprint_one",
        },
        coverage_plan=coverage,
        coverage_binding={
            "revision": 1,
            "content_sha256": coverage_plan_content_sha256(coverage),
            "evidence_manifest_sha256": coverage_canonical_sha256(
                coverage["evidence_manifest"]
            ),
            "operation_id": "op_coverage_one",
        },
        source_bindings=resolved_sources,
    )
    return result, blueprint, coverage, residual_binding


def _fake_ffmpeg(path: Path) -> Path:
    executable = path / "fake-ffmpeg"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        "output=pathlib.Path(sys.argv[-1])\n"
        "payload=b'master-artifact' if output.name=='master.mp4' else "
        "('clip:'+output.name).encode()\n"
        "output.write_bytes(payload)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _source_record(path: Path, *, source_id: str, asset_id: str, digest: str, kind: str) -> dict[str, object]:
    metadata = path.stat()
    return {
        "id": source_id,
        "asset_id": asset_id,
        "sha256": digest,
        "kind": kind,
        "availability": "available",
        "root_path": str(path.parent.resolve()),
        "relative_path": path.name,
        "display_filename": path.name,
        "observed_size": metadata.st_size,
        "observed_mtime_ns": metadata.st_mtime_ns,
        "source_file_id": str(metadata.st_ino),
        "file_size": metadata.st_size,
        "rotation_degrees": 0,
        "codec": {"video_stream_index": 0, "audio_stream_index": None} if kind == "video" else {},
    }


def _canonical_master_probe(
    *,
    duration_ms: int,
    width: int,
    height: int,
) -> dict[str, object]:
    return {
        "duration_ms": duration_ms,
        "width": width,
        "height": height,
        "rotation_degrees": 0,
        "captured_at": None,
        "codec": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "avg_frame_rate": "30/1",
            "time_base": "1/15360",
            "video_stream_index": 0,
            "video_stream_indices": [0],
            "audio_stream_index": None,
            "audio_stream_indices": [],
            "stream_selection": "default_disposition_then_lowest_index",
            "audio_streams": [],
        },
    }


class _ArtifactFixture:
    def __init__(self, root: Path):
        self.root = root
        self.result, self.blueprint, self.coverage = compile_result()
        self.timeline = self.result["timeline"]
        self.bindings = self.result["source_bindings"]
        image = root / "opening-image.bin"
        video = root / "ending-video.mov"
        image.write_bytes(b"timeline-image")
        video.write_bytes(b"timeline-video")
        self.records = {
            self.bindings[0]["asset_source_id"]: _source_record(
                image,
                source_id=str(self.bindings[0]["asset_source_id"]),
                asset_id=str(self.bindings[0]["asset_id"]),
                digest=IMAGE_SHA,
                kind="image",
            ),
            self.bindings[1]["asset_source_id"]: _source_record(
                video,
                source_id=str(self.bindings[1]["asset_source_id"]),
                asset_id=str(self.bindings[1]["asset_id"]),
                digest=VIDEO_SHA,
                kind="video",
            ),
        }
        self.ffmpeg = _fake_ffmpeg(root)

    def resolver(self, source_id: str) -> dict[str, object] | None:
        return self.records.get(source_id)

    @staticmethod
    def image_freezer(_source: Path, output: Path) -> None:
        output.write_bytes(b"normalized-image")

    def render(
        self,
        *,
        workspace_name: str = "job",
        resolver=None,
        fault_inject=None,
        cancelled=lambda: False,
        probe_master=None,
        command_runner=None,
        use_default_probe: bool = False,
    ) -> tuple[Path, dict[str, object]]:
        workspace = self.root / workspace_name
        workspace.mkdir()
        master = workspace / "master.mp4"
        plan = build_canonical_render_plan(self.timeline, self.bindings).render_plan
        runner_override = {"command_runner": command_runner} if command_runner is not None else {}
        if probe_master is not None:
            probe_override = {"probe_master": probe_master}
        elif use_default_probe:
            probe_override = {}
        else:
            probe_override = {
                "probe_master": lambda _path: _canonical_master_probe(
                    duration_ms=plan.duration_ms,
                    width=plan.width,
                    height=plan.height,
                )
            }
        proof = render_canonical_master(
            self.timeline,
            self.bindings,
            source_resolver=resolver or self.resolver,
            job_workspace=workspace,
            master_output=master,
            ffmpeg_binary=str(self.ffmpeg),
            cancelled=cancelled,
            image_freezer=self.image_freezer,
            runtime_identity={"ffmpeg": "fake-v1", "executor": "test"},
            fault_inject=fault_inject,
            **runner_override,
            **probe_override,
        )
        return master, proof

    def export_context(self) -> dict[str, object]:
        return {
            "export_id": "export_operation_one",
            "job_id": "export_job_one",
            "project_id": self.timeline["project_id"],
            "timeline_binding": {
                "revision": self.timeline["revision"],
                "revision_sha256": "a" * 64,
                "timeline_id": self.timeline["timeline_id"],
                "timeline_content_sha256": canonical_timeline_content_sha256(self.timeline),
                "source_bindings_sha256": timeline_source_bindings_sha256(
                    self.bindings,
                    timeline=self.timeline,
                ),
                "blueprint_binding": copy.deepcopy(self.timeline["blueprint_binding"]),
                "coverage_binding": copy.deepcopy(self.timeline["coverage_binding"]),
            },
            "profile": "export-1080p",
        }

    def expected_manifest(
        self,
        render_proof: dict[str, object],
        *,
        script_text: str,
        basename: str,
        occurrences: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        context = self.export_context()
        usage = copy.deepcopy(occurrences or render_proof["usage_occurrences"])
        script_bytes = script_text.encode("utf-8")
        return {
            "object": "canonical_export.package_manifest",
            "schema_version": "1",
            "export_id": context["export_id"],
            "job_id": context["job_id"],
            "project_id": context["project_id"],
            "timeline_binding": context["timeline_binding"],
            "profile": "export-1080p",
            "package_basename": basename,
            "package_roles": [
                "final_video",
                "script",
                "package_manifest",
                "human_usage_list",
                "completion_marker",
            ],
            "required_files": [
                VIDEO_FILENAME,
                SCRIPT_FILENAME,
                MANIFEST_FILENAME,
                USAGE_FILENAME,
                COMPLETION_MARKER_FILENAME,
            ],
            "video": {
                "sha256": render_proof["video_sha256"],
                "size_bytes": render_proof["video_size_bytes"],
                "duration_ms": render_proof["video_duration_ms"],
            },
            "script": {"sha256": _sha256(script_bytes), "size_bytes": len(script_bytes)},
            "usage_occurrences": usage,
            "usage_sha256": _sha256(canonical_json(usage).encode()),
            "runtime_manifest": copy.deepcopy(render_proof["runtime_manifest"]),
            "runtime_manifest_sha256": render_proof["runtime_manifest_sha256"],
            "source_policy": {
                "library_originals_copied": False,
                "library_originals_moved": False,
                "library_originals_modified": False,
                "automatic_upload": False,
            },
            "optional_roles": {"subtitle": "absent", "cover": "absent"},
        }


class CanonicalRenderPlanTests(unittest.TestCase):
    def test_script_projection_has_one_frozen_utf8_format(self) -> None:
        projected = project_script_text(
            [
                {"block_id": "opening", "text": "  First\r\nline\n\n"},
                {"block_id": "ending", "text": "Last memory\r"},
            ]
        )
        self.assertEqual(projected, "  First\nline\n\nLast memory\n")
        self.assertEqual(
            script_artifact_proof(projected),
            {
                "sha256": _sha256(b"  First\nline\n\nLast memory\n"),
                "size_bytes": len(b"  First\nline\n\nLast memory\n"),
            },
        )

    def test_fixed_export_shapes_and_exact_occurrences(self) -> None:
        expected = {
            "16:9": (1920, 1080),
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
        }
        for aspect_ratio, dimensions in expected.items():
            with self.subTest(aspect_ratio=aspect_ratio):
                blueprint = make_blueprint(aspect_ratio=aspect_ratio)
                coverage = make_plan(blueprint)
                result, _, _ = compile_result(blueprint=blueprint, plan=coverage)
                bundle = build_canonical_render_plan(result["timeline"], result["source_bindings"])
                self.assertEqual((bundle.render_plan.width, bundle.render_plan.height), dimensions)
                self.assertEqual((bundle.render_plan.fps, bundle.render_plan.profile), (30, "export-1080p"))
                self.assertFalse(bundle.render_plan.has_transitions)
                self.assertTrue(all(clip["audio_enabled"] is False for clip in bundle.render_plan.clips))
                self.assertTrue(all(clip["fit"] == "cover" for clip in bundle.render_plan.clips))

        result, _, _ = compile_result()
        occurrences = derive_usage_occurrences(result["timeline"], result["source_bindings"])
        self.assertEqual([row["media_kind"] for row in occurrences], ["image", "video"])
        self.assertEqual((occurrences[0]["source_start_ms"], occurrences[0]["source_end_ms"]), (None, None))
        self.assertEqual(
            occurrences[1]["source_end_ms"] - occurrences[1]["source_start_ms"],
            occurrences[1]["timeline_end_ms"] - occurrences[1]["timeline_start_ms"],
        )

    def test_closed_timeline_rejects_unsupported_shape_instead_of_guessing(self) -> None:
        result, _, _ = compile_result()
        timeline = copy.deepcopy(result["timeline"])
        timeline["compiler"]["transitions"] = "fade"
        with self.assertRaises(CanonicalExportArtifactError) as raised:
            build_canonical_render_plan(timeline, result["source_bindings"])
        self.assertEqual(raised.exception.code, "canonical_timeline_contract_invalid")


class CanonicalSourceAndRenderTests(unittest.TestCase):
    def test_command_runner_reaps_child_when_cancel_callback_raises_after_launch(self) -> None:
        launched: list[subprocess.Popen[bytes]] = []
        real_popen = subprocess.Popen
        callback_calls = 0

        def capture_process(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            launched.append(process)
            return process

        def raising_cancel_callback() -> bool:
            nonlocal callback_calls
            callback_calls += 1
            if callback_calls == 1:
                return False
            raise RuntimeError("cancel callback failed after launch")

        with mock.patch.object(
            canonical_artifacts.subprocess,
            "Popen",
            side_effect=capture_process,
        ):
            with self.assertRaisesRegex(RuntimeError, "cancel callback failed after launch"):
                canonical_artifacts._default_command_runner(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    60.0,
                    raising_cancel_callback,
                )

        self.assertEqual((callback_calls, len(launched)), (2, 1))
        child = launched[0]
        self.assertIsNotNone(child.returncode)
        self.assertEqual(child.wait(timeout=0.1), child.returncode)
        if os.name == "posix":
            with self.assertRaises(ChildProcessError):
                os.waitpid(child.pid, os.WNOHANG)

    def test_image_video_render_uses_frozen_actual_read_manifest_without_path_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-canonical-render-") as directory:
            fixture = _ArtifactFixture(Path(directory).resolve())
            master, proof = fixture.render()
            self.assertEqual(master.read_bytes(), b"master-artifact")
            self.assertEqual(proof["video_sha256"], _sha256(b"master-artifact"))
            self.assertEqual(proof["video_size_bytes"], len(b"master-artifact"))
            manifest = proof["runtime_manifest"]["actual_read_manifest"]
            self.assertEqual(manifest["source_bindings"], fixture.bindings)
            image_binding = manifest["source_bindings"][0]
            self.assertEqual(
                set(image_binding),
                canonical_artifacts._SOURCE_BINDING_IMAGE_FIELDS,
            )
            self.assertEqual([row["asset_sha256"] for row in manifest["snapshots"]], [IMAGE_SHA, VIDEO_SHA])
            serialized = canonical_json(proof)
            self.assertNotIn(str(fixture.root), serialized)
            self.assertNotIn("root_path", serialized)
            self.assertNotIn("relative_path", serialized)
            self.assertEqual([row["source_label"] for row in manifest["snapshots"]], [
                "opening-image.bin",
                "ending-video.mov",
            ])

            forged_runtime = copy.deepcopy(proof["runtime_manifest"])
            forged_occurrences = copy.deepcopy(proof["usage_occurrences"])
            forged_binding = forged_runtime["actual_read_manifest"][
                "source_bindings"
            ][0]
            forged_binding["analysis_content_sha256"] = "not-a-sha256"
            forged_occurrences[0]["source_binding_sha256"] = _sha256(
                canonical_json(forged_binding).encode()
            )
            forged_bindings_sha256 = _sha256(
                canonical_json(
                    forged_runtime["actual_read_manifest"]["source_bindings"]
                ).encode()
            )
            forged_runtime[
                "timeline_source_bindings_sha256"
            ] = forged_bindings_sha256
            forged_runtime["actual_read_manifest"][
                "source_bindings_sha256"
            ] = forged_bindings_sha256
            with self.assertRaises(CanonicalExportArtifactError) as invalid_image:
                canonical_artifacts._validate_runtime_manifest(
                    forged_runtime,
                    occurrences=forged_occurrences,
                )
            self.assertEqual(
                invalid_image.exception.code,
                "invalid_actual_read_binding",
            )

    def test_residual_video_actual_read_is_closed_and_forgery_resistant(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-residual-actual-read-"
        ) as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            result, blueprint, coverage, residual_binding = (
                _residual_compile_result()
            )
            fixture.result = result
            fixture.blueprint = blueprint
            fixture.coverage = coverage
            fixture.timeline = result["timeline"]
            fixture.bindings = result["source_bindings"]

            master, proof = fixture.render()
            manifest = fixture.expected_manifest(
                proof,
                script_text="script\n",
                basename="residual-film",
            )
            package = build_lightweight_package(
                verified_master=master,
                render_proof=proof,
                expected_package_manifest=manifest,
                script_text="script\n",
                output_root=output_root,
                package_basename="residual-film",
            )
            actual_read = package["runtime_manifest"]["actual_read_manifest"]
            residual = actual_read["source_bindings"][1]
            occurrence = package["usage_occurrences"][1]
            residual_ref = (
                f"memolens://evidence/span/{residual_binding['residual_id']}"
            )
            self.assertEqual(
                set(residual),
                canonical_artifacts._SOURCE_BINDING_RESIDUAL_VIDEO_FIELDS,
            )
            self.assertEqual(residual["evidence_ref"], residual_ref)
            self.assertEqual(
                residual["span_id"],
                residual_binding["parent_segment_id"],
            )
            self.assertEqual(
                residual["parent_segment_id"],
                residual_binding["parent_segment_id"],
            )
            self.assertEqual(
                residual["residual_id"],
                residual_binding["residual_id"],
            )
            self.assertEqual(residual["residual_binding"], residual_binding)
            self.assertEqual(
                (residual["source_in_ms"], residual["source_out_ms"]),
                (occurrence["source_start_ms"], occurrence["source_end_ms"]),
            )

            def resign(
                runtime: dict[str, object],
                occurrences: list[dict[str, object]],
            ) -> None:
                actual = runtime["actual_read_manifest"]
                bindings = actual["source_bindings"]
                occurrences[1]["source_binding_sha256"] = canonical_sha256(
                    bindings[1]
                )
                bindings_sha256 = canonical_sha256(bindings)
                actual["source_bindings_sha256"] = bindings_sha256
                runtime["timeline_source_bindings_sha256"] = bindings_sha256
                runtime["actual_read_manifest_sha256"] = canonical_sha256(actual)

            cases = {
                "ordinary_ref_with_residual_shape": (
                    "invalid_actual_read_binding",
                    lambda binding, _occurrence: binding.update(
                        evidence_ref=SPAN_REF
                    ),
                ),
                "top_level_parent_substitution": (
                    "actual_read_occurrence_mismatch",
                    lambda binding, _occurrence: binding.update(
                        parent_segment_id=SECOND_SPAN_ID
                    ),
                ),
                "nested_residual_forgery": (
                    "invalid_actual_read_binding",
                    lambda binding, _occurrence: binding[
                        "residual_binding"
                    ].update(usage_revision="6" * 64),
                ),
                "read_outside_residual": (
                    "actual_read_occurrence_mismatch",
                    lambda binding, selected_occurrence: (
                        binding.update(
                            source_out_ms=int(
                                binding["residual_binding"]["source_out_ms"]
                            )
                            + 1
                        ),
                        selected_occurrence.update(
                            source_end_ms=int(
                                binding["residual_binding"]["source_out_ms"]
                            )
                            + 1
                        ),
                    ),
                ),
            }
            for label, (expected_code, forge) in cases.items():
                with self.subTest(label=label):
                    forged_runtime = copy.deepcopy(proof["runtime_manifest"])
                    forged_occurrences = copy.deepcopy(proof["usage_occurrences"])
                    forged_binding = forged_runtime["actual_read_manifest"][
                        "source_bindings"
                    ][1]
                    forge(forged_binding, forged_occurrences[1])
                    resign(forged_runtime, forged_occurrences)
                    with self.assertRaises(CanonicalExportArtifactError) as raised:
                        canonical_artifacts._validate_runtime_manifest(
                            forged_runtime,
                            occurrences=forged_occurrences,
                        )
                    self.assertEqual(raised.exception.code, expected_code)

    def test_split_occurrences_may_repeat_exact_evidence_but_not_forge_proof(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-split-export-") as directory:
            fixture = _ArtifactFixture(Path(directory).resolve())
            _master, proof = fixture.render()
            runtime = copy.deepcopy(proof["runtime_manifest"])
            occurrences = copy.deepcopy(proof["usage_occurrences"])
            actual = runtime["actual_read_manifest"]
            bindings = actual["source_bindings"]
            snapshots = actual["snapshots"]

            first_occurrence = occurrences[1]
            first_binding = bindings[1]
            split_duration = (
                int(first_occurrence["timeline_end_ms"])
                - int(first_occurrence["timeline_start_ms"])
            ) // 2
            self.assertGreaterEqual(split_duration, 100)
            original_timeline_end = int(first_occurrence["timeline_end_ms"])
            original_source_end = int(first_occurrence["source_end_ms"])
            split_timeline_ms = int(first_occurrence["timeline_start_ms"]) + split_duration
            split_source_ms = int(first_occurrence["source_start_ms"]) + split_duration

            first_occurrence["timeline_end_ms"] = split_timeline_ms
            first_occurrence["source_end_ms"] = split_source_ms
            first_binding["source_out_ms"] = split_source_ms
            first_occurrence["source_binding_sha256"] = canonical_sha256(first_binding)

            child_clip_id = "clip_" + "f" * 24
            second_occurrence = copy.deepcopy(first_occurrence)
            second_occurrence.update(
                ordinal=2,
                clip_id=child_clip_id,
                timeline_start_ms=split_timeline_ms,
                timeline_end_ms=original_timeline_end,
                source_start_ms=split_source_ms,
                source_end_ms=original_source_end,
            )
            second_binding = copy.deepcopy(first_binding)
            second_binding.update(
                clip_id=child_clip_id,
                source_in_ms=split_source_ms,
                source_out_ms=original_source_end,
            )
            second_occurrence["source_binding_sha256"] = canonical_sha256(second_binding)
            second_snapshot = copy.deepcopy(snapshots[1])
            second_snapshot.update(ordinal=2, clip_id=child_clip_id)
            occurrences.append(second_occurrence)
            bindings.append(second_binding)
            snapshots.append(second_snapshot)

            bindings_sha256 = canonical_sha256(bindings)
            actual["source_bindings_sha256"] = bindings_sha256
            runtime["timeline_source_bindings_sha256"] = bindings_sha256
            runtime["actual_read_manifest_sha256"] = canonical_sha256(actual)
            validated_occurrences = canonical_artifacts._validate_occurrences(occurrences)
            self.assertEqual(
                canonical_artifacts._validate_runtime_manifest(
                    runtime,
                    occurrences=validated_occurrences,
                ),
                runtime,
            )

            forged_runtime = copy.deepcopy(runtime)
            forged_occurrences = copy.deepcopy(validated_occurrences)
            forged_actual = forged_runtime["actual_read_manifest"]
            forged_binding = forged_actual["source_bindings"][2]
            forged_binding["coverage_proof_sha256"] = "e" * 64
            forged_occurrences[2]["source_binding_sha256"] = canonical_sha256(
                forged_binding
            )
            forged_bindings_sha256 = canonical_sha256(
                forged_actual["source_bindings"]
            )
            forged_actual["source_bindings_sha256"] = forged_bindings_sha256
            forged_runtime["timeline_source_bindings_sha256"] = forged_bindings_sha256
            forged_runtime["actual_read_manifest_sha256"] = canonical_sha256(
                forged_actual
            )
            with self.assertRaises(CanonicalExportArtifactError) as forged:
                canonical_artifacts._validate_runtime_manifest(
                    forged_runtime,
                    occurrences=forged_occurrences,
                )
            self.assertEqual(forged.exception.code, "invalid_actual_read_binding")

    def test_source_missing_hash_identity_and_symlink_fail_closed(self) -> None:
        cases: list[tuple[str, callable, str]] = []
        with tempfile.TemporaryDirectory(prefix="memolens-source-oracle-") as directory:
            root = Path(directory).resolve()
            fixture = _ArtifactFixture(root)
            cases.append(("missing", lambda _source_id: None, "source_unavailable"))

            changed_records = copy.deepcopy(fixture.records)
            first_id = str(fixture.bindings[0]["asset_source_id"])
            changed_path = root / "hash-mismatch-image.bin"
            changed_path.write_bytes(b"changed")
            metadata = changed_path.stat()
            changed_records[first_id].update(
                relative_path=changed_path.name,
                observed_size=metadata.st_size,
                observed_mtime_ns=metadata.st_mtime_ns,
                source_file_id=str(metadata.st_ino),
                file_size=metadata.st_size,
            )
            cases.append(("hash", changed_records.get, "source_hash_mismatch"))

            # Build one independently stale identity against the original bytes.
            stale_records = copy.deepcopy(fixture.records)
            original_path = root / str(stale_records[first_id]["relative_path"])
            stale_records[first_id]["observed_mtime_ns"] = int(original_path.stat().st_mtime_ns) + 1
            cases.append(("identity", stale_records.get, "source_identity_mismatch"))

            symlink_records = copy.deepcopy(fixture.records)
            target = root / "target-image.bin"
            target.write_bytes(b"timeline-image")
            link = root / "linked-image.bin"
            link.symlink_to(target.name)
            metadata = target.stat()
            symlink_records[first_id].update(
                relative_path=link.name,
                observed_size=metadata.st_size,
                observed_mtime_ns=metadata.st_mtime_ns,
                source_file_id=str(metadata.st_ino),
                file_size=metadata.st_size,
            )
            cases.append(("symlink", symlink_records.get, "source_locator_invalid"))

            for index, (name, resolver, expected_code) in enumerate(cases):
                with self.subTest(case=name), self.assertRaises(CanonicalExportArtifactError) as raised:
                    fixture.render(workspace_name=f"job-{index}", resolver=resolver)
                self.assertEqual(raised.exception.code, expected_code)

    def test_snapshot_is_stable_when_original_changes_after_copy(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-source-race-") as directory:
            root = Path(directory).resolve()
            fixture = _ArtifactFixture(root)
            first = fixture.records[str(fixture.bindings[0]["asset_source_id"])]

            def mutate(stage: str) -> None:
                if stage == "source_frozen:0":
                    (root / str(first["relative_path"])).write_bytes(b"mutated-after-snapshot")

            master, proof = fixture.render(fault_inject=mutate)
            self.assertEqual(master.read_bytes(), b"master-artifact")
            self.assertEqual(
                proof["runtime_manifest"]["actual_read_manifest"]["snapshots"][0]["asset_sha256"],
                IMAGE_SHA,
            )

    def test_cancellation_and_fault_leave_no_verified_master(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-render-fault-") as directory:
            root = Path(directory).resolve()
            fixture = _ArtifactFixture(root)
            calls = 0

            def cancelled() -> bool:
                nonlocal calls
                calls += 1
                return calls > 7

            with self.assertRaises(MediaCancelled):
                fixture.render(workspace_name="cancelled", cancelled=cancelled)

            def fail(stage: str) -> None:
                if stage == "before_master_assemble":
                    raise RuntimeError("injected")

            with self.assertRaisesRegex(RuntimeError, "injected"):
                fixture.render(workspace_name="faulted", fault_inject=fail)
            self.assertFalse((root / "faulted" / "master.mp4").exists())

    def test_master_probe_is_closed_and_rejects_any_audio_stream(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-master-probe-") as directory:
            root = Path(directory).resolve()
            fixture = _ArtifactFixture(root)
            plan = build_canonical_render_plan(fixture.timeline, fixture.bindings).render_plan
            with_audio = _canonical_master_probe(
                duration_ms=plan.duration_ms,
                width=plan.width,
                height=plan.height,
            )
            with_audio["codec"]["audio_stream_index"] = 1
            with_audio["codec"]["audio_stream_indices"] = [1]
            with_audio["codec"]["audio_streams"] = [
                {"index": 1, "codec": "aac", "sample_rate": "48000", "channels": 2}
            ]
            with self.assertRaises(CanonicalExportArtifactError) as audio:
                fixture.render(
                    workspace_name="audio-master",
                    probe_master=lambda _path: with_audio,
                )
            self.assertEqual(audio.exception.code, "master_audio_stream_forbidden")

            incomplete = _canonical_master_probe(
                duration_ms=plan.duration_ms,
                width=plan.width,
                height=plan.height,
            )
            del incomplete["codec"]["audio_streams"]
            with self.assertRaises(CanonicalExportArtifactError) as missing_audio_attestation:
                fixture.render(
                    workspace_name="unclosed-master",
                    probe_master=lambda _path: incomplete,
                )
            self.assertEqual(missing_audio_attestation.exception.code, "master_probe_invalid")

            two_video = _canonical_master_probe(
                duration_ms=plan.duration_ms,
                width=plan.width,
                height=plan.height,
            )
            two_video["codec"]["video_stream_indices"] = [0, 1]
            with self.assertRaises(CanonicalExportArtifactError) as multiple_video:
                fixture.render(
                    workspace_name="two-video-master",
                    probe_master=lambda _path: two_video,
                )
            self.assertEqual(multiple_video.exception.code, "master_video_stream_count_invalid")

    def test_probe_callback_cannot_replace_master_between_probe_and_hash(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-master-probe-replacement-") as directory:
            root = Path(directory).resolve()
            fixture = _ArtifactFixture(root)
            plan = build_canonical_render_plan(fixture.timeline, fixture.bindings).render_plan

            def replace_master(path: Path) -> dict[str, object]:
                path.rename(path.with_name("master-probed-original.mp4"))
                path.write_bytes(b"replacement-master-bytes")
                return _canonical_master_probe(
                    duration_ms=plan.duration_ms,
                    width=plan.width,
                    height=plan.height,
                )

            with self.assertRaises(CanonicalExportArtifactError) as replaced:
                fixture.render(
                    workspace_name="replaced-during-probe",
                    probe_master=replace_master,
                )
            self.assertEqual(replaced.exception.code, "master_artifact_changed")


class LightweightPackageTests(unittest.TestCase):
    def _build(self, fixture: _ArtifactFixture, root: Path, *, basename: str = "memory-film"):
        master, proof = fixture.render(workspace_name=f"render-{basename}")
        manifest = fixture.expected_manifest(
            proof,
            script_text="First memory\nLast memory\n",
            basename=basename,
        )
        package = build_lightweight_package(
            verified_master=master,
            render_proof=proof,
            expected_package_manifest=manifest,
            script_text="First memory\nLast memory\n",
            output_root=root,
            package_basename=basename,
        )
        return master, proof, manifest, package

    def test_package_rejects_self_consistent_extra_runtime_or_occurrence_fields(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-package-closed-shape-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            master, proof = fixture.render()
            manifest = fixture.expected_manifest(
                proof,
                script_text="script\n",
                basename="closed-film",
            )

            forged_runtime_proof = copy.deepcopy(proof)
            forged_runtime_proof["runtime_manifest"]["untrusted_extension"] = "forged"
            forged_runtime_proof["runtime_manifest_sha256"] = _sha256(
                canonical_json(forged_runtime_proof["runtime_manifest"]).encode()
            )
            forged_runtime_manifest = fixture.expected_manifest(
                forged_runtime_proof,
                script_text="script\n",
                basename="closed-film",
            )
            with self.assertRaises(CanonicalExportArtifactError) as runtime_extra:
                build_lightweight_package(
                    verified_master=master,
                    render_proof=forged_runtime_proof,
                    expected_package_manifest=forged_runtime_manifest,
                    script_text="script\n",
                    output_root=output_root,
                    package_basename="closed-film",
                )
            self.assertEqual(runtime_extra.exception.code, "invalid_runtime_manifest")

            forged_occurrence = copy.deepcopy(manifest)
            forged_occurrence["usage_occurrences"][0]["database_row_id"] = "row_one"
            forged_occurrence["usage_sha256"] = _sha256(
                canonical_json(forged_occurrence["usage_occurrences"]).encode()
            )
            with self.assertRaises(CanonicalExportArtifactError) as occurrence_extra:
                build_lightweight_package(
                    verified_master=master,
                    render_proof=proof,
                    expected_package_manifest=forged_occurrence,
                    script_text="script\n",
                    output_root=output_root,
                    package_basename="closed-film",
                )
            self.assertEqual(occurrence_extra.exception.code, "invalid_usage_occurrence")

    def test_distinct_video_spans_can_share_one_actual_read_source(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-package-shared-video-source-"
        ) as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            proposed = semantic()
            proposed["script"]["blocks"].insert(
                1,
                {"block_id": "middle", "text": "Another part of the same video"},
            )
            proposed["material_hints"].append(
                {
                    "hint_id": "hint_second_span",
                    "evidence_ref": SECOND_SPAN_REF,
                    "script_block_ids": ["middle"],
                    "reason": "Use another verified span from the same video.",
                }
            )
            blueprint = compile_proposed_blueprint(proposed)
            rows = evidence_rows()
            rows.append(
                {
                    "evidence_ref": SECOND_SPAN_REF,
                    "proof": {
                        "kind": "span",
                        "span_id": SECOND_SPAN_ID,
                        "asset_id": VIDEO_ID,
                        "asset_sha256": VIDEO_SHA,
                        "analysis_run_id": ANALYSIS_RUN_ID,
                        "analysis_revision": 1,
                        "input_asset_sha256": VIDEO_SHA,
                        "start_ms": 7_000,
                        "end_ms": 13_000,
                    },
                }
            )
            coverage = compile_baseline_coverage_plan(
                project_id="project_timeline",
                revision=1,
                parent=None,
                blueprint=blueprint,
                blueprint_binding={
                    key: blueprint_binding(blueprint)[key]
                    for key in ("revision", "content_sha256", "semantic_sha256")
                },
                evidence_rows=rows,
            )
            sources = source_rows()
            sources.append(
                {
                    "evidence_ref": SECOND_SPAN_REF,
                    "asset_id": VIDEO_ID,
                    "asset_sha256": VIDEO_SHA,
                    "asset_source_id": VIDEO_SOURCE_ID,
                }
            )
            result, _, _ = compile_result(
                blueprint=blueprint,
                plan=coverage,
                sources=sources,
            )
            fixture.result = result
            fixture.blueprint = blueprint
            fixture.coverage = coverage
            fixture.timeline = result["timeline"]
            fixture.bindings = result["source_bindings"]

            master, render_proof = fixture.render()
            manifest = fixture.expected_manifest(
                render_proof,
                script_text="First memory\nAnother part of the same video\nLast memory\n",
                basename="shared-video-film",
            )
            package = build_lightweight_package(
                verified_master=master,
                render_proof=render_proof,
                expected_package_manifest=manifest,
                script_text="First memory\nAnother part of the same video\nLast memory\n",
                output_root=output_root,
                package_basename="shared-video-film",
            )

            actual_read = render_proof["runtime_manifest"]["actual_read_manifest"]
            video_bindings = [
                row
                for row in actual_read["source_bindings"]
                if row["media_kind"] == "video"
            ]
            video_snapshots = [
                row
                for row in actual_read["snapshots"]
                if row["media_kind"] == "video"
            ]
            video_usage = [
                row
                for row in package["usage_occurrences"]
                if row["media_kind"] == "video"
            ]
            self.assertEqual(
                [row["asset_source_id"] for row in video_bindings],
                [VIDEO_SOURCE_ID, VIDEO_SOURCE_ID],
            )
            self.assertEqual(
                [row["asset_source_id"] for row in video_snapshots],
                [VIDEO_SOURCE_ID, VIDEO_SOURCE_ID],
            )
            self.assertEqual(
                [row["asset_source_id"] for row in video_usage],
                [VIDEO_SOURCE_ID, VIDEO_SOURCE_ID],
            )
            self.assertEqual(
                len({row["clip_id"] for row in video_bindings}),
                2,
            )
            self.assertEqual(
                {row["evidence_ref"] for row in video_bindings},
                {SPAN_REF, SECOND_SPAN_REF},
            )

            forged_runtime = copy.deepcopy(render_proof["runtime_manifest"])
            forged_occurrences = copy.deepcopy(render_proof["usage_occurrences"])
            forged_bindings = forged_runtime["actual_read_manifest"][
                "source_bindings"
            ]
            forged_snapshots = forged_runtime["actual_read_manifest"]["snapshots"]
            image_source_id = forged_bindings[0]["asset_source_id"]
            forged_bindings[1]["asset_source_id"] = image_source_id
            forged_snapshots[1]["asset_source_id"] = image_source_id
            forged_occurrences[1]["asset_source_id"] = image_source_id
            forged_occurrences[1]["source_binding_sha256"] = _sha256(
                canonical_json(forged_bindings[1]).encode()
            )
            forged_source_digest = _sha256(canonical_json(forged_bindings).encode())
            forged_runtime["timeline_source_bindings_sha256"] = forged_source_digest
            forged_runtime["actual_read_manifest"][
                "source_bindings_sha256"
            ] = forged_source_digest
            with self.assertRaises(CanonicalExportArtifactError) as conflicting_source:
                canonical_artifacts._validate_runtime_manifest(
                    forged_runtime,
                    occurrences=forged_occurrences,
                )
            self.assertEqual(
                conflicting_source.exception.code,
                "invalid_actual_read_binding",
            )

    def test_package_has_exact_roles_digests_txt_marker_and_no_originals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-package-") as directory:
            workspace = Path(directory).resolve()
            source_root = workspace / "sources"
            output_root = workspace / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            _, render_proof, manifest_expected, package = self._build(fixture, output_root)
            self.assertEqual(
                manifest_expected,
                MediaRepository._canonical_export_package_manifest_value(
                    operation_id=str(manifest_expected["export_id"]),
                    job_id=str(manifest_expected["job_id"]),
                    project_id=str(manifest_expected["project_id"]),
                    timeline_binding=manifest_expected["timeline_binding"],
                    profile="export-1080p",
                    package_basename="memory-film",
                    video_sha256=str(render_proof["video_sha256"]),
                    video_size_bytes=int(render_proof["video_size_bytes"]),
                    video_duration_ms=int(render_proof["video_duration_ms"]),
                    script_sha256=_sha256(b"First memory\nLast memory\n"),
                    script_size_bytes=len(b"First memory\nLast memory\n"),
                    occurrences=render_proof["usage_occurrences"],
                    runtime_manifest=render_proof["runtime_manifest"],
                ),
            )
            package_dir = output_root / "memory-film"
            self.assertEqual(
                {path.name for path in package_dir.iterdir()},
                {VIDEO_FILENAME, SCRIPT_FILENAME, MANIFEST_FILENAME, USAGE_FILENAME, COMPLETION_MARKER_FILENAME},
            )
            manifest = json.loads((package_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["package_roles"],
                ["final_video", "script", "package_manifest", "human_usage_list", "completion_marker"],
            )
            self.assertEqual(manifest["optional_roles"], {"subtitle": "absent", "cover": "absent"})
            self.assertEqual(manifest["source_policy"]["library_originals_copied"], False)
            self.assertEqual((package_dir / SCRIPT_FILENAME).read_text(encoding="utf-8"), "First memory\nLast memory\n")
            self.assertEqual((package_dir / USAGE_FILENAME).read_text(encoding="utf-8"), usage_text_from_manifest(manifest))
            marker = json.loads((package_dir / COMPLETION_MARKER_FILENAME).read_text(encoding="utf-8"))
            self.assertEqual(marker["package_manifest_sha256"], package["package_manifest_sha256"])
            self.assertEqual(marker["required_file_sha256"][SCRIPT_FILENAME], _sha256(b"First memory\nLast memory\n"))
            self.assertEqual(package["usage_occurrences"], render_proof["usage_occurrences"])
            self.assertEqual(
                set(package["core_artifact_proof"]),
                {
                    "video_sha256",
                    "video_size_bytes",
                    "video_duration_ms",
                    "script_sha256",
                    "script_size_bytes",
                    "package_manifest",
                    "package_manifest_sha256",
                    "human_usage_sha256",
                    "completion_marker_sha256",
                },
            )
            self.assertEqual(
                package["core_artifact_proof"]["human_usage_sha256"],
                _sha256((package_dir / USAGE_FILENAME).read_bytes()),
            )
            self.assertEqual(
                package["core_artifact_proof"]["completion_marker_sha256"],
                _sha256((package_dir / COMPLETION_MARKER_FILENAME).read_bytes()),
            )
            self.assertEqual(
                verify_existing_package(
                    output_root=output_root,
                    package_basename="memory-film",
                    expected_package_manifest=manifest_expected,
                ),
                package,
            )
            extra = package_dir / "unexpected.bin"
            extra.write_bytes(b"not allowed")
            with self.assertRaises(CanonicalExportArtifactError) as extra_file:
                verify_existing_package(output_root=output_root, package_basename="memory-film")
            self.assertEqual(extra_file.exception.code, "package_file_set_mismatch")
            extra.unlink()

            marker["unexpected"] = True
            (package_dir / COMPLETION_MARKER_FILENAME).write_text(
                canonical_json(marker),
                encoding="utf-8",
            )
            with self.assertRaises(CanonicalExportArtifactError) as marker_extra:
                verify_existing_package(output_root=output_root, package_basename="memory-film")
            self.assertEqual(marker_extra.exception.code, "package_completion_marker_invalid")

    def test_held_output_root_fd_survives_real_directory_replacement_race(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-output-root-fd-race-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_locator = root / "exports"
            approved_root = root / "exports-approved-inode"
            source_root.mkdir()
            output_locator.mkdir()
            fixture = _ArtifactFixture(source_root)
            master, render_proof = fixture.render(workspace_name="render-held-root")
            manifest = fixture.expected_manifest(
                render_proof,
                script_text="script\n",
                basename="held-root-film",
            )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            approved_fd = os.open(output_locator, flags)
            try:
                approved_identity = (os.fstat(approved_fd).st_dev, os.fstat(approved_fd).st_ino)
                output_locator.rename(approved_root)
                output_locator.mkdir()
                replacement_identity = (
                    output_locator.stat().st_dev,
                    output_locator.stat().st_ino,
                )
                self.assertNotEqual(approved_identity, replacement_identity)

                package = build_lightweight_package(
                    verified_master=master,
                    render_proof=render_proof,
                    expected_package_manifest=manifest,
                    script_text="script\n",
                    output_root_fd=approved_fd,
                    package_basename="held-root-film",
                )
                self.assertEqual(
                    (os.fstat(approved_fd).st_dev, os.fstat(approved_fd).st_ino),
                    approved_identity,
                )
                self.assertTrue((approved_root / "held-root-film").is_dir())
                self.assertEqual(list(output_locator.iterdir()), [])

                self.assertEqual(
                    verify_existing_package(
                        output_root_fd=approved_fd,
                        package_basename="held-root-film",
                        expected_package_manifest=manifest,
                    ),
                    package,
                )
                inspected = inspect_existing_package(
                    output_root_fd=approved_fd,
                    package_basename="held-root-film",
                    expected_package_manifest=manifest,
                )
                self.assertEqual(inspected, {"state": "complete", "proof": package})
                self.assertEqual(
                    inspect_existing_package(
                        output_root=output_locator,
                        package_basename="held-root-film",
                        expected_package_manifest=manifest,
                    ),
                    {"state": "missing", "code": "package_missing"},
                )
                with self.assertRaises(CanonicalExportArtifactError) as ambiguous:
                    verify_existing_package(
                        output_root=output_locator,
                        output_root_fd=approved_fd,
                        package_basename="held-root-film",
                    )
                self.assertEqual(ambiguous.exception.code, "output_root_authority_invalid")

                quarantined = quarantine_published_package(
                    output_root_fd=approved_fd,
                    package_basename="held-root-film",
                    expected_package_manifest=manifest,
                )
                self.assertEqual(
                    (quarantined["state"], quarantined["replayed"]),
                    ("quarantined", False),
                )
                self.assertFalse((approved_root / "held-root-film").exists())
                self.assertEqual(
                    len(list(approved_root.glob(".memolens-quarantine-*"))),
                    1,
                )
                self.assertEqual(list(output_locator.iterdir()), [])
                self.assertEqual(
                    (os.fstat(approved_fd).st_dev, os.fstat(approved_fd).st_ino),
                    approved_identity,
                )
            finally:
                os.close(approved_fd)

    def test_post_publish_verify_rejects_final_video_race_instead_of_returning_staging_proof(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-post-publish-race-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            master, render_proof = fixture.render(workspace_name="render-post-publish-race")
            manifest = fixture.expected_manifest(
                render_proof,
                script_text="script\n",
                basename="post-publish-race",
            )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            output_root_fd = os.open(output_root, flags)
            stages: list[str] = []

            def tamper_after_publish(stage: str) -> None:
                stages.append(stage)
                if stage != "package_published":
                    return
                package_fd = os.open("post-publish-race", flags, dir_fd=output_root_fd)
                try:
                    video_fd = os.open(
                        VIDEO_FILENAME,
                        os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=package_fd,
                    )
                    try:
                        os.write(video_fd, b"post-publish-tamper")
                        os.fsync(video_fd)
                    finally:
                        os.close(video_fd)
                finally:
                    os.close(package_fd)

            try:
                with self.assertRaises(CanonicalExportArtifactError) as tampered:
                    build_lightweight_package(
                        verified_master=master,
                        render_proof=render_proof,
                        expected_package_manifest=manifest,
                        script_text="script\n",
                        output_root_fd=output_root_fd,
                        package_basename="post-publish-race",
                        fault_inject=tamper_after_publish,
                    )
                self.assertEqual(tampered.exception.code, "package_video_digest_mismatch")
                self.assertIn("package_published", stages)
                self.assertTrue((output_root / "post-publish-race").is_dir())
            finally:
                os.close(output_root_fd)

    def test_package_published_stage_precedes_output_root_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-publish-fsync-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            master, render_proof = fixture.render(workspace_name="render-publish-fsync")
            manifest = fixture.expected_manifest(
                render_proof,
                script_text="script\n",
                basename="publish-fsync",
            )
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            output_root_fd = os.open(output_root, flags)
            root_identity = (os.fstat(output_root_fd).st_dev, os.fstat(output_root_fd).st_ino)
            stages: list[str] = []
            real_fsync = os.fsync

            def fail_only_output_root_fsync(descriptor: int) -> None:
                metadata = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) == root_identity:
                    raise OSError("injected output-root fsync failure")
                real_fsync(descriptor)

            try:
                with mock.patch.object(os, "fsync", side_effect=fail_only_output_root_fsync):
                    with self.assertRaisesRegex(OSError, "output-root fsync failure"):
                        build_lightweight_package(
                            verified_master=master,
                            render_proof=render_proof,
                            expected_package_manifest=manifest,
                            script_text="script\n",
                            output_root_fd=output_root_fd,
                            package_basename="publish-fsync",
                            fault_inject=stages.append,
                        )
                self.assertIn("package_published", stages)
                self.assertTrue((output_root / "publish-fsync").is_dir())
            finally:
                os.close(output_root_fd)

    def test_repeat_occurrences_are_not_collapsed_and_intervals_remain_half_open(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-repeat-package-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            proposed = semantic()
            repeated_span_id = f"seg_{VIDEO_SHA[:24]}_1_1"
            repeated_span_ref = f"memolens://evidence/span/{repeated_span_id}"
            proposed["script"]["blocks"].append(
                {"block_id": "repeat_ending", "text": "Last memory again"}
            )
            proposed["material_hints"].append(
                {
                    "hint_id": "hint_span_repeat",
                    "evidence_ref": repeated_span_ref,
                    "script_block_ids": ["repeat_ending"],
                    "reason": "Repeat the bounded verified video source as a distinct occurrence.",
                }
            )
            blueprint = compile_proposed_blueprint(proposed)
            repeated_evidence = evidence_rows()
            repeated_evidence.append(
                {
                    "evidence_ref": repeated_span_ref,
                    "proof": {
                        "kind": "span",
                        "span_id": repeated_span_id,
                        "asset_id": fixture.bindings[1]["asset_id"],
                        "asset_sha256": VIDEO_SHA,
                        "analysis_run_id": ANALYSIS_RUN_ID,
                        "analysis_revision": 1,
                        "input_asset_sha256": VIDEO_SHA,
                        "start_ms": 1_000,
                        "end_ms": 7_000,
                    },
                }
            )
            coverage = compile_baseline_coverage_plan(
                project_id="project_timeline",
                revision=1,
                parent=None,
                blueprint=blueprint,
                blueprint_binding={
                    "revision": blueprint["revision"],
                    "content_sha256": blueprint_content_sha256(blueprint),
                    "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
                },
                evidence_rows=repeated_evidence,
            )
            repeated_source_id = f"src_{'4' * 24}"
            sources = source_rows()
            sources.append(
                {
                    "evidence_ref": repeated_span_ref,
                    "asset_id": fixture.bindings[1]["asset_id"],
                    "asset_sha256": VIDEO_SHA,
                    "asset_source_id": repeated_source_id,
                }
            )
            result, _, _ = compile_result(
                blueprint=blueprint,
                plan=coverage,
                sources=sources,
                current_blueprint_binding=blueprint_binding(blueprint),
                current_coverage_binding=coverage_binding(coverage),
            )
            fixture.result = result
            fixture.blueprint = blueprint
            fixture.coverage = coverage
            fixture.timeline = result["timeline"]
            fixture.bindings = result["source_bindings"]
            video_path = source_root / "ending-video.mov"
            fixture.records[repeated_source_id] = _source_record(
                video_path,
                source_id=repeated_source_id,
                asset_id=str(fixture.bindings[2]["asset_id"]),
                digest=VIDEO_SHA,
                kind="video",
            )
            master, proof = fixture.render()
            manifest = fixture.expected_manifest(
                proof,
                script_text="script\n",
                basename="repeat-film",
            )
            package = build_lightweight_package(
                verified_master=master,
                render_proof=proof,
                expected_package_manifest=manifest,
                script_text="script\n",
                output_root=output_root,
                package_basename="repeat-film",
            )
            self.assertEqual(len(package["usage_occurrences"]), 3)
            text = (output_root / "repeat-film" / USAGE_FILENAME).read_text(encoding="utf-8")
            self.assertIn("3. ending-video.mov", text)
            self.assertEqual(text.count(str(proof["usage_occurrences"][2]["asset_id"])), 2)

    def test_safe_name_no_overwrite_tamper_and_marker_fault_states(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-package-safety-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            master, proof = fixture.render()
            manifest = fixture.expected_manifest(
                proof,
                script_text="script\n",
                basename="safe-film",
            )
            kwargs = {
                "verified_master": master,
                "render_proof": proof,
                "expected_package_manifest": manifest,
                "script_text": "script\n",
                "output_root": output_root,
            }
            for unsafe in ("../escape", "a/b", ".hidden", "bad name"):
                with self.subTest(name=unsafe), self.assertRaises(CanonicalExportArtifactError) as raised:
                    unsafe_manifest = copy.deepcopy(manifest)
                    unsafe_manifest["package_basename"] = unsafe
                    build_lightweight_package(
                        **{**kwargs, "expected_package_manifest": unsafe_manifest},
                        package_basename=unsafe,
                    )
                self.assertEqual(raised.exception.code, "invalid_package_basename")

            build_lightweight_package(**kwargs, package_basename="safe-film")
            with self.assertRaises(CanonicalExportArtifactError) as exists:
                build_lightweight_package(**kwargs, package_basename="safe-film")
            self.assertEqual(exists.exception.code, "package_exists")
            (output_root / "safe-film" / VIDEO_FILENAME).write_bytes(b"tampered")
            with self.assertRaises(CanonicalExportArtifactError) as tampered:
                verify_existing_package(output_root=output_root, package_basename="safe-film")
            self.assertEqual(tampered.exception.code, "package_video_digest_mismatch")

            def before_marker(stage: str) -> None:
                if stage == "package_manifest_written":
                    raise RuntimeError("before marker")

            with self.assertRaisesRegex(RuntimeError, "before marker"):
                before_manifest = fixture.expected_manifest(
                    proof,
                    script_text="script\n",
                    basename="before-marker",
                )
                build_lightweight_package(
                    **{**kwargs, "expected_package_manifest": before_manifest},
                    package_basename="before-marker",
                    fault_inject=before_marker,
                )
            stage = next(output_root.glob(".before-marker.staging-*"))
            self.assertEqual(
                inspect_package_directory(package_directory=stage, package_basename="before-marker")["state"],
                "incomplete",
            )
            self.assertFalse((output_root / "before-marker").exists())

            def after_marker(stage_name: str) -> None:
                if stage_name == "package_completion_marker_written":
                    raise RuntimeError("after marker")

            with self.assertRaisesRegex(RuntimeError, "after marker"):
                after_manifest = fixture.expected_manifest(
                    proof,
                    script_text="script\n",
                    basename="after-marker",
                )
                build_lightweight_package(
                    **{**kwargs, "expected_package_manifest": after_manifest},
                    package_basename="after-marker",
                    fault_inject=after_marker,
                )
            stage = next(output_root.glob(".after-marker.staging-*"))
            self.assertEqual(
                inspect_package_directory(
                    package_directory=stage,
                    package_basename="after-marker",
                    expected_package_manifest=after_manifest,
                )["state"],
                "complete",
            )
            self.assertFalse((output_root / "after-marker").exists())

    def test_exact_published_orphan_is_atomically_quarantined_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-package-quarantine-") as directory:
            root = Path(directory).resolve()
            source_root = root / "sources"
            output_root = root / "exports"
            source_root.mkdir()
            output_root.mkdir()
            fixture = _ArtifactFixture(source_root)
            _, _, manifest, _ = self._build(fixture, output_root, basename="orphan-film")
            wrong_manifest = copy.deepcopy(manifest)
            wrong_manifest["job_id"] = "different_job"
            with self.assertRaises(CanonicalExportArtifactError) as mismatch:
                quarantine_published_package(
                    output_root=output_root,
                    package_basename="orphan-film",
                    expected_package_manifest=wrong_manifest,
                )
            self.assertEqual(mismatch.exception.code, "package_export_binding_mismatch")
            self.assertTrue((output_root / "orphan-film").is_dir())

            first = quarantine_published_package(
                output_root=output_root,
                package_basename="orphan-film",
                expected_package_manifest=manifest,
            )
            self.assertEqual((first["state"], first["replayed"]), ("quarantined", False))
            self.assertFalse((output_root / "orphan-film").exists())
            hidden = list(output_root.glob(".memolens-quarantine-*"))
            self.assertEqual(len(hidden), 1)
            second = quarantine_published_package(
                output_root=output_root,
                package_basename="orphan-film",
                expected_package_manifest=manifest,
            )
            self.assertEqual((second["state"], second["replayed"]), ("quarantined", True))
            self.assertEqual(second["quarantine_id"], first["quarantine_id"])
            self.assertNotIn(str(output_root), canonical_json(first))


@unittest.skipUnless(
    binary_capability("ffmpeg").get("available") and binary_capability("ffprobe").get("available"),
    "FFmpeg 6+ and ffprobe 6+ are required",
)
class RealFFmpegSmokeTests(unittest.TestCase):
    def test_synthesized_two_video_no_audio_master_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-real-two-video-master-") as directory:
            root = Path(directory).resolve()
            two_video = root / "two-video-no-audio.mp4"
            subprocess.run(
                [
                    str(shutil.which("ffmpeg")),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=64x36:r=30:d=1",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=blue:s=64x36:r=30:d=1",
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:v:0",
                    "-c:v",
                    "mpeg4",
                    "-q:v",
                    "5",
                    "-an",
                    "-t",
                    "1",
                    str(two_video),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            raw_probe = subprocess.run(
                [
                    str(shutil.which("ffprobe")),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=index,codec_type",
                    "-of",
                    "json",
                    str(two_video),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            streams = json.loads(raw_probe.stdout)["streams"]
            self.assertEqual([stream["codec_type"] for stream in streams], ["video", "video"])

            fixture_root = root / "fixture"
            fixture_root.mkdir()
            fixture = _ArtifactFixture(fixture_root)

            def install_two_video_master(
                argv: list[str],
                _timeout: float,
                _cancelled,
            ) -> None:
                output = Path(argv[-1])
                if output.name == "master.mp4":
                    shutil.copyfile(two_video, output)
                else:
                    output.write_bytes(b"synthetic-video-only-clip")

            with self.assertRaises(CanonicalExportArtifactError) as multiple_video:
                fixture.render(
                    workspace_name="two-video-render",
                    command_runner=install_two_video_master,
                    use_default_probe=True,
                )
            self.assertEqual(multiple_video.exception.code, "master_video_stream_count_invalid")

    def test_one_small_real_image_export(self) -> None:
        with tempfile.TemporaryDirectory(prefix="memolens-real-canonical-export-") as directory:
            root = Path(directory).resolve()
            image_path = root / "still.png"
            Image.new("RGB", (64, 36), "navy").save(image_path)
            image_sha = _sha256(image_path.read_bytes())
            asset_id = f"asset_{image_sha[:24]}"
            project_id = "real_export_project"

            def stable(prefix: str, *parts: object) -> str:
                material = "\0".join(str(part) for part in parts)
                return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:24]}"

            timeline_id = stable("timeline", project_id)
            evidence_ref = f"memolens://evidence/asset/{asset_id}"
            clip_id = stable(
                "clip",
                timeline_id,
                "beat_real",
                "assignment_real",
                evidence_ref,
                asset_id,
                image_sha,
                "image",
            )
            timeline = {
                "object": "memolens.canonical_timeline",
                "schema_version": "1",
                "timeline_id": timeline_id,
                "project_id": project_id,
                "revision": 1,
                "parent": None,
                "blueprint_binding": {
                    "revision": 1,
                    "content_sha256": "1" * 64,
                    "semantic_sha256": "2" * 64,
                    "operation_id": "op_blueprint_real",
                },
                "coverage_binding": {
                    "revision": 1,
                    "content_sha256": "3" * 64,
                    "evidence_manifest_sha256": "4" * 64,
                    "operation_id": "op_coverage_real",
                },
                "compiler": {
                    "id": "memolens.timeline-lowerer/v1",
                    "media": "image_and_video_span",
                    "transitions": "hard_cut",
                    "audio": "silent",
                    "subtitles": "none",
                },
                "output": {"duration_ms": 1_000, "aspect_ratio": "16:9"},
                "tracks": [
                    {
                        "track_id": stable("track", timeline_id, "primary_visual"),
                        "kind": "primary_visual",
                        "clips": [
                            {
                                "clip_id": clip_id,
                                "ordinal": 0,
                                "beat_id": "beat_real",
                                "assignment_id": "assignment_real",
                                "evidence_ref": evidence_ref,
                                "asset_id": asset_id,
                                "asset_sha256": image_sha,
                                "media_kind": "image",
                                "start_ms": 0,
                                "end_ms": 1_000,
                                "fit": "cover",
                                "audio_enabled": False,
                            }
                        ],
                    }
                ],
            }
            source_id = f"src_{'a' * 24}"
            bindings = [
                {
                    "clip_id": clip_id,
                    "evidence_ref": evidence_ref,
                    "asset_id": asset_id,
                    "asset_sha256": image_sha,
                    "asset_source_id": source_id,
                    "coverage_proof_sha256": "5" * 64,
                    "media_kind": "image",
                }
            ]
            record = _source_record(
                image_path,
                source_id=source_id,
                asset_id=asset_id,
                digest=image_sha,
                kind="image",
            )
            workspace = root / "job"
            workspace.mkdir()
            proof = render_canonical_master(
                timeline,
                bindings,
                source_resolver=lambda _source_id: record,
                job_workspace=workspace,
                master_output=workspace / "master.mp4",
                ffmpeg_binary=str(shutil.which("ffmpeg")),
                runtime_identity={"ffmpeg": "system-capability-verified"},
            )
            self.assertGreater(proof["video_size_bytes"], 0)
            self.assertEqual((proof["width"], proof["height"], proof["fps"]), (1920, 1080, 30))
            probed = subprocess.run(
                [
                    str(shutil.which("ffprobe")),
                    "-v",
                    "error",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    str(workspace / "master.mp4"),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
            stream_types = [
                stream.get("codec_type")
                for stream in json.loads(probed.stdout).get("streams", [])
            ]
            self.assertEqual(stream_types, ["video"])


if __name__ == "__main__":
    unittest.main()
