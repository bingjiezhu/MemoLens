from __future__ import annotations

from copy import deepcopy
from http.cookiejar import CookieJar
from pathlib import Path
import hashlib
import json
import sys
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from memolens_agent_client import AgentApiError, canonical_sha256  # noqa: E402
from memolens_canonical_editor import (  # noqa: E402
    CanonicalEditorError,
    PairedCanonicalEditorBackend,
    canonical_editor_historical_snapshot,
    canonical_editor_snapshot,
    closed_canonical_structural_edit,
)
from memolens_editor_server import EditorServerManager  # noqa: E402
from memolens_mcp import TOOLS  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402


BLUEPRINT_CONTENT = "1" * 64
BLUEPRINT_SEMANTIC = "2" * 64
VIDEO_SHA = "3" * 64
IMAGE_SHA = "4" * 64
IMAGE_ANALYSIS_RUN_ID = f"arun_{'a' * 32}"
IMAGE_ANALYSIS_CONTENT_SHA256 = "b" * 64
IMAGE_SOURCE_BINDING_SHA256 = "c" * 64
SPAN_ID = "seg_333333333333333333333333_1_0"
VIDEO_REF = f"memolens://evidence/span/{SPAN_ID}"
IMAGE_ASSET = f"asset_{IMAGE_SHA[:24]}"
IMAGE_REF = f"memolens://evidence/asset/{IMAGE_ASSET}"
BROWSER_FORBIDDEN_KEYS = frozenset(
    {
        "analysis_revision",
        "analysis_run_id",
        "analysis_content_sha256",
        "asset_id",
        "asset_sha256",
        "asset_source_id",
        "content_sha256",
        "coverage_proof_sha256",
        "database_uuid",
        "evidence_manifest_sha256",
        "evidence_ref",
        "input_asset_sha256",
        "operation_id",
        "proof_sha256",
        "revision_sha256",
        "semantic_sha256",
        "source_binding_sha256",
        "span_id",
        "timeline_content_sha256",
    }
)
BROWSER_FORBIDDEN_VALUES = frozenset(
    {
        "analysis_run_1",
        "db-11111111-2222-4333-8444-555555555555",
        "https://authority.invalid/private",
        "/Users/private/raw.mov",
        "lease-secret-sentinel",
        "pairing-token-sentinel",
        "src_333333333333333333333333",
        BLUEPRINT_CONTENT,
        BLUEPRINT_SEMANTIC,
        IMAGE_REF,
        IMAGE_SHA,
        IMAGE_ANALYSIS_RUN_ID,
        IMAGE_ANALYSIS_CONTENT_SHA256,
        IMAGE_SOURCE_BINDING_SHA256,
        SPAN_ID,
        VIDEO_REF,
        VIDEO_SHA,
    }
)


def _keys(value):
    if isinstance(value, dict):
        result = set(value)
        for nested in value.values():
            result.update(_keys(nested))
        return result
    if isinstance(value, list):
        result = set()
        for nested in value:
            result.update(_keys(nested))
        return result
    return set()


def _raw_snapshot(revision: int) -> tuple[dict, dict, dict]:
    blueprint = {
        "revision": 1,
        "content_sha256": BLUEPRINT_CONTENT,
        "semantic_sha256": BLUEPRINT_SEMANTIC,
        "operation_id": "blueprint_op_1",
    }
    video_proof = {
        "kind": "span",
        "span_id": SPAN_ID,
        "asset_id": f"asset_{VIDEO_SHA[:24]}",
        "asset_sha256": VIDEO_SHA,
        "analysis_run_id": "analysis_run_1",
        "analysis_revision": 1,
        "input_asset_sha256": VIDEO_SHA,
        "start_ms": 1000,
        "end_ms": 5000,
    }
    image_proof = {
        "kind": "asset",
        "asset_id": IMAGE_ASSET,
        "asset_sha256": IMAGE_SHA,
        "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
        "analysis_revision": 1,
        "analysis_content_sha256": IMAGE_ANALYSIS_CONTENT_SHA256,
        "source_binding_sha256": IMAGE_SOURCE_BINDING_SHA256,
    }
    manifest = [
        {
            "evidence_ref": IMAGE_REF,
            "status": "verified",
            "proof_sha256": canonical_sha256(image_proof),
            "proof": image_proof,
        },
        {
            "evidence_ref": VIDEO_REF,
            "status": "verified",
            "proof_sha256": canonical_sha256(video_proof),
            "proof": video_proof,
        },
    ]
    plan = {
        "object": "memolens.coverage_plan",
        "schema_version": "1",
        "project_id": "proj_canonical",
        "revision": 1,
        "parent": None,
        "blueprint_binding": {
            key: blueprint[key]
            for key in ("revision", "content_sha256", "semantic_sha256")
        },
        "compiler": {
            "id": "memolens.coverage-baseline/v1",
            "timing_capability": "estimated_text",
            "semantic_matching": False,
            "global_optimization": False,
        },
        "output_binding": {"duration_target_ms": 1000, "aspect_ratio": "16:9"},
        "beats": [
            {
                "beat_id": "beat_opening",
                "script_block_id": "block_opening",
                "ordinal": 0,
                "text_sha256": "5" * 64,
                "timing": {"basis": "estimated_text", "start_ms": 0, "end_ms": 1000},
                "needs": [
                    {
                        "need_id": "need_opening",
                        "kind": "depiction_unspecified",
                        "priority": "should",
                        "allow_empty": True,
                    }
                ],
                "selected_assignments": [
                    {
                        "assignment_id": "assign_video",
                        "need_id": "need_opening",
                        "hint_id": "hint_video",
                        "evidence_ref": VIDEO_REF,
                        "match_type": "depiction_unspecified",
                        "timeline_duration_ms": 1000,
                        "reason_sha256": "6" * 64,
                        "locked": False,
                    }
                ],
                "alternatives": [
                    {
                        "assignment_id": "assign_image",
                        "need_id": "need_opening",
                        "hint_id": "hint_image",
                        "evidence_ref": IMAGE_REF,
                        "match_type": "depiction_unspecified",
                        "timeline_duration_ms": 1000,
                        "reason_sha256": "7" * 64,
                        "locked": False,
                        "not_selected_reason": "baseline_first_verified_selected",
                    }
                ],
                "gap": None,
            }
        ],
        "evidence_manifest": manifest,
    }
    coverage = {
        "revision": 1,
        "content_sha256": canonical_sha256(plan),
        "evidence_manifest_sha256": canonical_sha256(manifest),
        "operation_id": "coverage_op_1",
    }
    timeline = {
        "object": "memolens.canonical_timeline",
        "schema_version": "1",
        "timeline_id": "timeline_canonical",
        "project_id": "proj_canonical",
        "revision": revision,
        "parent": None if revision == 1 else {"revision": revision - 1, "content_sha256": "8" * 64},
        "blueprint_binding": blueprint,
        "coverage_binding": coverage,
        "compiler": {
            "id": "memolens.timeline-lowerer/v1",
            "media": "image_and_video_span",
            "transitions": "hard_cut",
            "audio": "silent",
            "subtitles": "none",
        },
        "output": {"duration_ms": 1000, "aspect_ratio": "16:9"},
        "tracks": [
            {
                "track_id": "track_primary",
                "kind": "primary_visual",
                "clips": [
                    {
                        "clip_id": f"clip_{revision}",
                        "ordinal": 0,
                        "beat_id": "beat_opening",
                        "assignment_id": "assign_video",
                        "evidence_ref": VIDEO_REF,
                        "asset_id": f"asset_{VIDEO_SHA[:24]}",
                        "asset_sha256": VIDEO_SHA,
                        "start_ms": 0,
                        "end_ms": 1000,
                        "fit": "cover",
                        "audio_enabled": False,
                        "media_kind": "video",
                        "span_id": SPAN_ID,
                        "analysis_run_id": "analysis_run_1",
                        "analysis_revision": 1,
                        "input_asset_sha256": VIDEO_SHA,
                        "source_in_ms": 1000,
                        "source_out_ms": 2000,
                    }
                ],
            }
        ],
    }
    timeline_head = {
        "revision": revision,
        "revision_sha256": f"{revision:x}" * 64,
        "timeline_id": "timeline_canonical",
        "timeline_content_sha256": canonical_sha256(timeline),
        "blueprint_binding": blueprint,
        "coverage_binding": coverage,
        "operation_id": f"timeline_op_{revision}",
    }
    source_bindings = [
        {
            "clip_id": f"clip_{revision}",
            "evidence_ref": VIDEO_REF,
            "asset_id": f"asset_{VIDEO_SHA[:24]}",
            "asset_sha256": VIDEO_SHA,
            "asset_source_id": "src_333333333333333333333333",
            "coverage_proof_sha256": canonical_sha256(video_proof),
            "media_kind": "video",
            "span_id": SPAN_ID,
            "analysis_run_id": "analysis_run_1",
            "analysis_revision": 1,
            "input_asset_sha256": VIDEO_SHA,
            "source_in_ms": 1000,
            "source_out_ms": 2000,
        }
    ]
    project = {
        "project": {
            "object": "creative.project_workspace",
            "schema_version": "1",
            "database_uuid": "db-11111111-2222-4333-8444-555555555555",
            "id": "proj_canonical",
            "project_id": "proj_canonical",
            "title": "Canonical project",
            "status": "current",
            "canonical_source": "creative_blueprint",
            "workspace_mode": "canonical_blueprint",
            "current_blueprint": {
                **blueprint,
                "object": "creative_blueprint.revision",
                "schema_version": "1",
            },
            "coverage": {
                "state": "current",
                "head": {key: coverage[key] for key in ("revision", "content_sha256")},
            },
            "timeline": {"state": "current", "head": timeline_head},
            "capabilities": {"timeline_mutation": True},
        }
    }
    coverage_workspace = {
        "object": "coverage_plan.workspace",
        "schema_version": "1",
        "database_uuid": "db-11111111-2222-4333-8444-555555555555",
        "project_id": "proj_canonical",
        "head": {key: coverage[key] for key in ("revision", "content_sha256")},
        "selected_revision": {**coverage, "is_head": True},
        "freshness": {"state": "current", "reasons": []},
        "coverage_plan": plan,
        "operations": [],
    }
    timeline_workspace = {
        "object": "canonical_timeline.workspace",
        "schema_version": "1",
        "database_uuid": "db-11111111-2222-4333-8444-555555555555",
        "project_id": "proj_canonical",
        "head": timeline_head,
        "selected_revision": {**timeline_head, "is_head": True},
        "freshness": {"state": "current", "reasons": []},
        "timeline": timeline,
        "source_bindings": source_bindings,
        "lifecycle": {"state": "draft", "approval": "not_established", "exportable": False},
    }
    return project, coverage_workspace, timeline_workspace


def _stable_image_clip_id(
    *,
    timeline_id: str,
    beat_id: str,
    assignment_id: str,
    evidence_ref: str,
    asset_id: str,
    asset_sha256: str,
    analysis_fields: dict[str, object] | None,
) -> str:
    parts: list[object] = [
        timeline_id,
        beat_id,
        assignment_id,
        evidence_ref,
        asset_id,
        asset_sha256,
        "image",
    ]
    if analysis_fields is not None:
        parts.extend(
            analysis_fields[field]
            for field in (
                "analysis_run_id",
                "analysis_revision",
                "analysis_content_sha256",
                "source_binding_sha256",
            )
        )
    material = "\0".join(str(part) for part in parts)
    return f"clip_{hashlib.sha256(material.encode()).hexdigest()[:24]}"


def _reseal_snapshot(project: dict, coverage: dict, timeline_workspace: dict) -> None:
    plan = coverage["coverage_plan"]
    manifest = plan["evidence_manifest"]
    coverage_binding = {
        "revision": plan["revision"],
        "content_sha256": canonical_sha256(plan),
        "evidence_manifest_sha256": canonical_sha256(manifest),
        "operation_id": coverage["selected_revision"]["operation_id"],
    }
    coverage["head"] = {
        key: coverage_binding[key] for key in ("revision", "content_sha256")
    }
    coverage["selected_revision"] = {
        **coverage_binding,
        "is_head": True,
    }
    project["project"]["coverage"] = {
        "state": "current",
        "head": deepcopy(coverage["head"]),
    }

    timeline = timeline_workspace["timeline"]
    timeline["coverage_binding"] = deepcopy(coverage_binding)
    timeline_head = timeline_workspace["head"]
    timeline_head["coverage_binding"] = deepcopy(coverage_binding)
    timeline_head["timeline_content_sha256"] = canonical_sha256(timeline)
    timeline_workspace["selected_revision"] = {
        **deepcopy(timeline_head),
        "is_head": True,
    }
    project["project"]["timeline"] = {
        "state": "current",
        "head": deepcopy(timeline_head),
    }


def _raw_image_snapshot(
    revision: int,
    *,
    legacy: bool = False,
    analysis_revision: int = 1,
) -> tuple[dict, dict, dict]:
    project, coverage, timeline_workspace = _raw_snapshot(revision)
    plan = coverage["coverage_plan"]
    manifest = plan["evidence_manifest"]
    image_evidence = next(row for row in manifest if row["evidence_ref"] == IMAGE_REF)
    analysis_fields: dict[str, object] | None
    if legacy:
        image_evidence["proof"] = {
            "kind": "asset",
            "asset_id": IMAGE_ASSET,
            "asset_sha256": IMAGE_SHA,
        }
        analysis_fields = None
    else:
        analysis_fields = {
            "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
            "analysis_revision": analysis_revision,
            "analysis_content_sha256": hashlib.sha256(
                f"image-analysis:{analysis_revision}".encode()
            ).hexdigest(),
            "source_binding_sha256": hashlib.sha256(
                f"image-source:{analysis_revision}".encode()
            ).hexdigest(),
        }
        image_evidence["proof"] = {
            "kind": "asset",
            "asset_id": IMAGE_ASSET,
            "asset_sha256": IMAGE_SHA,
            **analysis_fields,
        }
    image_evidence["proof_sha256"] = canonical_sha256(image_evidence["proof"])

    beat = plan["beats"][0]
    image_assignment = deepcopy(beat["alternatives"][0])
    image_assignment.pop("not_selected_reason", None)
    video_assignment = deepcopy(beat["selected_assignments"][0])
    video_assignment["not_selected_reason"] = "baseline_first_verified_selected"
    beat["selected_assignments"] = [image_assignment]
    beat["alternatives"] = [video_assignment]

    clip = {
        "clip_id": _stable_image_clip_id(
            timeline_id="timeline_canonical",
            beat_id="beat_opening",
            assignment_id="assign_image",
            evidence_ref=IMAGE_REF,
            asset_id=IMAGE_ASSET,
            asset_sha256=IMAGE_SHA,
            analysis_fields=analysis_fields,
        ),
        "ordinal": 0,
        "beat_id": "beat_opening",
        "assignment_id": "assign_image",
        "evidence_ref": IMAGE_REF,
        "asset_id": IMAGE_ASSET,
        "asset_sha256": IMAGE_SHA,
        "start_ms": 0,
        "end_ms": 1000,
        "fit": "cover",
        "audio_enabled": False,
        "media_kind": "image",
    }
    if analysis_fields is not None:
        clip.update(analysis_fields)
    timeline_workspace["timeline"]["tracks"][0]["clips"] = [clip]
    source_binding = {
        "clip_id": clip["clip_id"],
        "evidence_ref": IMAGE_REF,
        "asset_id": IMAGE_ASSET,
        "asset_sha256": IMAGE_SHA,
        "asset_source_id": "src_444444444444444444444444",
        "coverage_proof_sha256": image_evidence["proof_sha256"],
        "media_kind": "image",
    }
    if analysis_fields is not None:
        source_binding.update(analysis_fields)
    timeline_workspace["source_bindings"] = [source_binding]
    _reseal_snapshot(project, coverage, timeline_workspace)
    return project, coverage, timeline_workspace


def _image_snapshot(
    revision: int,
    *,
    legacy: bool = False,
    analysis_revision: int = 1,
):
    project, coverage, timeline = _raw_image_snapshot(
        revision,
        legacy=legacy,
        analysis_revision=analysis_revision,
    )
    return canonical_editor_snapshot(
        project_id="proj_canonical",
        credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
        project_response=project,
        coverage_response=coverage,
        timeline_response=timeline,
    )


def _snapshot(revision: int):
    project, coverage, timeline = _raw_snapshot(revision)
    return canonical_editor_snapshot(
        project_id="proj_canonical",
        credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
        project_response=project,
        coverage_response=coverage,
        timeline_response=timeline,
    )


def _split_snapshot(revision: int = 2, *, source_split_ms: int = 1500):
    """Build a fixture for Core's expected canonical split successor.

    The fixture deliberately gives both children fresh identities.  It is only
    a mock of the future authority endpoint; the plugin never constructs or
    submits this Timeline document.
    """

    project, coverage, workspace = _raw_snapshot(revision)
    timeline = workspace["timeline"]
    # Core split/delete successors are canonical Timeline schema v2.  Keeping
    # this fixture on v1 would let a fake backend conceal a real post-commit
    # reread failure in the plugin editor.
    timeline["schema_version"] = "2"
    parent = _snapshot(revision - 1)
    original_clip = timeline["tracks"][0]["clips"][0]
    original_binding = workspace["source_bindings"][0]
    left_duration = source_split_ms - int(original_clip["source_in_ms"])
    right_duration = int(original_clip["source_out_ms"]) - source_split_ms
    left = {
        **deepcopy(original_clip),
        "clip_id": f"clip_split_left_{revision}",
        "ordinal": 0,
        "start_ms": 0,
        "end_ms": left_duration,
        "source_out_ms": source_split_ms,
    }
    right = {
        **deepcopy(original_clip),
        "clip_id": f"clip_split_right_{revision}",
        "ordinal": 1,
        "start_ms": left_duration,
        "end_ms": left_duration + right_duration,
        "source_in_ms": source_split_ms,
    }
    timeline["parent"] = {
        "revision": revision - 1,
        "content_sha256": parent.expected_timeline_head[
            "timeline_content_sha256"
        ],
    }
    timeline["tracks"][0]["clips"] = [left, right]
    timeline["output"]["duration_ms"] = left_duration + right_duration
    workspace["source_bindings"] = [
        {
            **deepcopy(original_binding),
            "clip_id": left["clip_id"],
            "source_out_ms": source_split_ms,
        },
        {
            **deepcopy(original_binding),
            "clip_id": right["clip_id"],
            "source_in_ms": source_split_ms,
        },
    ]
    _reseal_snapshot(project, coverage, workspace)
    return canonical_editor_snapshot(
        project_id="proj_canonical",
        credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
        project_response=project,
        coverage_response=coverage,
        timeline_response=workspace,
    )


def _historical_snapshot(current_revision: int = 2, target_revision: int = 1):
    current = _snapshot(current_revision)
    _project, _coverage, current_workspace = _raw_snapshot(current_revision)
    _target_project, _target_coverage, target_workspace = _raw_snapshot(
        target_revision
    )
    target_workspace["head"] = deepcopy(current_workspace["head"])
    target_workspace["selected_revision"]["is_head"] = False
    return canonical_editor_historical_snapshot(
        current=current,
        timeline_response=target_workspace,
    )


def _cross_binding_historical_snapshot():
    current = _snapshot(2)
    _project, _coverage, current_workspace = _raw_snapshot(2)
    _target_project, _target_coverage, target_workspace = _raw_snapshot(1)
    old_blueprint = {
        "revision": 1,
        "content_sha256": "a" * 64,
        "semantic_sha256": "b" * 64,
        "operation_id": "blueprint_op_old",
    }
    old_coverage = {
        "revision": 1,
        "content_sha256": "c" * 64,
        "evidence_manifest_sha256": "d" * 64,
        "operation_id": "coverage_op_old",
    }
    target_workspace["head"] = deepcopy(current_workspace["head"])
    target_workspace["timeline"]["blueprint_binding"] = deepcopy(old_blueprint)
    target_workspace["timeline"]["coverage_binding"] = deepcopy(old_coverage)
    target_workspace["selected_revision"].update(
        {
            "blueprint_binding": deepcopy(old_blueprint),
            "coverage_binding": deepcopy(old_coverage),
            "timeline_content_sha256": canonical_sha256(
                target_workspace["timeline"]
            ),
            "is_head": False,
        }
    )
    target_workspace["freshness"] = {
        "state": "stale_blueprint",
        "reasons": ["blueprint_head_changed"],
    }
    return canonical_editor_historical_snapshot(
        current=current,
        timeline_response=target_workspace,
    )


def _legacy_image_historical_snapshot():
    current = _image_snapshot(2, analysis_revision=2)
    _project, _coverage, current_workspace = _raw_image_snapshot(
        2,
        analysis_revision=2,
    )
    _target_project, _target_coverage, target_workspace = _raw_image_snapshot(
        1,
        legacy=True,
    )
    target_workspace["head"] = deepcopy(current_workspace["head"])
    target_workspace["timeline"]["coverage_binding"] = deepcopy(
        current.expected_coverage
    )
    current_image_evidence = next(
        row
        for row in current.coverage_plan["evidence_manifest"]
        if row["evidence_ref"] == IMAGE_REF
    )
    target_workspace["source_bindings"][0]["coverage_proof_sha256"] = (
        current_image_evidence["proof_sha256"]
    )
    target_head = deepcopy(target_workspace["selected_revision"])
    target_head["coverage_binding"] = deepcopy(current.expected_coverage)
    target_head["timeline_content_sha256"] = canonical_sha256(
        target_workspace["timeline"]
    )
    target_head["is_head"] = False
    target_workspace["selected_revision"] = target_head
    target_workspace["freshness"] = {"state": "current", "reasons": []}
    return current, canonical_editor_historical_snapshot(
        current=current,
        timeline_response=target_workspace,
    )


def _restored_snapshot(current_revision: int = 2, target_revision: int = 1):
    project, coverage, _current_workspace = _raw_snapshot(current_revision)
    _target_project, _target_coverage, target_workspace = _raw_snapshot(
        target_revision
    )
    restored_timeline = deepcopy(target_workspace["timeline"])
    restored_timeline["revision"] = current_revision + 1
    restored_timeline["parent"] = {
        "revision": current_revision,
        "content_sha256": _snapshot(
            current_revision
        ).expected_timeline_head["timeline_content_sha256"],
    }
    restored_head = {
        "revision": current_revision + 1,
        "revision_sha256": "d" * 64,
        "timeline_id": restored_timeline["timeline_id"],
        "timeline_content_sha256": canonical_sha256(restored_timeline),
        "blueprint_binding": deepcopy(restored_timeline["blueprint_binding"]),
        "coverage_binding": deepcopy(restored_timeline["coverage_binding"]),
        "operation_id": f"timeline_restore_op_{current_revision + 1}",
    }
    project["project"]["timeline"]["head"] = deepcopy(restored_head)
    target_workspace["head"] = deepcopy(restored_head)
    target_workspace["selected_revision"] = {
        **deepcopy(restored_head),
        "is_head": True,
    }
    target_workspace["timeline"] = restored_timeline
    return canonical_editor_snapshot(
        project_id="proj_canonical",
        credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
        project_response=project,
        coverage_response=coverage,
        timeline_response=target_workspace,
    )


class _Backend:
    def __init__(
        self,
        *,
        conflict: bool = False,
        reread_mismatch: bool = False,
        conflict_reread_error: bool = False,
        post_save_reread_error: bool = False,
    ) -> None:
        self.snapshots = [_snapshot(1), _snapshot(2)]
        self.index = 0
        self.conflict = conflict
        self.reread_mismatch = reread_mismatch
        self.conflict_reread_error = conflict_reread_error
        self.post_save_reread_error = post_save_reread_error
        self.saved_edits: list[dict] = []
        self.saved_requests: list[dict] = []

    def read(self):
        if self.saved_edits and self.conflict and self.conflict_reread_error:
            self.conflict_reread_error = False
            raise AgentApiError(
                "Current canonical head is temporarily unavailable.",
                code="timeline_head_unavailable",
                status=503,
            )
        if self.saved_edits and not self.conflict and self.post_save_reread_error:
            self.post_save_reread_error = False
            raise AgentApiError(
                "Canonical reread raced with admission.",
                code="timeline_head_conflict",
                status=409,
            )
        return self.snapshots[self.index]

    def save(self, *, snapshot, edit):
        self.saved_edits.append(deepcopy(dict(edit)))
        self.saved_requests.append(
            {
                "timeline_head": deepcopy(snapshot.expected_timeline_head),
                "edit": deepcopy(dict(edit)),
            }
        )
        self.index = 1
        if self.conflict:
            raise AgentApiError(
                "Canonical Timeline changed.",
                code="timeline_head_conflict",
                status=409,
            )
        head = deepcopy(self.snapshots[1].expected_timeline_head)
        if self.reread_mismatch:
            head["revision_sha256"] = "f" * 64
        return {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": "proj_canonical",
            "command_type": "timeline.apply_edit",
            "operation_id": head["operation_id"],
            "result": {
                "kind": "revision_created",
                "result_head": head,
                "edit": dict(edit),
            },
        }


class _StructuralBackend:
    can_edit = True
    can_restore = True
    can_structural_edit = True
    can_preview = True

    def __init__(self, *, unknown_once: bool = False) -> None:
        self.snapshots = [_snapshot(1), _split_snapshot()]
        self.index = 0
        self.unknown_once = unknown_once
        self.saved_requests: list[dict] = []
        self.preview_mints: list[list[str]] = []

    def read(self):
        return self.snapshots[self.index]

    def mint_preview_lease(self, *, snapshot):
        clips = snapshot.public["timeline"]["tracks"][0]["clips"]
        clip_ids = [str(clip["clip_id"]) for clip in clips]
        self.preview_mints.append(clip_ids)
        return {
            "project_id": snapshot.public["project_id"],
            "observed_head": {
                "revision": snapshot.expected_timeline_head["revision"],
                "content_sha256": snapshot.expected_timeline_head[
                    "timeline_content_sha256"
                ],
            },
            "clips": [
                {
                    "clip_id": clip["clip_id"],
                    "status": "playable",
                    "reason_code": None,
                }
                for clip in clips
            ],
        }

    @staticmethod
    def drop_preview_lease(lease):
        if lease is not None:
            lease.clear()

    def read_revision(self, revision, *, current):
        if revision != 1:
            raise AssertionError("structural fake exposes only revision one")
        return _historical_snapshot()

    def save_structural_edit(self, *, snapshot, structural_edit):
        exact = deepcopy(dict(structural_edit))
        self.saved_requests.append(
            {
                "timeline_head": deepcopy(snapshot.expected_timeline_head),
                "structural_edit": exact,
            }
        )
        if self.unknown_once:
            self.unknown_once = False
            raise MemoLensError(
                "Structural outcome is temporarily unknown.",
                code="service_unavailable",
            )
        self.index = 1
        head = deepcopy(self.snapshots[1].expected_timeline_head)
        return {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": "proj_canonical",
            "command_type": "timeline.apply_structural_edit",
            "operation_id": head["operation_id"],
            "result": {
                "kind": "revision_created",
                "result_head": head,
                "structural_edit": exact,
            },
        }


class _ThumbnailBackend(_Backend):
    def __init__(self) -> None:
        super().__init__()
        self.thumbnail_requests: list[str] = []
        self.replacement_thumbnail_requests: list[tuple[str, str]] = []

    def thumbnail_for_clip(self, *, snapshot, clip_id):
        self.asserted_thumbnail_snapshot = snapshot
        self.thumbnail_requests.append(clip_id)
        if clip_id != "clip_1":
            raise CanonicalEditorError(
                "canonical_editor_media_unavailable",
                "The requested clip has no verified thumbnail.",
                status=404,
            )
        return b"\xff\xd8verified-thumbnail\xff\xd9"

    def thumbnail_for_replacement(self, *, snapshot, clip_id, assignment_id):
        self.asserted_replacement_thumbnail_snapshot = snapshot
        self.replacement_thumbnail_requests.append((clip_id, assignment_id))
        return b"\xff\xd8verified-replacement-thumbnail\xff\xd9"


class _RestoreBackend:
    can_edit = False
    can_restore = True

    def __init__(self, *, post_save_reread_error: bool = False) -> None:
        self.current = _snapshot(2)
        self.historical = _historical_snapshot()
        self.successor = _restored_snapshot()
        self.index = 0
        self.post_save_reread_error = post_save_reread_error
        self.saved_requests: list[dict] = []

    def read(self):
        if self.saved_requests and self.post_save_reread_error:
            self.post_save_reread_error = False
            raise AgentApiError(
                "Canonical restore is durable but reread is uncertain.",
                code="timeline_head_conflict",
                status=409,
            )
        return self.current if self.index == 0 else self.successor

    def read_revision(self, revision, *, current):
        self.asserted_current = current
        if revision != 1:
            raise AssertionError("restore fake exposes only revision one")
        return self.historical

    def save_restore(self, *, snapshot, historical):
        self.saved_requests.append(
            {
                "timeline_head": deepcopy(snapshot.expected_timeline_head),
                "restore_from": deepcopy(historical.restore_from),
            }
        )
        self.index = 1
        head = deepcopy(self.successor.expected_timeline_head)
        return {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": "proj_canonical",
            "command_type": "timeline.restore_revision",
            "operation_id": head["operation_id"],
            "result": {
                "kind": "revision_restored",
                "result_head": head,
                "restore_from": deepcopy(historical.restore_from),
            },
        }


class _EditOnlyHistoryBackend(_Backend):
    can_restore = False

    def __init__(self) -> None:
        super().__init__()
        self.index = 1
        self.historical = _historical_snapshot()

    def read_revision(self, revision, *, current):
        self.asserted_current = current
        if revision != 1:
            raise AssertionError("edit-only history fake exposes only revision one")
        return self.historical


class _CrossBindingRestoreBackend(_RestoreBackend):
    def __init__(self) -> None:
        super().__init__()
        self.historical = _cross_binding_historical_snapshot()


class _LegacyImageCurrentBackend:
    can_edit = True
    can_restore = True
    can_preview = True

    def __init__(self) -> None:
        self.snapshot = _image_snapshot(1, legacy=True)
        self.preview_mints = 0
        self.saved_edits: list[dict] = []

    def read(self):
        return self.snapshot

    def mint_preview_lease(self, *, snapshot):
        self.preview_mints += 1
        return {
            "project_id": snapshot.public["project_id"],
            "observed_head": {
                "revision": snapshot.expected_timeline_head["revision"],
                "content_sha256": snapshot.expected_timeline_head[
                    "timeline_content_sha256"
                ],
            },
            "clips": [],
        }

    def save(self, *, snapshot, edit):
        self.saved_edits.append(deepcopy(dict(edit)))
        raise AssertionError("legacy image evidence must never reach canonical write")


class _LegacyImageHistoryBackend:
    can_edit = False
    can_restore = True
    can_preview = False

    def __init__(self) -> None:
        self.current, self.historical = _legacy_image_historical_snapshot()
        self.restore_writes = 0

    def read(self):
        return self.current

    def read_revision(self, revision, *, current):
        if revision != 1 or current is not self.current:
            raise AssertionError("legacy-history fake is exact-revision bound")
        return self.historical

    def save_restore(self, *, snapshot, historical):
        self.restore_writes += 1
        raise AssertionError("legacy image history must never reach canonical write")


class _DurableCanonicalStore:
    """Permanent head/receipt state shared by otherwise independent processes."""

    def __init__(self) -> None:
        self.snapshots = {1: _snapshot(1), 2: _snapshot(2)}
        self.head_revision = 1
        self.receipts: dict[str, dict] = {}
        self.write_attempts: list[dict] = []
        self.domain_mutations = 0
        self.receipt_validation_reads = 0
        self.fail_post_commit_read_once = False

    @staticmethod
    def _result(snapshot, edit: dict) -> dict:
        head = deepcopy(snapshot.expected_timeline_head)
        return {
            "object": "canonical_timeline.command_result",
            "schema_version": "1",
            "project_id": "proj_canonical",
            "command_type": "timeline.apply_edit",
            "operation_id": head["operation_id"],
            "result": {
                "kind": "revision_created",
                "result_head": head,
                "edit": deepcopy(edit),
            },
        }

    def advance_external(self) -> None:
        if self.head_revision != 1:
            raise AssertionError("durable fake supports one external successor")
        edit = {
            "op": "trim_clip",
            "clip_id": "clip_1",
            "source_in_ms": 1100,
            "source_out_ms": 2000,
        }
        response = self._result(self.snapshots[2], edit)
        self.receipts["external-successor"] = {
            "request_head": deepcopy(self.snapshots[1].expected_timeline_head),
            "edit": edit,
            "response": response,
        }
        self.head_revision = 2
        self.domain_mutations += 1


class _DurableBackend:
    def __init__(self, store: _DurableCanonicalStore) -> None:
        self.store = store
        self._saved_in_this_process = False

    def read(self):
        if self._saved_in_this_process and self.store.fail_post_commit_read_once:
            self.store.fail_post_commit_read_once = False
            raise AgentApiError(
                "Canonical commit is durable but its immediate reread is uncertain.",
                code="timeline_head_conflict",
                status=409,
            )
        snapshot = self.store.snapshots[self.store.head_revision]
        if self.store.head_revision > 1:
            current_head = snapshot.expected_timeline_head
            matching = [
                receipt
                for receipt in self.store.receipts.values()
                if receipt["response"]["result"]["result_head"] == current_head
            ]
            if len(matching) != 1:
                raise AssertionError("durable head must have one permanent receipt")
            self.store.receipt_validation_reads += 1
        return snapshot

    def save(self, *, snapshot, edit):
        exact_edit = deepcopy(dict(edit))
        identity = canonical_sha256(
            [
                "timeline.apply_edit",
                "1",
                snapshot.public["database_uuid"],
                snapshot.expected_timeline_head,
                exact_edit,
            ]
        )
        self.store.write_attempts.append(
            {
                "identity": identity,
                "timeline_head": deepcopy(snapshot.expected_timeline_head),
                "edit": exact_edit,
            }
        )
        receipt = self.store.receipts.get(identity)
        if receipt is not None:
            self._saved_in_this_process = True
            return deepcopy(receipt["response"])
        current = self.store.snapshots[
            self.store.head_revision
        ].expected_timeline_head
        if snapshot.expected_timeline_head != current:
            raise AgentApiError(
                "Canonical Timeline changed.",
                code="timeline_head_conflict",
                status=409,
            )
        next_revision = self.store.head_revision + 1
        if next_revision not in self.store.snapshots:
            raise AssertionError("durable fake supports one editor successor")
        response = self.store._result(self.store.snapshots[next_revision], exact_edit)
        self.store.receipts[identity] = {
            "request_head": deepcopy(snapshot.expected_timeline_head),
            "edit": exact_edit,
            "response": deepcopy(response),
        }
        self.store.head_revision = next_revision
        self.store.domain_mutations += 1
        self._saved_in_this_process = True
        return response


class CanonicalEditorHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = EditorServerManager(session_ttl_seconds=60, boot_ttl_seconds=30)

    def tearDown(self) -> None:
        self.manager.close()

    @staticmethod
    def _post(opener, url: str, payload: dict, origin: str):
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Origin": origin},
            method="POST",
        )
        with opener.open(request) as response:
            return response.status, json.load(response)

    def _open(self, backend, *, manager: EditorServerManager | None = None):
        target = manager or self.manager
        handoff = target.create_canonical_handoff(backend=backend)
        parsed = urlsplit(handoff["editor_url"])
        origin = f"{parsed.scheme}://{parsed.netloc}"
        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        boot = parse_qs(parsed.fragment)["boot"][0]
        _, state = self._post(
            opener,
            f"{origin}/api/sessions/{handoff['session_id']}/bootstrap",
            {"token": boot},
            origin,
        )
        return handoff, origin, opener, state

    def assert_browser_redacted(self, payload) -> None:
        self.assertEqual(BROWSER_FORBIDDEN_KEYS & _keys(payload), set())
        rendered = json.dumps(payload, sort_keys=True)
        for forbidden in BROWSER_FORBIDDEN_VALUES:
            self.assertNotIn(forbidden, rendered)

    def test_current_browser_projection_is_closed_timing_only(self) -> None:
        backend = _Backend()
        private = backend.snapshots[0].public
        private["absolute_path"] = "/Users/private/raw.mov"
        private["backend_url"] = "https://authority.invalid/private"
        private["lease_secret"] = "lease-secret-sentinel"
        private["authority_token"] = "pairing-token-sentinel"

        handoff, _origin, _opener, state = self._open(backend)

        self.assertNotIn("database_uuid", handoff)
        self.assertEqual(
            set(handoff["timeline_head"]),
            {"revision", "timeline_id"},
        )
        self.assert_browser_redacted(state)
        projection = state["projection"]
        self.assertEqual(
            projection["timeline"]["object"],
            "memolens.canonical_timeline_timing_projection",
        )
        self.assertTrue(projection["content_preview"]["canonical_timing_exact"])
        self.assertTrue(projection["content_preview"]["identity_redacted"])
        self.assertFalse(projection["content_preview"]["media_playback"])
        self.assertNotIn("content_exact", projection["content_preview"])
        clip = projection["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(
            set(clip),
            {
                "assignment_id",
                "audio_enabled",
                "beat_id",
                "clip_id",
                "end_ms",
                "fit",
                "media_kind",
                "ordinal",
                "source_in_ms",
                "source_out_ms",
                "start_ms",
            },
        )

    def test_exact_image_evidence_is_preserved_server_side_and_browser_redacted(self) -> None:
        snapshot = _image_snapshot(1)
        clip = snapshot.public["timeline"]["tracks"][0]["clips"][0]
        source = snapshot.source_bindings[0]
        candidate = snapshot.public["replacement_candidates"][clip["clip_id"]][0]
        exact_fields = {
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        }
        self.assertTrue(exact_fields.issubset(clip))
        self.assertTrue(exact_fields.issubset(source))
        self.assertEqual(source["asset_source_id"], "src_444444444444444444444444")
        # The selected image's same-Beat alternative is video; the default
        # video snapshot below proves the image-alternative path.
        self.assertEqual(candidate["media_kind"], "video")

        video_snapshot = _snapshot(1)
        image_candidate = video_snapshot.public["replacement_candidates"]["clip_1"][0]
        self.assertEqual(
            {field: image_candidate[field] for field in exact_fields},
            {
                "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
                "analysis_revision": 1,
                "analysis_content_sha256": IMAGE_ANALYSIS_CONTENT_SHA256,
                "source_binding_sha256": IMAGE_SOURCE_BINDING_SHA256,
            },
        )

        backend = _Backend()
        backend.snapshots[0] = video_snapshot
        _handoff, _origin, _opener, state = self._open(backend)
        self.assert_browser_redacted(state)
        browser_candidate = state["projection"]["replacement_candidates"]["clip_1"][0]
        self.assertEqual(
            set(browser_candidate),
            {
                "assignment_id",
                "media_kind",
                "current_slot_duration_ms",
                "available_duration_ms",
            },
        )

    def test_partial_or_contradictory_image_evidence_fails_closed(self) -> None:
        project, coverage, timeline = _raw_image_snapshot(1)
        timeline["timeline"]["tracks"][0]["clips"][0].pop(
            "analysis_content_sha256"
        )
        _reseal_snapshot(project, coverage, timeline)
        with self.assertRaises(CanonicalEditorError) as stripped_clip:
            canonical_editor_snapshot(
                project_id="proj_canonical",
                credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
                project_response=project,
                coverage_response=coverage,
                timeline_response=timeline,
            )
        self.assertEqual(stripped_clip.exception.code, "canonical_editor_projection_invalid")

        project, coverage, timeline = _raw_image_snapshot(1)
        timeline["source_bindings"][0]["analysis_revision"] = 2
        with self.assertRaises(CanonicalEditorError) as contradictory_source:
            canonical_editor_snapshot(
                project_id="proj_canonical",
                credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
                project_response=project,
                coverage_response=coverage,
                timeline_response=timeline,
            )
        self.assertEqual(
            contradictory_source.exception.code,
            "canonical_editor_projection_invalid",
        )

        project, coverage, timeline = _raw_snapshot(1)
        image_evidence = coverage["coverage_plan"]["evidence_manifest"][0]
        image_evidence["proof"].pop("source_binding_sha256")
        image_evidence["proof_sha256"] = canonical_sha256(image_evidence["proof"])
        _reseal_snapshot(project, coverage, timeline)
        with self.assertRaises(CanonicalEditorError) as partial_alternative:
            canonical_editor_snapshot(
                project_id="proj_canonical",
                credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
                project_response=project,
                coverage_response=coverage,
                timeline_response=timeline,
            )
        self.assertEqual(
            partial_alternative.exception.code,
            "canonical_editor_projection_invalid",
        )

        project, coverage, timeline = _raw_snapshot(1)
        image_evidence = coverage["coverage_plan"]["evidence_manifest"][0]
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            image_evidence["proof"].pop(field)
        image_evidence["proof_sha256"] = canonical_sha256(image_evidence["proof"])
        _reseal_snapshot(project, coverage, timeline)
        legacy_alternative = canonical_editor_snapshot(
            project_id="proj_canonical",
            credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
            project_response=project,
            coverage_response=coverage,
            timeline_response=timeline,
        )
        self.assertEqual(
            legacy_alternative.public["evidence_currency"],
            "legacy_non_current",
        )
        self.assertEqual(
            legacy_alternative.public["replacement_candidates"]["clip_1"],
            [],
        )

    def test_image_analysis_revision_changes_clip_identity_and_same_id_is_rejected(self) -> None:
        first = _image_snapshot(1, analysis_revision=1)
        second = _image_snapshot(2, analysis_revision=2)
        first_id = first.public["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        second_id = second.public["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        self.assertNotEqual(first_id, second_id)

        project, coverage, timeline = _raw_image_snapshot(2, analysis_revision=2)
        timeline["timeline"]["tracks"][0]["clips"][0]["clip_id"] = first_id
        timeline["source_bindings"][0]["clip_id"] = first_id
        _reseal_snapshot(project, coverage, timeline)
        with self.assertRaises(CanonicalEditorError) as reused:
            canonical_editor_snapshot(
                project_id="proj_canonical",
                credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
                project_response=project,
                coverage_response=coverage,
                timeline_response=timeline,
            )
        self.assertEqual(reused.exception.code, "canonical_editor_projection_invalid")

    def test_identity_only_current_is_visible_audit_only_and_server_bypass_is_rejected(self) -> None:
        backend = _LegacyImageCurrentBackend()
        handoff, origin, opener, state = self._open(backend)
        self.assertEqual(
            state["projection"]["evidence_currency"],
            "legacy_non_current",
        )
        self.assertFalse(state["projection"]["eligible_for_current_use"])
        self.assertEqual(
            state["projection"]["reason_code"],
            "legacy_image_identity_only",
        )
        self.assertFalse(state["capabilities"]["move_clip"])
        self.assertFalse(state["capabilities"]["replace_clip"])
        self.assertFalse(state["capabilities"]["save"])
        self.assertFalse(state["capabilities"]["media_playback"])
        self.assertEqual(backend.preview_mints, 0)
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        with self.assertRaises(HTTPError) as bypass:
            self._post(
                opener,
                f"{origin}/api/sessions/{handoff['session_id']}/actions",
                {
                    "type": "stage",
                    "edit": {
                        "op": "set_clip_duration",
                        "clip_id": clip_id,
                        "duration_ms": 1200,
                    },
                },
                origin,
            )
        self.assertEqual(bypass.exception.code, 409)
        with bypass.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_evidence_non_current",
        )
        self.assertEqual(backend.saved_edits, [])

    def test_identity_only_history_stays_visible_but_not_restorable_even_if_fresh(self) -> None:
        backend = _LegacyImageHistoryBackend()
        handoff, origin, opener, _state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        _, selected = self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        historical = selected["historical"]
        self.assertEqual(historical["freshness"]["state"], "current")
        self.assertEqual(historical["evidence_currency"], "legacy_non_current")
        self.assertFalse(historical["eligible_for_current_use"])
        self.assertFalse(historical["restore_eligible"])
        with self.assertRaises(HTTPError) as denied:
            self._post(opener, endpoint, {"type": "stage_restore"}, origin)
        self.assertEqual(denied.exception.code, 409)
        with denied.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_history_not_restorable",
        )
        self.assertEqual(backend.restore_writes, 0)

        html = (PLUGIN_ROOT / "ui" / "canonical-editor.html").read_text()
        self.assertIn("!restoreEligible", html)
        self.assertIn("state.capabilities.save !== true", html)

    def test_pending_browser_projection_redacts_private_timeline_and_head(self) -> None:
        backend = _Backend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]

        _, staged = self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            origin,
        )

        self.assert_browser_redacted(staged)
        pending = staged["pending"]
        self.assertEqual(set(pending["based_on_head"]), {"revision", "timeline_id"})
        self.assertTrue(pending["preview"]["canonical_timing_exact"])
        self.assertTrue(pending["preview"]["identity_redacted"])
        self.assertNotIn("content_exact", pending["preview"])
        self.assertEqual(
            pending["preview"]["timeline"]["object"],
            "memolens.canonical_timeline_timing_projection",
        )

    def test_history_browser_projection_redacts_k_and_pending_restore(self) -> None:
        backend = _RestoreBackend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        self.assert_browser_redacted(state)

        _, selected = self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        self.assert_browser_redacted(selected)
        historical = selected["historical"]
        self.assertEqual(
            set(historical["selected_revision"]),
            {"revision", "timeline_id"},
        )
        self.assertEqual(set(historical["freshness"]), {"state"})
        self.assertTrue(historical["content_preview"]["canonical_timing_exact"])
        self.assertTrue(historical["content_preview"]["identity_redacted"])

        _, staged = self._post(
            opener,
            endpoint,
            {"type": "stage_restore"},
            origin,
        )
        self.assert_browser_redacted(staged)
        self.assertEqual(
            set(staged["pending"]["restore_from"]),
            {"revision", "timeline_id"},
        )

    def test_replacement_browser_projection_has_route_identity_not_media_identity(self) -> None:
        backend = _ThumbnailBackend()
        handoff, origin, opener, state = self._open(backend)
        self.assert_browser_redacted(state)
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        candidate = state["projection"]["replacement_candidates"][clip_id][0]
        self.assertEqual(
            set(candidate),
            {
                "assignment_id",
                "available_duration_ms",
                "current_slot_duration_ms",
                "media_kind",
            },
        )
        preview_url = (
            f"{origin}/api/sessions/{handoff['session_id']}/"
            f"replacement-previews/{clip_id}/{candidate['assignment_id']}"
        )
        with opener.open(Request(preview_url, headers={"Origin": origin})) as response:
            self.assertEqual(response.read(), b"\xff\xd8verified-replacement-thumbnail\xff\xd9")
        self.assertEqual(
            backend.replacement_thumbnail_requests,
            [(clip_id, candidate["assignment_id"])],
        )

    def test_canonical_editor_html_never_reads_redacted_identity(self) -> None:
        html = (PLUGIN_ROOT / "ui" / "canonical-editor.html").read_text()
        for forbidden in (
            "analysis_revision",
            "analysis_run_id",
            "asset_id",
            "asset_sha256",
            "asset_source_id",
            "content-exact",
            "database-id",
            "database_uuid",
            "evidence_ref",
            "input_asset_sha256",
            "proof_sha256",
            "span_id",
            "/Users/",
            "file://",
        ):
            self.assertNotIn(forbidden, html)
        self.assertIn(
            "Canonical timing is exact while media and authority identity are redacted.",
            html,
        )
        self.assertIn("does not attest audio-stream presence", html)
        self.assertEqual(html.count("<video"), 1)
        self.assertIn('muted playsinline preload="metadata"', html)
        self.assertIn(
            "Raw MP4 preview transport may contain audio. This page disables "
            "audio playback and keeps output muted. Audio-stream presence, "
            "content, and mix are not attested; this is not final-fidelity proof.",
            html,
        )
        self.assertIn("ui.sourceVideo.defaultMuted = true", html)
        self.assertIn(
            "ui.sourceVideo.addEventListener('volumechange', enforceMutedPreview)",
            html,
        )
        self.assertIn("clip-media", html)

    def test_verified_thumbnail_is_session_bound_path_free_and_read_only(self) -> None:
        backend = _ThumbnailBackend()
        handoff, origin, opener, state = self._open(backend)
        session_id = handoff["session_id"]
        preview_url = f"{origin}/api/sessions/{session_id}/previews/clip_1"

        self.assertTrue(state["capabilities"]["media_thumbnail"])
        self.assertTrue(state["projection"]["content_preview"]["media_thumbnail"])
        self.assertTrue(state["safety"]["verified_thumbnail_bytes_proxied"])
        self.assertFalse(state["capabilities"]["media_playback"])
        self.assertFalse(state["safety"]["source_paths_loaded_by_browser"])
        with opener.open(Request(preview_url, headers={"Origin": origin})) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(response.read(), b"\xff\xd8verified-thumbnail\xff\xd9")
        self.assertEqual(backend.thumbnail_requests, ["clip_1"])
        self.assertIs(backend.asserted_thumbnail_snapshot, backend.snapshots[0])

        without_cookie = build_opener()
        with self.assertRaises(HTTPError) as unauthorized:
            without_cookie.open(Request(preview_url, headers={"Origin": origin}))
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        with self.assertRaises(HTTPError) as invalid_clip:
            opener.open(
                Request(
                    f"{origin}/api/sessions/{session_id}/previews/%2Fprivate",
                    headers={"Origin": origin},
                )
            )
        self.assertEqual(invalid_clip.exception.code, 404)
        invalid_clip.exception.close()
        self.assertEqual(backend.thumbnail_requests, ["clip_1"])

        with self.assertRaises(HTTPError) as cross_origin:
            opener.open(Request(preview_url, headers={"Origin": "http://evil.invalid"}))
        self.assertEqual(cross_origin.exception.code, 403)
        cross_origin.exception.close()

    def test_verified_replacement_thumbnail_is_clip_scoped_cookie_bound_and_read_only(self) -> None:
        backend = _ThumbnailBackend()
        backend.snapshots[0].public["replacement_candidates"]["clip_other_beat"] = [
            {
                **deepcopy(
                    backend.snapshots[0].public["replacement_candidates"]["clip_1"][0]
                ),
                "assignment_id": "assign_cross_beat",
            }
        ]
        handoff, origin, opener, state = self._open(backend)
        session_id = handoff["session_id"]
        preview_url = (
            f"{origin}/api/sessions/{session_id}/replacement-previews/"
            "clip_1/assign_image"
        )

        self.assertTrue(state["capabilities"]["replacement_thumbnail"])
        self.assertTrue(
            state["projection"]["content_preview"]["replacement_thumbnail"]
        )
        with opener.open(Request(preview_url, headers={"Origin": origin})) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/jpeg")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertEqual(
                response.read(),
                b"\xff\xd8verified-replacement-thumbnail\xff\xd9",
            )
        self.assertEqual(
            backend.replacement_thumbnail_requests,
            [("clip_1", "assign_image")],
        )
        self.assertIs(
            backend.asserted_replacement_thumbnail_snapshot,
            backend.snapshots[0],
        )

        without_cookie = build_opener()
        with self.assertRaises(HTTPError) as unauthorized:
            without_cookie.open(Request(preview_url, headers={"Origin": origin}))
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        for clip_id, assignment_id in (
            ("clip_1", "assign_unknown"),
            ("clip_1", "assign_cross_beat"),
            ("clip_other_beat", "assign_cross_beat"),
            ("clip_unknown", "assign_image"),
        ):
            with self.assertRaises(HTTPError) as unavailable:
                opener.open(
                    Request(
                        f"{origin}/api/sessions/{session_id}/replacement-previews/"
                        f"{clip_id}/{assignment_id}",
                        headers={"Origin": origin},
                    )
                )
            self.assertEqual(unavailable.exception.code, 404)
            unavailable.exception.close()
        self.assertEqual(
            backend.replacement_thumbnail_requests,
            [("clip_1", "assign_image")],
        )

        with self.assertRaises(HTTPError) as cross_origin:
            opener.open(Request(preview_url, headers={"Origin": "http://evil.invalid"}))
        self.assertEqual(cross_origin.exception.code, 403)
        cross_origin.exception.close()

    def test_paired_backend_derives_image_and_video_replacement_thumbnails_from_proof(self) -> None:
        class _MediaClient:
            def __init__(self) -> None:
                self.requests: list[dict] = []

            def media_thumbnail(self, **request):
                self.requests.append(request)
                return b"\xff\xd8candidate\xff\xd9"

        client = _MediaClient()
        backend = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={"actions": []},
            client=client,
        )
        snapshot = deepcopy(_snapshot(1))

        self.assertEqual(
            backend.thumbnail_for_replacement(
                snapshot=snapshot,
                clip_id="clip_1",
                assignment_id="assign_image",
            ),
            b"\xff\xd8candidate\xff\xd9",
        )
        self.assertEqual(
            client.requests[-1],
            {"media_kind": "image", "asset_id": IMAGE_ASSET, "span_id": None},
        )

        candidate_span_id = "seg_aaaaaaaaaaaaaaaaaaaaaaaa_1_1000"
        candidate_asset_id = "asset_aaaaaaaaaaaaaaaaaaaaaaaa"
        candidate_sha = "a" * 64
        candidate_ref = f"memolens://evidence/span/{candidate_span_id}"
        proof = {
            "kind": "span",
            "span_id": candidate_span_id,
            "asset_id": candidate_asset_id,
            "asset_sha256": candidate_sha,
            "analysis_run_id": "analysis_run_candidate",
            "analysis_revision": 1,
            "input_asset_sha256": candidate_sha,
            "start_ms": 1000,
            "end_ms": 3000,
        }
        snapshot.public["replacement_candidates"]["clip_1"].append(
            {
                "assignment_id": "assign_video_candidate",
                "evidence_ref": candidate_ref,
                "media_kind": "video",
                "asset_id": candidate_asset_id,
                "asset_sha256": candidate_sha,
                "current_slot_duration_ms": 1000,
                "available_duration_ms": 2000,
            }
        )
        snapshot.coverage_plan["evidence_manifest"].append(
            {
                "evidence_ref": candidate_ref,
                "status": "verified",
                "proof_sha256": canonical_sha256(proof),
                "proof": proof,
            }
        )
        snapshot.coverage_plan["beats"][0]["alternatives"].append(
            {
                "assignment_id": "assign_video_candidate",
                "need_id": "need_opening",
                "hint_id": "hint_video_candidate",
                "evidence_ref": candidate_ref,
                "match_type": "depiction_unspecified",
                "timeline_duration_ms": 1000,
                "reason_sha256": "b" * 64,
                "locked": False,
                "not_selected_reason": "verified_visual_review_fixture",
            }
        )
        self.assertEqual(
            backend.thumbnail_for_replacement(
                snapshot=snapshot,
                clip_id="clip_1",
                assignment_id="assign_video_candidate",
            ),
            b"\xff\xd8candidate\xff\xd9",
        )
        self.assertEqual(
            client.requests[-1],
            {
                "media_kind": "video",
                "asset_id": candidate_asset_id,
                "span_id": candidate_span_id,
            },
        )

        calls_before_invalid_proof = len(client.requests)
        snapshot.coverage_plan["evidence_manifest"][0]["status"] = "unverified"
        with self.assertRaisesRegex(
            CanonicalEditorError,
            "eligible same-Beat verified alternative",
        ):
            backend.thumbnail_for_replacement(
                snapshot=snapshot,
                clip_id="clip_1",
                assignment_id="assign_image",
            )
        self.assertEqual(len(client.requests), calls_before_invalid_proof)

        with self.assertRaisesRegex(
            CanonicalEditorError,
            "eligible same-Beat verified alternative",
        ):
            backend.thumbnail_for_replacement(
                snapshot=snapshot,
                clip_id="clip_1",
                assignment_id="assign_cross_beat",
            )

    def test_precommit_process_crash_loses_only_pending_and_restarts_from_durable_head(self) -> None:
        store = _DurableCanonicalStore()
        first_manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )
        second_manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )
        first_backend = _DurableBackend(store)
        second_backend = _DurableBackend(store)
        try:
            handoff, origin, opener, state = self._open(
                first_backend,
                manager=first_manager,
            )
            endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
            clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
                "clip_id"
            ]
            candidate = state["projection"]["replacement_candidates"][clip_id][0]
            self.assertEqual(candidate["assignment_id"], "assign_image")
            self.assertNotIn("asset_id", candidate)

            with self.assertRaises(HTTPError) as injected_source:
                self._post(
                    opener,
                    endpoint,
                    {
                        "type": "stage",
                        "edit": {
                            "op": "replace_clip",
                            "clip_id": clip_id,
                            "assignment_id": "assign_image",
                            "asset_id": IMAGE_ASSET,
                        },
                    },
                    origin,
                )
            self.assertEqual(injected_source.exception.code, 422)
            injected_source.exception.close()

            _, staged_replace = self._post(
                opener,
                endpoint,
                {
                    "type": "stage",
                    "edit": {
                        "op": "replace_clip",
                        "clip_id": clip_id,
                        "assignment_id": "assign_image",
                    },
                },
                origin,
            )
            self.assertEqual(
                staged_replace["pending"]["edit"],
                {
                    "op": "replace_clip",
                    "clip_id": clip_id,
                    "assignment_id": "assign_image",
                },
            )
            self.assertFalse(staged_replace["pending"]["canonical"])
            self.assertFalse(staged_replace["pending"]["persisted"])
            self.assertIsNone(staged_replace["pending"]["preview"])
            self.assertEqual(store.head_revision, 1)
            self.assertEqual(store.domain_mutations, 0)
            self.assertEqual(store.receipts, {})

            _, discarded = self._post(opener, endpoint, {"type": "discard"}, origin)
            self.assertIsNone(discarded["pending"])
            self.assertEqual(store.head_revision, 1)
            self.assertEqual(store.write_attempts, [])

            store.advance_external()
            _, refreshed = self._post(opener, endpoint, {"type": "refresh"}, origin)
            self.assertEqual(refreshed["projection"]["timeline_head"]["revision"], 2)
            self.assertIsNone(refreshed["pending"])
            self.assertGreaterEqual(store.receipt_validation_reads, 1)

            refreshed_clip = refreshed["projection"]["timeline"]["tracks"][0][
                "clips"
            ][0]["clip_id"]
            _, staged_before_crash = self._post(
                opener,
                endpoint,
                {
                    "type": "stage",
                    "edit": {
                        "op": "trim_clip",
                        "clip_id": refreshed_clip,
                        "source_in_ms": 1100,
                        "source_out_ms": 2000,
                    },
                },
                origin,
            )
            self.assertFalse(staged_before_crash["pending"]["canonical"])
            self.assertEqual(staged_before_crash["projection"]["timeline_head"]["revision"], 2)
            self.assertEqual(store.write_attempts, [])

            first_manager.close()
            restarted, next_origin, next_opener, restarted_state = self._open(
                second_backend,
                manager=second_manager,
            )
            self.assertIsNot(first_backend, second_backend)
            self.assertEqual(restarted["timeline_head"]["revision"], 2)
            self.assertEqual(
                restarted_state["projection"]["timeline_head"]["revision"],
                2,
            )
            self.assertIsNone(restarted_state["pending"])
            self.assertIsNone(restarted_state["last_save"])
            self.assertEqual(store.domain_mutations, 1)
            self.assertEqual(store.write_attempts, [])

            with self.assertRaises(HTTPError) as no_old_pending:
                self._post(
                    next_opener,
                    f"{next_origin}/api/sessions/{restarted['session_id']}/actions",
                    {"type": "save"},
                    next_origin,
                )
            self.assertEqual(no_old_pending.exception.code, 409)
            no_old_pending.exception.close()
            self.assertEqual(store.write_attempts, [])
        finally:
            first_manager.close()
            second_manager.close()

    def test_postcommit_uncertainty_crash_recovers_permanent_receipt_without_duplicate_write(self) -> None:
        store = _DurableCanonicalStore()
        store.fail_post_commit_read_once = True
        first_manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )
        second_manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )
        first_backend = _DurableBackend(store)
        second_backend = _DurableBackend(store)
        try:
            handoff, origin, opener, state = self._open(
                first_backend,
                manager=first_manager,
            )
            endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
            clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0][
                "clip_id"
            ]
            self._post(
                opener,
                endpoint,
                {
                    "type": "stage",
                    "edit": {
                        "op": "trim_clip",
                        "clip_id": clip_id,
                        "source_in_ms": 1200,
                        "source_out_ms": 2000,
                    },
                },
                origin,
            )
            with self.assertRaises(HTTPError) as uncertain:
                self._post(opener, endpoint, {"type": "save"}, origin)
            self.assertEqual(uncertain.exception.code, 503)
            with uncertain.exception as response:
                payload = json.load(response)
            self.assertEqual(
                payload["error"]["code"],
                "canonical_editor_post_commit_reconciliation_required",
            )
            self.assertNotIn("details", payload["error"])
            self.assertEqual(store.head_revision, 2)
            self.assertEqual(store.domain_mutations, 1)
            self.assertEqual(len(store.receipts), 1)
            self.assertEqual(len(store.write_attempts), 1)

            with opener.open(
                f"{origin}/api/sessions/{handoff['session_id']}"
            ) as response:
                uncertain_memory = json.load(response)
            self.assertEqual(
                uncertain_memory["projection"]["timeline_head"]["revision"],
                1,
            )
            self.assertIsNotNone(uncertain_memory["pending"])
            self.assertIsNone(uncertain_memory["last_save"])

            first_manager.close()
            restarted, next_origin, next_opener, recovered = self._open(
                second_backend,
                manager=second_manager,
            )
            self.assertIsNot(first_backend, second_backend)
            self.assertEqual(restarted["timeline_head"]["revision"], 2)
            self.assertEqual(recovered["projection"]["timeline_head"]["revision"], 2)
            self.assertIsNone(recovered["pending"])
            self.assertIsNone(recovered["last_save"])
            self.assertGreaterEqual(store.receipt_validation_reads, 1)

            permanent_receipt = next(iter(store.receipts.values()))
            self.assertEqual(permanent_receipt["request_head"]["revision"], 1)
            self.assertEqual(
                permanent_receipt["response"]["result"]["result_head"]["revision"],
                2,
            )
            with self.assertRaises(HTTPError) as no_replayed_memory:
                self._post(
                    next_opener,
                    f"{next_origin}/api/sessions/{restarted['session_id']}/actions",
                    {"type": "save"},
                    next_origin,
                )
            self.assertEqual(no_replayed_memory.exception.code, 409)
            no_replayed_memory.exception.close()
            self.assertEqual(store.domain_mutations, 1)
            self.assertEqual(len(store.receipts), 1)
            self.assertEqual(len(store.write_attempts), 1)
        finally:
            first_manager.close()
            second_manager.close()


    def test_handoff_is_project_bound_and_browser_projection_is_path_and_secret_free(self) -> None:
        handoff, origin, opener, state = self._open(_Backend())
        parsed = urlsplit(handoff["editor_url"])
        self.assertEqual(parsed.hostname, "127.0.0.1")
        self.assertTrue(parsed.path.startswith("/canonical-editor/canonical_"))
        self.assertEqual(handoff["project_id"], "proj_canonical")
        self.assertEqual(handoff["timeline_head"]["revision"], 1)
        self.assertFalse(handoff["safety"]["pairing_secret_exposed"])
        self.assertEqual(state["authority"]["origin"], "agent_plugin_editor")
        self.assertFalse(state["authority"]["semantic_confirmation"])
        self.assertEqual(
            state["projection"]["replacement_candidates"]["clip_1"][0]["assignment_id"],
            "assign_image",
        )
        rendered = json.dumps({"handoff": handoff, "state": state})
        for forbidden in (
            "proof_secret",
            "capability_id",
            "desktop_session_token",
            "/Users/",
            "absolute_path",
            "asset_source_id",
        ):
            self.assertNotIn(forbidden, rendered)
        with opener.open(f"{origin}{parsed.path}") as response:
            html = response.read().decode()
            self.assertIn("MemoLens Canonical Editor", html)
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertNotIn("proof_secret", html)

    def test_stage_is_one_closed_b2b3_edit_discard_and_save_requires_exact_reread(self) -> None:
        backend = _Backend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        _, staged = self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            origin,
        )
        self.assertFalse(staged["pending"]["canonical"])
        self.assertEqual(staged["pending"]["preview"]["object"], "canonical_timeline.pending_preview")
        _, discarded = self._post(opener, endpoint, {"type": "discard"}, origin)
        self.assertIsNone(discarded["pending"])

        _, staged_again = self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {"op": "replace_clip", "clip_id": clip_id, "assignment_id": "assign_image"},
            },
            origin,
        )
        self.assertIsNone(staged_again["pending"]["preview"])
        _, saved = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assert_browser_redacted(saved)
        self.assertEqual(saved["last_save"]["status"], "saved")
        self.assertEqual(saved["last_save"]["revision"], 2)
        self.assertEqual(saved["projection"]["timeline_head"]["revision"], 2)
        self.assertIsNone(saved["pending"])
        self.assertEqual(backend.saved_edits[-1], {
            "op": "replace_clip",
            "clip_id": clip_id,
            "assignment_id": "assign_image",
        })

    def test_unknown_editor_action_fails_closed(self) -> None:
        backend = _Backend()
        handoff, origin, opener, _state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        with self.assertRaises(HTTPError) as captured:
            self._post(opener, endpoint, {"type": "future_magic"}, origin)
        self.assertEqual(captured.exception.code, 400)
        payload = json.load(captured.exception)
        captured.exception.close()
        self.assertEqual(payload["error"]["code"], "invalid_canonical_editor_action")
        self.assertEqual(backend.saved_edits, [])

    def test_restore_only_editor_keeps_current_n_separate_and_saves_server_held_k(self) -> None:
        backend = _RestoreBackend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        self.assertFalse(state["capabilities"]["move_clip"])
        self.assertTrue(state["capabilities"]["restore_revision"])
        self.assertEqual(state["projection"]["timeline_head"]["revision"], 2)
        self.assertIsNone(state["historical"])

        with self.assertRaises(HTTPError) as injected:
            self._post(
                opener,
                endpoint,
                {
                    "type": "select_revision",
                    "revision": 1,
                    "restore_from": {"revision": 1},
                },
                origin,
            )
        self.assertEqual(injected.exception.code, 422)
        injected.exception.close()

        _, selected = self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        self.assertEqual(selected["projection"]["timeline_head"]["revision"], 2)
        self.assertEqual(selected["historical"]["selected_revision"]["revision"], 1)
        self.assertTrue(selected["historical"]["restore_eligible"])
        self.assertIsNone(selected["pending"])
        rendered = json.dumps(selected)
        self.assertNotIn("asset_source_id", rendered)
        self.assertNotIn("proof_secret", rendered)

        _, staged = self._post(
            opener,
            endpoint,
            {"type": "stage_restore"},
            origin,
        )
        self.assertEqual(
            staged["pending"]["object"],
            "memolens.canonical_editor_pending_restore",
        )
        self.assertEqual(staged["pending"]["based_on_head"]["revision"], 2)
        self.assertEqual(staged["pending"]["restore_from"]["revision"], 1)
        self.assertFalse(staged["pending"]["canonical"])

        _, saved = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(saved["last_save"]["command_type"], "timeline.restore_revision")
        self.assertEqual(saved["last_save"]["restore_from_revision"], 1)
        self.assertEqual(saved["projection"]["timeline_head"]["revision"], 3)
        self.assertIsNone(saved["historical"])
        self.assertIsNone(saved["pending"])
        self.assertEqual(backend.saved_requests, [{
            "timeline_head": backend.current.expected_timeline_head,
            "restore_from": backend.historical.restore_from,
        }])

    def test_restore_post_commit_reread_uncertainty_retains_exact_pending_identity(self) -> None:
        backend = _RestoreBackend(post_save_reread_error=True)
        handoff, origin, opener, _state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        self._post(opener, endpoint, {"type": "stage_restore"}, origin)

        with self.assertRaises(HTTPError) as unresolved:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(unresolved.exception.code, 503)
        with unresolved.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_post_commit_reconciliation_required",
        )

        with opener.open(
            f"{origin}/api/sessions/{handoff['session_id']}"
        ) as response:
            retained = json.load(response)
        self.assertEqual(retained["pending"]["restore_from"]["revision"], 1)
        self.assertEqual(retained["projection"]["timeline_head"]["revision"], 2)
        self.assertIsNone(retained["last_save"])

        _, reconciled = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertIsNone(reconciled["pending"])
        self.assertEqual(reconciled["last_save"]["status"], "saved")
        self.assertEqual(reconciled["last_save"]["restore_from_revision"], 1)
        self.assertEqual(reconciled["projection"]["timeline_head"]["revision"], 3)
        self.assertEqual(backend.saved_requests[0], backend.saved_requests[1])

    def test_edit_only_editor_inspects_history_but_rejects_restore(self) -> None:
        handoff, origin, opener, state = self._open(_EditOnlyHistoryBackend())
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        self.assertTrue(state["capabilities"]["inspect_history"])
        self.assertFalse(state["capabilities"]["restore_revision"])
        _, selected = self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        self.assertEqual(selected["historical"]["selected_revision"]["revision"], 1)
        with self.assertRaises(HTTPError) as denied:
            self._post(
                opener,
                endpoint,
                {"type": "stage_restore"},
                origin,
            )
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()

    def test_real_edit_only_backend_reads_history_without_restore_authority(self) -> None:
        _project, _coverage, current_workspace = _raw_snapshot(2)
        _target_project, _target_coverage, target_workspace = _raw_snapshot(1)
        target_workspace["head"] = deepcopy(current_workspace["head"])
        target_workspace["selected_revision"]["is_head"] = False

        class _HistoryClient:
            def timeline_workspace(self, project_id, revision=None):
                self.request = (project_id, revision)
                return target_workspace

        client = _HistoryClient()
        backend = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={
                "database_uuid": "db-11111111-2222-4333-8444-555555555555",
                "actions": ["timeline.apply_edit"],
            },
            client=client,
        )
        historical = backend.read_revision(1, current=_snapshot(2))
        self.assertEqual(client.request, ("proj_canonical", 1))
        self.assertTrue(historical.restore_eligible)
        self.assertFalse(backend.can_restore)

    def test_cross_binding_history_is_inspectable_but_not_restorable(self) -> None:
        handoff, origin, opener, _state = self._open(_CrossBindingRestoreBackend())
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        _, selected = self._post(
            opener,
            endpoint,
            {"type": "select_revision", "revision": 1},
            origin,
        )
        self.assertEqual(selected["historical"]["selected_revision"]["revision"], 1)
        self.assertEqual(
            selected["historical"]["freshness"]["state"],
            "stale_blueprint",
        )
        self.assertFalse(selected["historical"]["restore_eligible"])
        with self.assertRaises(HTTPError) as denied:
            self._post(opener, endpoint, {"type": "stage_restore"}, origin)
        self.assertEqual(denied.exception.code, 409)
        with denied.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_history_not_restorable",
        )

    def test_conflict_returns_409_discards_pending_and_loads_current_head(self) -> None:
        handoff, origin, opener, state = self._open(_Backend(conflict=True))
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {"op": "trim_clip", "clip_id": clip_id, "source_in_ms": 1100, "source_out_ms": 2000},
            },
            origin,
        )
        with self.assertRaises(HTTPError) as raised:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(raised.exception.code, 409)
        with raised.exception as response:
            payload = json.load(response)
        current = payload["error"]["details"]["current"]
        self.assertIsNone(current["pending"])
        self.assertEqual(current["projection"]["timeline_head"]["revision"], 2)

    def test_save_conflict_with_failed_reread_retains_pending_and_write_identity(self) -> None:
        backend = _Backend(conflict=True, conflict_reread_error=True)
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1100,
                    "source_out_ms": 2000,
                },
            },
            origin,
        )

        with self.assertRaises(HTTPError) as failed_reread:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(failed_reread.exception.code, 503)
        with failed_reread.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_conflict_refresh_required",
        )
        self.assertNotIn("details", payload["error"])

        with opener.open(
            f"{origin}/api/sessions/{handoff['session_id']}"
        ) as response:
            retained = json.load(response)
        self.assertIsNotNone(retained["pending"])
        self.assertEqual(retained["projection"]["timeline_head"]["revision"], 1)
        self.assertIsNone(retained["last_save"])

        with self.assertRaises(HTTPError) as reconciled_conflict:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(reconciled_conflict.exception.code, 409)
        with reconciled_conflict.exception as response:
            current = json.load(response)["error"]["details"]["current"]
        self.assertIsNone(current["pending"])
        self.assertEqual(current["projection"]["timeline_head"]["revision"], 2)
        self.assertEqual(backend.saved_requests[0], backend.saved_requests[1])

    def test_post_save_reread_409_is_reconciliation_not_conflict(self) -> None:
        backend = _Backend(post_save_reread_error=True)
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        clip_id = state["projection"]["timeline"]["tracks"][0]["clips"][0]["clip_id"]
        self._post(
            opener,
            endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            origin,
        )

        with self.assertRaises(HTTPError) as unresolved:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(unresolved.exception.code, 503)
        with unresolved.exception as response:
            payload = json.load(response)
        self.assertEqual(
            payload["error"]["code"],
            "canonical_editor_post_commit_reconciliation_required",
        )
        self.assertNotIn("details", payload["error"])

        with opener.open(
            f"{origin}/api/sessions/{handoff['session_id']}"
        ) as response:
            retained = json.load(response)
        self.assertIsNotNone(retained["pending"])
        self.assertEqual(retained["projection"]["timeline_head"]["revision"], 1)
        self.assertIsNone(retained["last_save"])

        _, reconciled = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertIsNone(reconciled["pending"])
        self.assertEqual(reconciled["last_save"]["status"], "saved")
        self.assertEqual(reconciled["projection"]["timeline_head"]["revision"], 2)
        self.assertEqual(backend.saved_requests[0], backend.saved_requests[1])

    def test_refresh_reads_current_head_and_reread_mismatch_never_reports_saved(self) -> None:
        backend = _Backend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        backend.index = 1
        _, refreshed = self._post(opener, endpoint, {"type": "refresh"}, origin)
        self.assertEqual(refreshed["projection"]["timeline_head"]["revision"], 2)
        self.assertIsNone(refreshed["pending"])

        mismatch = _Backend(reread_mismatch=True)
        mismatch_handoff, mismatch_origin, mismatch_opener, mismatch_state = self._open(
            mismatch
        )
        mismatch_endpoint = (
            f"{mismatch_origin}/api/sessions/{mismatch_handoff['session_id']}/actions"
        )
        clip_id = mismatch_state["projection"]["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        self._post(
            mismatch_opener,
            mismatch_endpoint,
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": clip_id,
                    "source_in_ms": 1200,
                    "source_out_ms": 2000,
                },
            },
            mismatch_origin,
        )
        with self.assertRaises(HTTPError) as rejected:
            self._post(
                mismatch_opener,
                mismatch_endpoint,
                {"type": "save"},
                mismatch_origin,
            )
        self.assertEqual(rejected.exception.code, 502)
        rejected.exception.close()
        mismatch_state_url = (
            f"{mismatch_origin}/api/sessions/{mismatch_handoff['session_id']}"
        )
        with mismatch_opener.open(mismatch_state_url) as response:
            current = json.load(response)
        self.assertIsNotNone(current["pending"])
        self.assertIsNone(current["last_save"])

    def test_security_rejects_wrong_origin_content_type_and_bootstrap_replay(self) -> None:
        handoff, origin, opener, _state = self._open(_Backend())
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        bad_host = Request(
            f"{origin}/canonical-editor/{handoff['session_id']}",
            headers={"Host": "attacker.invalid"},
        )
        with self.assertRaises(HTTPError) as denied_host:
            opener.open(bad_host)
        self.assertEqual(denied_host.exception.code, 403)
        denied_host.exception.close()
        bad_origin = Request(
            endpoint,
            data=b'{"type":"discard"}',
            headers={"Content-Type": "application/json", "Origin": "https://attacker.invalid"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as denied:
            opener.open(bad_origin)
        self.assertEqual(denied.exception.code, 403)
        denied.exception.close()
        bad_type = Request(
            endpoint,
            data=b'{"type":"discard"}',
            headers={"Content-Type": "text/plain", "Origin": origin},
            method="POST",
        )
        with self.assertRaises(HTTPError) as denied_type:
            opener.open(bad_type)
        self.assertEqual(denied_type.exception.code, 415)
        denied_type.exception.close()

        wrong_method = Request(
            endpoint,
            data=b'{"type":"discard"}',
            headers={"Content-Type": "application/json", "Origin": origin},
            method="PUT",
        )
        with self.assertRaises(HTTPError) as denied_method:
            opener.open(wrong_method)
        self.assertEqual(denied_method.exception.code, 405)
        denied_method.exception.close()

        unexpected_get = Request(
            f"{origin}/api/sessions/{handoff['session_id']}/unexpected"
        )
        with self.assertRaises(HTTPError) as denied_route:
            opener.open(unexpected_get)
        self.assertEqual(denied_route.exception.code, 404)
        denied_route.exception.close()

        boot = parse_qs(urlsplit(handoff["editor_url"]).fragment)["boot"][0]
        with self.assertRaises(HTTPError) as replay:
            self._post(
                opener,
                f"{origin}/api/sessions/{handoff['session_id']}/bootstrap",
                {"token": boot},
                origin,
            )
        self.assertEqual(replay.exception.code, 401)
        replay.exception.close()

    def test_structural_edit_is_closed_and_split_uses_absolute_source_time(self) -> None:
        snapshot = _snapshot(1)
        split, preview = closed_canonical_structural_edit(
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
            },
            snapshot=snapshot,
        )
        self.assertEqual(
            split,
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
            },
        )
        self.assertIsNotNone(preview)
        preview_clips = preview["timeline"]["tracks"][0]["clips"]
        self.assertEqual(len(preview_clips), 2)
        self.assertEqual(
            [
                (clip["source_in_ms"], clip["source_out_ms"])
                for clip in preview_clips
            ],
            [(1000, 1500), (1500, 2000)],
        )
        self.assertNotEqual(preview_clips[0]["clip_id"], "clip_1")
        self.assertNotEqual(preview_clips[1]["clip_id"], "clip_1")
        self.assertNotEqual(preview_clips[0]["clip_id"], preview_clips[1]["clip_id"])

        capped = _split_snapshot()
        template = capped.public["timeline"]["tracks"][0]["clips"][0]
        capped_clips = []
        for index in range(256):
            capped_clips.append(
                {
                    **deepcopy(template),
                    "clip_id": f"clip_cap_{index}",
                    "ordinal": index,
                    "start_ms": index * 500,
                    "end_ms": (index + 1) * 500,
                }
            )
        capped.public["timeline"]["tracks"][0]["clips"] = capped_clips
        capped.public["timeline"]["output"]["duration_ms"] = 128_000
        with self.assertRaises(CanonicalEditorError) as capped_error:
            closed_canonical_structural_edit(
                {
                    "op": "split_clip",
                    "clip_id": "clip_cap_0",
                    "source_split_ms": 1500,
                },
                snapshot=capped,
            )
        self.assertEqual(capped_error.exception.status, 422)
        self.assertIn("256", str(capped_error.exception))

        for invalid in (
            {"op": "split_clip", "clip_id": "clip_1", "offset_ms": 500},
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1099,
            },
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1901,
            },
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
                "draft_id": "draft_forbidden",
            },
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
                "timeline": snapshot.public["timeline"],
            },
            {
                "op": "split_clip",
                "clip_id": "clip_1",
                "source_split_ms": 1500,
                "operations": [],
            },
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(CanonicalEditorError) as captured:
                    closed_canonical_structural_edit(invalid, snapshot=snapshot)
                self.assertEqual(captured.exception.status, 422)

        image = _image_snapshot(1)
        image_clip_id = image.public["timeline"]["tracks"][0]["clips"][0][
            "clip_id"
        ]
        with self.assertRaises(CanonicalEditorError):
            closed_canonical_structural_edit(
                {
                    "op": "split_clip",
                    "clip_id": image_clip_id,
                    "source_split_ms": 500,
                },
                snapshot=image,
            )

    def test_delete_last_clip_is_forbidden_and_original_media_is_not_an_input(self) -> None:
        snapshot = _snapshot(1)
        with self.assertRaises(CanonicalEditorError) as captured:
            closed_canonical_structural_edit(
                {"op": "delete_clip", "clip_id": "clip_1"},
                snapshot=snapshot,
            )
        self.assertEqual(captured.exception.status, 422)
        self.assertIn("last", str(captured.exception).lower())

        two_clips = _split_snapshot()
        delete, preview = closed_canonical_structural_edit(
            {"op": "delete_clip", "clip_id": "clip_split_left_2"},
            snapshot=two_clips,
        )
        self.assertEqual(
            delete,
            {"op": "delete_clip", "clip_id": "clip_split_left_2"},
        )
        remaining = preview["timeline"]["tracks"][0]["clips"]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["clip_id"], "clip_split_right_2")
        self.assertEqual((remaining[0]["start_ms"], remaining[0]["end_ms"]), (0, 500))
        self.assertEqual(preview["timeline"]["output"]["duration_ms"], 500)

    def test_structural_stage_is_mutually_exclusive_and_does_not_write(self) -> None:
        backend = _StructuralBackend()
        handoff, origin, opener, state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        self.assertTrue(state["capabilities"]["split_clip"])
        self.assertTrue(state["capabilities"]["delete_clip"])
        _, staged = self._post(
            opener,
            endpoint,
            {
                "type": "stage_structural",
                "structural_edit": {
                    "op": "split_clip",
                    "clip_id": "clip_1",
                    "source_split_ms": 1500,
                },
            },
            origin,
        )
        self.assertEqual(
            staged["pending"]["object"],
            "memolens.canonical_editor_pending_structural_edit",
        )
        self.assertFalse(staged["pending"]["canonical"])
        self.assertFalse(staged["pending"]["persisted"])
        self.assertEqual(backend.saved_requests, [])
        self.assertFalse(staged["playback"]["available"])
        self.assertEqual(
            staged["playback"]["reason_code"],
            "canonical_editor_pending_source_change",
        )

        conflicts = (
            {
                "type": "stage",
                "edit": {
                    "op": "trim_clip",
                    "clip_id": "clip_1",
                    "source_in_ms": 1100,
                    "source_out_ms": 2000,
                },
            },
            {"type": "select_revision", "revision": 1},
            {"type": "stage_restore"},
            {"type": "refresh"},
        )
        for payload in conflicts:
            with self.subTest(payload=payload):
                with self.assertRaises(HTTPError) as denied:
                    self._post(opener, endpoint, payload, origin)
                self.assertEqual(denied.exception.code, 409)
                with denied.exception as response:
                    error = json.load(response)["error"]
                self.assertEqual(error["code"], "canonical_editor_pending_conflict")

        _, discarded = self._post(opener, endpoint, {"type": "discard"}, origin)
        self.assertIsNone(discarded["pending"])
        self.assertTrue(discarded["playback"]["available"])
        self.assertEqual(
            backend.preview_mints,
            [["clip_1"], ["clip_1"], ["clip_1"]],
        )

    def test_fragmentless_reload_restores_structural_pending_head_and_save_identity(
        self,
    ) -> None:
        backend = _StructuralBackend(unknown_once=True)
        handoff, origin, opener, state = self._open(backend)
        session_id = handoff["session_id"]
        state_url = f"{origin}/api/sessions/{session_id}"
        endpoint = f"{state_url}/actions"
        structural_edit = {
            "op": "split_clip",
            "clip_id": "clip_1",
            "source_split_ms": 1500,
        }
        _, staged = self._post(
            opener,
            endpoint,
            {"type": "stage_structural", "structural_edit": structural_edit},
            origin,
        )
        staged_pending = deepcopy(staged["pending"])
        staged_head = deepcopy(staged["projection"]["timeline_head"])
        self.assertEqual(staged_head, state["projection"]["timeline_head"])

        # A normal browser reload has no fragment after replaceState. The page
        # is loaded without a boot token, then its existing HttpOnly cookie
        # authenticates the read-only session GET used by boot().
        page_url = f"{origin}/canonical-editor/{session_id}"
        self.assertNotIn("#", page_url)
        with opener.open(page_url) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/html")
            self.assertIn(b"state = await jsonRequest(apiBase)", response.read())
        with opener.open(state_url) as response:
            restored = json.load(response)
        self.assertEqual(restored["pending"], staged_pending)
        self.assertEqual(restored["projection"]["timeline_head"], staged_head)

        without_cookie = build_opener()
        with self.assertRaises(HTTPError) as unauthorized:
            without_cookie.open(state_url)
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()

        # The first authority result is uncertain. Another fragmentless reload
        # must retain the exact pending/head pair so retrying Save derives the
        # same stable write identity instead of issuing a distinct mutation.
        with self.assertRaises(HTTPError) as unknown:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(unknown.exception.code, 503)
        unknown.exception.close()
        with opener.open(state_url) as response:
            retained = json.load(response)
        self.assertEqual(retained["pending"], staged_pending)
        self.assertEqual(retained["projection"]["timeline_head"], staged_head)

        _, reconciled = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertIsNone(reconciled["pending"])
        self.assertEqual(backend.saved_requests[0], backend.saved_requests[1])

    def test_structural_save_exactly_rereads_and_resigns_new_child_ids(self) -> None:
        backend = _StructuralBackend()
        handoff, origin, opener, _state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        structural_edit = {
            "op": "split_clip",
            "clip_id": "clip_1",
            "source_split_ms": 1500,
        }
        self._post(
            opener,
            endpoint,
            {"type": "stage_structural", "structural_edit": structural_edit},
            origin,
        )
        _, saved = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(
            saved["last_save"]["command_type"],
            "timeline.apply_structural_edit",
        )
        self.assertEqual(saved["projection"]["timeline_head"]["revision"], 2)
        self.assertEqual(
            backend.snapshots[1].public["timeline"]["schema_version"], "2"
        )
        # The browser receives the separately versioned, redacted timing
        # projection; it does not learn the private canonical schema contract.
        self.assertEqual(saved["projection"]["timeline"]["schema_version"], "1")
        self.assertIsNone(saved["pending"])
        child_ids = [
            clip["clip_id"]
            for clip in saved["projection"]["timeline"]["tracks"][0]["clips"]
        ]
        self.assertEqual(child_ids, ["clip_split_left_2", "clip_split_right_2"])
        self.assertEqual(
            [row["clip_id"] for row in saved["playback"]["clips"]],
            child_ids,
        )
        self.assertEqual(
            backend.preview_mints,
            [["clip_1"], ["clip_1"], child_ids],
        )
        self.assertEqual(
            backend.saved_requests,
            [
                {
                    "timeline_head": deepcopy(
                        backend.snapshots[0].expected_timeline_head
                    ),
                    "structural_edit": structural_edit,
                }
            ],
        )

    def test_unknown_structural_save_retains_pending_and_exact_write_identity(self) -> None:
        backend = _StructuralBackend(unknown_once=True)
        handoff, origin, opener, _state = self._open(backend)
        endpoint = f"{origin}/api/sessions/{handoff['session_id']}/actions"
        structural_edit = {
            "op": "split_clip",
            "clip_id": "clip_1",
            "source_split_ms": 1500,
        }
        self._post(
            opener,
            endpoint,
            {"type": "stage_structural", "structural_edit": structural_edit},
            origin,
        )
        with self.assertRaises(HTTPError) as unknown:
            self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertEqual(unknown.exception.code, 503)
        with unknown.exception as response:
            error = json.load(response)["error"]
        self.assertEqual(error["code"], "service_unavailable")
        with opener.open(
            f"{origin}/api/sessions/{handoff['session_id']}"
        ) as response:
            retained = json.load(response)
        self.assertEqual(
            retained["pending"]["structural_edit"],
            structural_edit,
        )
        self.assertIsNone(retained["last_save"])

        _, reconciled = self._post(opener, endpoint, {"type": "save"}, origin)
        self.assertIsNone(reconciled["pending"])
        self.assertEqual(
            reconciled["last_save"]["command_type"],
            "timeline.apply_structural_edit",
        )
        self.assertEqual(backend.saved_requests[0], backend.saved_requests[1])

    def test_paired_structural_backend_sends_only_closed_intent_with_stable_key(self) -> None:
        class _WriteClient:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def timeline_write(self, **kwargs):
                self.calls.append(deepcopy(kwargs))
                return {"accepted": True}

        client = _WriteClient()
        backend = PairedCanonicalEditorBackend(
            project_id="proj_canonical",
            credential={
                "database_uuid": "db-11111111-2222-4333-8444-555555555555",
                "actions": ["timeline.apply_structural_edit"],
            },
            client=client,
        )
        snapshot = _snapshot(1)
        structural_edit = {
            "op": "split_clip",
            "clip_id": "clip_1",
            "source_split_ms": 1500,
        }
        backend.save_structural_edit(
            snapshot=snapshot,
            structural_edit=structural_edit,
        )
        backend.save_structural_edit(
            snapshot=snapshot,
            structural_edit=structural_edit,
        )
        self.assertEqual(client.calls[0], client.calls[1])
        call = client.calls[0]
        self.assertEqual(call["command"], "structural_edit")
        self.assertEqual(
            set(call["body"]),
            {
                "expected_blueprint",
                "expected_coverage",
                "expected_timeline_head",
                "structural_edit",
            },
        )
        self.assertEqual(call["body"]["structural_edit"], structural_edit)
        for forbidden in ("edit", "timeline", "operations", "draft_id", "offset_ms"):
            self.assertNotIn(forbidden, call["body"])

    def test_mcp_schema_and_page_expose_canonical_b2b4c_dialect(self) -> None:
        tool = next(
            item for item in TOOLS
            if item["name"] == "memolens_canonical_editor_handoff"
        )
        self.assertEqual(set(tool["inputSchema"]["properties"]), {"project_id"})
        self.assertEqual(tool["inputSchema"]["required"], ["project_id"])
        html = (PLUGIN_ROOT / "ui" / "canonical-editor.html").read_text()
        for operation in (
            "move_clip",
            "trim_clip",
            "set_clip_duration",
            "replace_clip",
            "split_clip",
            "delete_clip",
        ):
            self.assertIn(operation, html)
        for legacy in ("remove_clip", "set_volume", "set_format", "offset_ms"):
            self.assertNotIn(legacy, html)
        self.assertIn("Split", html)
        self.assertIn("Remove clip from Timeline", html)
        self.assertIn("Original media remains unchanged", html)
        self.assertIn("source_split_ms", html)
        self.assertIn("MAX_TIMELINE_CLIPS = 256", html)
        self.assertIn("{ type: 'stage_structural', structural_edit }", html)
        self.assertIn("state = await jsonRequest(apiBase)", html)
        self.assertIn("error.status === 401 || error.status === 404", html)
        self.assertIn("{ type: 'select_revision', revision }", html)
        self.assertIn("action({ type: 'stage_restore' })", html)
        self.assertIn("Stage restore revision", html)
        self.assertIn("ui.discard.disabled = busy || pending === null", html)
        self.assertIn("ui.save.disabled = busy || pending === null", html)
        self.assertNotIn(
            "element.disabled = busy || element.disabled",
            html,
        )

    def test_visual_timeline_is_view_only_and_stages_closed_canonical_operations(self) -> None:
        html = (PLUGIN_ROOT / "ui" / "canonical-editor.html").read_text()

        for control_id in (
            'id="visual-timeline"',
            'id="timeline-ruler"',
            'id="playhead"',
            'id="timeline-zoom"',
            'id="timeline-track"',
            'id="gesture-note"',
        ):
            self.assertIn(control_id, html)
        self.assertIn("function displayedTimeline()", html)
        self.assertIn(
            "pending.preview?.object === 'canonical_timeline.pending_preview'",
            html,
        )
        self.assertIn("return pending.preview.timeline", html)
        self.assertIn("return state.projection.timeline", html)
        self.assertIn(
            "body.draggable = controlsEnabled && clips.length > 1",
            html,
        )
        self.assertIn(
            "stage({ op: 'move_clip', clip_id: proposed.clipId, to_index: proposed.toIndex })",
            html,
        )
        self.assertIn("op: 'trim_clip'", html)
        self.assertIn("op: 'set_clip_duration'", html)
        self.assertIn("handle.setPointerCapture(event.pointerId)", html)
        self.assertIn("aria-keyshortcuts", html)
        self.assertIn("Alt+Arrow reorders a focused clip", html)
        self.assertIn("server-returned pending preview", html)
        self.assertIn("Current canonical", html)
        self.assertIn("Eligible replacement", html)
        self.assertIn("replacement-previews", html)
        self.assertIn("Stage this replacement", html)
        self.assertIn(
            "stage({ op: 'replace_clip', clip_id: clip.clip_id, assignment_id: candidate.assignment_id })",
            html,
        )
        self.assertNotIn("document.createElement('select')", html)

        for second_authority in (
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "showOpenFilePicker",
            "FileReader",
        ):
            self.assertNotIn(second_authority, html)

    def test_projection_fails_closed_on_cross_resource_head_mismatch(self) -> None:
        project, coverage, timeline = _raw_snapshot(1)
        project["project"]["timeline"]["head"]["revision_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "not its head"):
            canonical_editor_snapshot(
                project_id="proj_canonical",
                credential_database_uuid="db-11111111-2222-4333-8444-555555555555",
                project_response=project,
                coverage_response=coverage,
                timeline_response=timeline,
            )


if __name__ == "__main__":
    unittest.main()
