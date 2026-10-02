from __future__ import annotations

import copy
import hashlib
import math
import unittest

from core.blueprint_contract import (
    blueprint_content_sha256,
    blueprint_semantic_sha256,
    compile_blueprint_document,
)
from core.coverage_contract import (
    canonical_sha256 as coverage_canonical_sha256,
    compile_baseline_coverage_plan,
    coverage_plan_content_sha256,
)
from core.timeline_edit_contract import (
    TimelineEditContractError,
    apply_timeline_edit,
    require_exact_timeline_edit_replay,
)
from core.timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_json,
    canonical_timeline_clip_id,
    canonical_timeline_content_sha256,
    compile_canonical_timeline,
    require_exact_canonical_timeline_derivation,
    validate_canonical_timeline,
    validate_timeline_source_bindings,
)


def _sha(material: bytes) -> str:
    return hashlib.sha256(material).hexdigest()


IMAGE_SHA = _sha(b"timeline-edit-selected-image")
ALT_IMAGE_SHA = _sha(b"timeline-edit-alternative-image")
UNRESOLVED_SHA = _sha(b"timeline-edit-unresolved-image")
VIDEO_SHA = _sha(b"timeline-edit-video")

IMAGE_ID = f"asset_{IMAGE_SHA[:24]}"
ALT_IMAGE_ID = f"asset_{ALT_IMAGE_SHA[:24]}"
UNRESOLVED_ID = f"asset_{UNRESOLVED_SHA[:24]}"
VIDEO_ID = f"asset_{VIDEO_SHA[:24]}"

IMAGE_REF = f"memolens://evidence/asset/{IMAGE_ID}"
ALT_IMAGE_REF = f"memolens://evidence/asset/{ALT_IMAGE_ID}"
UNRESOLVED_REF = f"memolens://evidence/asset/{UNRESOLVED_ID}"

SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_0"
SECOND_SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_1"
SHORT_SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_2"
SPAN_REF = f"memolens://evidence/span/{SPAN_ID}"
SECOND_SPAN_REF = f"memolens://evidence/span/{SECOND_SPAN_ID}"
SHORT_SPAN_REF = f"memolens://evidence/span/{SHORT_SPAN_ID}"

ANALYSIS_RUN_ID = f"arun_{'3' * 32}"
IMAGE_SOURCE_ID = f"src_{'1' * 24}"
ALT_IMAGE_SOURCE_ID = f"src_{'2' * 24}"
VIDEO_SOURCE_ID = f"src_{'3' * 24}"


def _image_analysis_fields(digest: str, *, revision: int = 1) -> dict[str, object]:
    return {
        "analysis_run_id": f"arun_{_sha(('image-run:' + digest).encode())[:32]}",
        "analysis_revision": revision,
        "analysis_content_sha256": _sha(
            f"image-analysis:{digest}:{revision}".encode()
        ),
        "source_binding_sha256": _sha(
            f"image-source-binding:{digest}:{revision}".encode()
        ),
    }


def _asset_proof(asset_id: str, digest: str) -> dict[str, object]:
    return {
        "kind": "asset",
        "asset_id": asset_id,
        "asset_sha256": digest,
        **_image_analysis_fields(digest),
    }


def _span_proof(
    span_id: str,
    *,
    start_ms: int,
    end_ms: int,
) -> dict[str, object]:
    return {
        "kind": "span",
        "span_id": span_id,
        "asset_id": VIDEO_ID,
        "asset_sha256": VIDEO_SHA,
        "analysis_run_id": ANALYSIS_RUN_ID,
        "analysis_revision": 1,
        "input_asset_sha256": VIDEO_SHA,
        "start_ms": start_ms,
        "end_ms": end_ms,
    }


def _semantic() -> dict[str, object]:
    return {
        "intent": {
            "goal": "Make a deterministic memory film",
            "stance": None,
            "audience": None,
            "platform": None,
        },
        "script": {
            "blocks": [
                {"block_id": "opening", "text": "Opening"},
                {"block_id": "closing", "text": "Closing"},
            ]
        },
        "direction": {
            "theme": None,
            "narrative_arc": None,
            "emotion": None,
            "tone": None,
            "pace": None,
        },
        "output": {"duration_target_ms": 8_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [
            {
                "hint_id": "hint_selected_image",
                "evidence_ref": IMAGE_REF,
                "script_block_ids": ["opening"],
                "reason": "Use the selected image first.",
            },
            {
                "hint_id": "hint_alternative_image",
                "evidence_ref": ALT_IMAGE_REF,
                "script_block_ids": ["opening"],
                "reason": "Keep a verified image alternative.",
            },
            {
                "hint_id": "hint_selected_video",
                "evidence_ref": SPAN_REF,
                "script_block_ids": ["opening", "closing"],
                "reason": "Use this video span for the closing.",
            },
            {
                "hint_id": "hint_alternative_video",
                "evidence_ref": SECOND_SPAN_REF,
                "script_block_ids": ["opening"],
                "reason": "Keep a distinct verified video alternative.",
            },
            {
                "hint_id": "hint_short_video",
                "evidence_ref": SHORT_SPAN_REF,
                "script_block_ids": ["opening"],
                "reason": "Expose a verified span that is too short.",
            },
            {
                "hint_id": "hint_unresolved",
                "evidence_ref": UNRESOLVED_REF,
                "script_block_ids": ["opening"],
                "reason": "Expose an unresolved alternative.",
            },
        ],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def _fixture() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    semantic = _semantic()
    evidence_refs = sorted(str(item["evidence_ref"]) for item in semantic["material_hints"])
    blueprint = compile_blueprint_document(
        project_id="project_timeline_edit",
        revision=1,
        parent=None,
        created_by_operation_id="op_blueprint_edit",
        semantic=semantic,
        source_candidate_sha256="a" * 64,
        initial_legacy_brief={"revision": 1, "content_sha256": "b" * 64},
        evidence_manifest=[
            {
                "evidence_ref": evidence_ref,
                "status": "verified",
                "proof_sha256": _sha(evidence_ref.encode()),
            }
            for evidence_ref in evidence_refs
        ],
    )
    plan = compile_baseline_coverage_plan(
        project_id="project_timeline_edit",
        revision=1,
        parent=None,
        blueprint=blueprint,
        blueprint_binding={
            "revision": blueprint["revision"],
            "content_sha256": blueprint_content_sha256(blueprint),
            "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
        },
        evidence_rows=[
            {"evidence_ref": IMAGE_REF, "proof": _asset_proof(IMAGE_ID, IMAGE_SHA)},
            {
                "evidence_ref": ALT_IMAGE_REF,
                "proof": _asset_proof(ALT_IMAGE_ID, ALT_IMAGE_SHA),
            },
            {
                "evidence_ref": SPAN_REF,
                "proof": _span_proof(SPAN_ID, start_ms=1_000, end_ms=9_000),
            },
            {
                "evidence_ref": SECOND_SPAN_REF,
                "proof": _span_proof(SECOND_SPAN_ID, start_ms=10_000, end_ms=16_000),
            },
            {
                "evidence_ref": SHORT_SPAN_REF,
                "proof": _span_proof(SHORT_SPAN_ID, start_ms=20_000, end_ms=20_200),
            },
            {"evidence_ref": UNRESOLVED_REF, "proof": None},
        ],
    )
    blueprint_binding = {
        "revision": blueprint["revision"],
        "content_sha256": blueprint_content_sha256(blueprint),
        "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
        "operation_id": blueprint["created_by_operation_id"],
    }
    coverage_binding = {
        "revision": plan["revision"],
        "content_sha256": coverage_plan_content_sha256(plan),
        "evidence_manifest_sha256": coverage_canonical_sha256(plan["evidence_manifest"]),
        "operation_id": "op_coverage_edit",
    }
    result = compile_canonical_timeline(
        project_id="project_timeline_edit",
        revision=1,
        parent=None,
        blueprint=blueprint,
        blueprint_binding=blueprint_binding,
        coverage_plan=plan,
        coverage_binding=coverage_binding,
        source_bindings=[
            {
                "evidence_ref": IMAGE_REF,
                "asset_id": IMAGE_ID,
                "asset_sha256": IMAGE_SHA,
                "asset_source_id": IMAGE_SOURCE_ID,
                **_image_analysis_fields(IMAGE_SHA),
            },
            {
                "evidence_ref": SPAN_REF,
                "asset_id": VIDEO_ID,
                "asset_sha256": VIDEO_SHA,
                "asset_source_id": VIDEO_SOURCE_ID,
            },
        ],
    )
    return result, blueprint, plan


def _clip(result: dict[str, object], media_kind: str) -> dict[str, object]:
    return next(
        item
        for item in result["timeline"]["tracks"][0]["clips"]
        if item["media_kind"] == media_kind
    )


def _alternative_id(plan: dict[str, object], evidence_ref: str) -> str:
    return next(
        str(item["assignment_id"])
        for item in plan["beats"][0]["alternatives"]
        if item["evidence_ref"] == evidence_ref
    )


def _replacement_source(evidence_ref: str) -> dict[str, object]:
    if evidence_ref == IMAGE_REF:
        return {
            "evidence_ref": evidence_ref,
            "asset_id": IMAGE_ID,
            "asset_sha256": IMAGE_SHA,
            "asset_source_id": IMAGE_SOURCE_ID,
            **_image_analysis_fields(IMAGE_SHA),
        }
    if evidence_ref == ALT_IMAGE_REF:
        return {
            "evidence_ref": evidence_ref,
            "asset_id": ALT_IMAGE_ID,
            "asset_sha256": ALT_IMAGE_SHA,
            "asset_source_id": ALT_IMAGE_SOURCE_ID,
            **_image_analysis_fields(ALT_IMAGE_SHA),
        }
    if evidence_ref == UNRESOLVED_REF:
        return {
            "evidence_ref": evidence_ref,
            "asset_id": UNRESOLVED_ID,
            "asset_sha256": UNRESOLVED_SHA,
            "asset_source_id": ALT_IMAGE_SOURCE_ID,
            **_image_analysis_fields(UNRESOLVED_SHA),
        }
    return {
        "evidence_ref": evidence_ref,
        "asset_id": VIDEO_ID,
        "asset_sha256": VIDEO_SHA,
        "asset_source_id": VIDEO_SOURCE_ID,
    }


class TimelineEditContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.result, self.blueprint, self.plan = _fixture()
        self.image = _clip(self.result, "image")
        self.video = _clip(self.result, "video")

    def apply(
        self,
        edit: object,
        *,
        replacement_source: object = None,
        result: dict[str, object] | None = None,
        plan: dict[str, object] | None = None,
    ) -> dict[str, object]:
        current = result or self.result
        return apply_timeline_edit(
            parent_timeline=current["timeline"],
            parent_source_bindings=current["source_bindings"],
            coverage_plan=plan or self.plan,
            edit=edit,
            replacement_source=replacement_source,
        )

    def assert_error(self, code: str, callback) -> None:
        with self.assertRaises(TimelineEditContractError) as raised:
            callback()
        self.assertEqual(raised.exception.errors[0]["code"], code)

    def test_move_clip_reflows_track_and_source_manifest(self) -> None:
        frozen_parent = copy.deepcopy(self.result)
        edited = self.apply(
            {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1}
        )
        timeline = edited["timeline"]
        clips = timeline["tracks"][0]["clips"]

        self.assertEqual(self.result, frozen_parent)
        self.assertEqual([clip["media_kind"] for clip in clips], ["video", "image"])
        self.assertEqual([clip["ordinal"] for clip in clips], [0, 1])
        self.assertEqual([clip["start_ms"] for clip in clips], [0, clips[0]["end_ms"]])
        self.assertEqual(
            [source["clip_id"] for source in edited["source_bindings"]],
            [clip["clip_id"] for clip in clips],
        )
        self.assertEqual(timeline["revision"], 2)
        self.assertEqual(
            timeline["parent"],
            {
                "revision": 1,
                "content_sha256": canonical_timeline_content_sha256(self.result["timeline"]),
            },
        )
        for field in ("blueprint_binding", "coverage_binding", "compiler"):
            self.assertEqual(timeline[field], self.result["timeline"][field])
        self.assertEqual(validate_canonical_timeline(timeline), [])
        self.assertEqual(
            validate_timeline_source_bindings(edited["source_bindings"], timeline=timeline),
            [],
        )

    def test_trim_video_stays_inside_proof_and_changes_range_bound_clip_id(self) -> None:
        edited = self.apply(
            {
                "op": "trim_clip",
                "clip_id": self.video["clip_id"],
                "source_in_ms": 2_000,
                "source_out_ms": 2_500,
            }
        )
        video = _clip(edited, "video")
        binding = next(row for row in edited["source_bindings"] if row["media_kind"] == "video")

        self.assertNotEqual(video["clip_id"], self.video["clip_id"])
        self.assertEqual((video["source_in_ms"], video["source_out_ms"]), (2_000, 2_500))
        self.assertEqual(video["end_ms"] - video["start_ms"], 500)
        self.assertEqual(binding["clip_id"], video["clip_id"])
        self.assertEqual((binding["source_in_ms"], binding["source_out_ms"]), (2_000, 2_500))
        self.assertEqual(edited["timeline"]["output"]["duration_ms"], 4_500)

    def test_set_image_duration_preserves_image_identity_and_reflows(self) -> None:
        edited = self.apply(
            {
                "op": "set_clip_duration",
                "clip_id": self.image["clip_id"],
                "duration_ms": 1_250,
            }
        )
        clips = edited["timeline"]["tracks"][0]["clips"]
        self.assertEqual(clips[0]["clip_id"], self.image["clip_id"])
        self.assertEqual(clips[0]["end_ms"] - clips[0]["start_ms"], 1_250)
        self.assertEqual(clips[1]["start_ms"], 1_250)
        self.assertEqual(edited["timeline"]["output"]["duration_ms"], 5_250)

    def test_replace_with_verified_image_alternative_preserves_slot_and_beat(self) -> None:
        edited = self.apply(
            {
                "op": "replace_clip",
                "clip_id": self.image["clip_id"],
                "assignment_id": _alternative_id(self.plan, ALT_IMAGE_REF),
            },
            replacement_source=_replacement_source(ALT_IMAGE_REF),
        )
        replacement = edited["timeline"]["tracks"][0]["clips"][0]
        source = edited["source_bindings"][0]

        self.assertEqual(replacement["beat_id"], self.image["beat_id"])
        self.assertEqual(replacement["evidence_ref"], ALT_IMAGE_REF)
        self.assertEqual(replacement["media_kind"], "image")
        self.assertEqual(
            replacement["end_ms"] - replacement["start_ms"],
            self.image["end_ms"] - self.image["start_ms"],
        )
        self.assertEqual(source["asset_source_id"], ALT_IMAGE_SOURCE_ID)
        for field, value in _image_analysis_fields(ALT_IMAGE_SHA).items():
            self.assertEqual(replacement[field], value)
            self.assertEqual(source[field], value)
        self.assertNotIn("path", canonical_json(edited).casefold())

    def test_replace_with_verified_video_alternative_uses_bounded_slot(self) -> None:
        edited = self.apply(
            {
                "op": "replace_clip",
                "clip_id": self.image["clip_id"],
                "assignment_id": _alternative_id(self.plan, SECOND_SPAN_REF),
            },
            replacement_source=_replacement_source(SECOND_SPAN_REF),
        )
        replacement = edited["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(replacement["media_kind"], "video")
        self.assertEqual(replacement["span_id"], SECOND_SPAN_ID)
        self.assertEqual(
            (replacement["source_in_ms"], replacement["source_out_ms"]),
            (10_000, 14_000),
        )
        self.assertEqual(edited["timeline"]["output"]["duration_ms"], 8_000)

    def test_deterministic_replay_and_cold_audit_cover_timeline_and_sources(self) -> None:
        edit = {
            "op": "replace_clip",
            "clip_id": self.image["clip_id"],
            "assignment_id": _alternative_id(self.plan, ALT_IMAGE_REF),
        }
        source = _replacement_source(ALT_IMAGE_REF)
        first = self.apply(edit, replacement_source=source)
        second = self.apply(copy.deepcopy(edit), replacement_source=copy.deepcopy(source))
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertIs(
            require_exact_timeline_edit_replay(
                first["timeline"],
                source_bindings=first["source_bindings"],
                parent_timeline=self.result["timeline"],
                parent_source_bindings=self.result["source_bindings"],
                coverage_plan=self.plan,
                edit=edit,
                replacement_source=source,
            ),
            first["timeline"],
        )

        tampered_sources = copy.deepcopy(first["source_bindings"])
        tampered_sources[0]["asset_source_id"] = f"src_{'9' * 24}"
        self.assert_error(
            "timeline_edit_replay_mismatch",
            lambda: require_exact_timeline_edit_replay(
                first["timeline"],
                source_bindings=tampered_sources,
                parent_timeline=self.result["timeline"],
                parent_source_bindings=self.result["source_bindings"],
                coverage_plan=self.plan,
                edit=edit,
                replacement_source=source,
            ),
        )

    def test_manual_revision_uses_edit_replay_not_exact_first_cut_claim(self) -> None:
        edit = {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1}
        moved = self.apply(edit)
        self.assertIs(
            require_exact_timeline_edit_replay(
                moved["timeline"],
                source_bindings=moved["source_bindings"],
                parent_timeline=self.result["timeline"],
                parent_source_bindings=self.result["source_bindings"],
                coverage_plan=self.plan,
                edit=edit,
            ),
            moved["timeline"],
        )
        with self.assertRaises(TimelineLoweringContractError):
            require_exact_canonical_timeline_derivation(
                moved["timeline"],
                source_manifest=moved["source_bindings"],
                blueprint=self.blueprint,
                blueprint_binding=self.result["timeline"]["blueprint_binding"],
                coverage_plan=self.plan,
                coverage_binding=self.result["timeline"]["coverage_binding"],
            )

    def test_chained_edit_accepts_exact_manual_parent(self) -> None:
        trimmed = self.apply(
            {
                "op": "trim_clip",
                "clip_id": self.video["clip_id"],
                "source_in_ms": 2_000,
                "source_out_ms": 2_600,
            }
        )
        trimmed_video = _clip(trimmed, "video")
        moved = self.apply(
            {"op": "move_clip", "clip_id": trimmed_video["clip_id"], "to_index": 0},
            result=trimmed,
        )
        self.assertEqual(moved["timeline"]["revision"], 3)
        self.assertEqual(
            moved["timeline"]["parent"],
            {
                "revision": 2,
                "content_sha256": canonical_timeline_content_sha256(trimmed["timeline"]),
            },
        )
        self.assertEqual(moved["timeline"]["tracks"][0]["clips"][0]["clip_id"], trimmed_video["clip_id"])

    def test_edit_objects_are_closed_and_clip_must_exist(self) -> None:
        invalid_cases = [
            (
                "invalid_edit",
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1, "extra": True},
            ),
            ("unsupported_edit", {"op": "delete_clip", "clip_id": self.image["clip_id"]}),
            ("invalid_edit", ["move_clip"]),
            ("unknown_clip", {"op": "move_clip", "clip_id": f"clip_{'f' * 24}", "to_index": 0}),
        ]
        for code, edit in invalid_cases:
            with self.subTest(code=code, edit=edit):
                self.assert_error(code, lambda edit=edit: self.apply(edit))
        self.assert_error(
            "unexpected_replacement_source",
            lambda: self.apply(
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1},
                replacement_source=_replacement_source(ALT_IMAGE_REF),
            ),
        )

    def test_numeric_edit_fields_reject_bool_float_nan_and_index_bounds(self) -> None:
        for invalid in (True, 1.0, math.nan, -1, 2):
            with self.subTest(to_index=invalid):
                self.assert_error(
                    "invalid_edit_integer",
                    lambda invalid=invalid: self.apply(
                        {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": invalid}
                    ),
                )
        for invalid in (True, 1.5, math.nan, -1):
            with self.subTest(duration_ms=invalid):
                self.assert_error(
                    "invalid_edit_integer",
                    lambda invalid=invalid: self.apply(
                        {
                            "op": "set_clip_duration",
                            "clip_id": self.image["clip_id"],
                            "duration_ms": invalid,
                        }
                    ),
                )

    def test_trim_rejects_image_short_reverse_and_out_of_proof_ranges(self) -> None:
        cases = [
            (
                "invalid_trim_target",
                {"op": "trim_clip", "clip_id": self.image["clip_id"], "source_in_ms": 0, "source_out_ms": 100},
            ),
            (
                "trim_too_short",
                {"op": "trim_clip", "clip_id": self.video["clip_id"], "source_in_ms": 2_000, "source_out_ms": 2_099},
            ),
            (
                "trim_too_short",
                {"op": "trim_clip", "clip_id": self.video["clip_id"], "source_in_ms": 2_000, "source_out_ms": 1_900},
            ),
            (
                "trim_outside_coverage_proof",
                {"op": "trim_clip", "clip_id": self.video["clip_id"], "source_in_ms": 999, "source_out_ms": 1_500},
            ),
            (
                "trim_outside_coverage_proof",
                {"op": "trim_clip", "clip_id": self.video["clip_id"], "source_in_ms": 8_500, "source_out_ms": 9_001},
            ),
        ]
        for code, edit in cases:
            with self.subTest(code=code):
                self.assert_error(code, lambda edit=edit: self.apply(edit))

    def test_set_duration_rejects_video_and_total_duration_limit(self) -> None:
        self.assert_error(
            "invalid_duration_target",
            lambda: self.apply(
                {
                    "op": "set_clip_duration",
                    "clip_id": self.video["clip_id"],
                    "duration_ms": 500,
                }
            ),
        )
        self.assert_error(
            "timeline_duration_limit_exceeded",
            lambda: self.apply(
                {
                    "op": "set_clip_duration",
                    "clip_id": self.image["clip_id"],
                    "duration_ms": 1_800_000,
                }
            ),
        )

    def test_replace_rejects_non_alternative_unverified_short_and_duplicate_evidence(self) -> None:
        selected_assignment = self.plan["beats"][0]["selected_assignments"][0]["assignment_id"]
        cases = [
            (
                "invalid_replacement_candidate",
                selected_assignment,
                _replacement_source(IMAGE_REF),
            ),
            (
                "unverified_replacement",
                _alternative_id(self.plan, UNRESOLVED_REF),
                _replacement_source(UNRESOLVED_REF),
            ),
            (
                "replacement_slot_unsatisfied",
                _alternative_id(self.plan, SHORT_SPAN_REF),
                _replacement_source(SHORT_SPAN_REF),
            ),
            (
                "duplicate_replacement_evidence",
                _alternative_id(self.plan, SPAN_REF),
                _replacement_source(SPAN_REF),
            ),
        ]
        for code, assignment_id, source in cases:
            with self.subTest(code=code):
                self.assert_error(
                    code,
                    lambda assignment_id=assignment_id, source=source: self.apply(
                        {
                            "op": "replace_clip",
                            "clip_id": self.image["clip_id"],
                            "assignment_id": assignment_id,
                        },
                        replacement_source=source,
                    ),
                )

    def test_replacement_source_is_required_closed_path_free_and_proof_exact(self) -> None:
        edit = {
            "op": "replace_clip",
            "clip_id": self.image["clip_id"],
            "assignment_id": _alternative_id(self.plan, ALT_IMAGE_REF),
        }
        self.assert_error("invalid_replacement_source", lambda: self.apply(edit))

        with_path = _replacement_source(ALT_IMAGE_REF)
        with_path["path"] = "/private/library/image.jpg"
        self.assert_error(
            "invalid_replacement_source",
            lambda: self.apply(edit, replacement_source=with_path),
        )

        path_as_source_id = _replacement_source(ALT_IMAGE_REF)
        path_as_source_id["asset_source_id"] = "/private/library/image.jpg"
        self.assert_error(
            "invalid_replacement_source",
            lambda: self.apply(edit, replacement_source=path_as_source_id),
        )

        wrong_asset = _replacement_source(ALT_IMAGE_REF)
        wrong_asset.update(asset_id=IMAGE_ID, asset_sha256=IMAGE_SHA)
        self.assert_error(
            "replacement_source_mismatch",
            lambda: self.apply(edit, replacement_source=wrong_asset),
        )

        contradictory_revision = _replacement_source(ALT_IMAGE_REF)
        contradictory_revision["analysis_revision"] = 2
        self.assert_error(
            "replacement_source_mismatch",
            lambda: self.apply(edit, replacement_source=contradictory_revision),
        )

        missing_binding = _replacement_source(ALT_IMAGE_REF)
        del missing_binding["source_binding_sha256"]
        self.assert_error(
            "invalid_replacement_source",
            lambda: self.apply(edit, replacement_source=missing_binding),
        )

    def test_legacy_identity_only_parent_cannot_be_edited(self) -> None:
        legacy = copy.deepcopy(self.result)
        image = _clip(legacy, "image")
        source = next(
            row for row in legacy["source_bindings"] if row["media_kind"] == "image"
        )
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            image.pop(field)
            source.pop(field)
        image["clip_id"] = canonical_timeline_clip_id(
            timeline_id=legacy["timeline"]["timeline_id"],
            beat_id=image["beat_id"],
            assignment_id=image["assignment_id"],
            evidence_ref=image["evidence_ref"],
            asset_id=image["asset_id"],
            asset_sha256=image["asset_sha256"],
            media_kind="image",
        )
        source["clip_id"] = image["clip_id"]

        self.assert_error(
            "invalid_parent_timeline",
            lambda: self.apply(
                {"op": "set_clip_duration", "clip_id": image["clip_id"], "duration_ms": 2_000},
                result=legacy,
            ),
        )

    def test_exact_parent_coverage_and_proof_bindings_are_required(self) -> None:
        forged_binding = copy.deepcopy(self.result)
        forged_binding["timeline"]["coverage_binding"]["content_sha256"] = "f" * 64
        self.assert_error(
            "coverage_binding_mismatch",
            lambda: self.apply(
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1},
                result=forged_binding,
            ),
        )

        forged_proof = copy.deepcopy(self.result)
        forged_proof["source_bindings"][0]["coverage_proof_sha256"] = "f" * 64
        self.assertEqual(
            validate_timeline_source_bindings(
                forged_proof["source_bindings"], timeline=forged_proof["timeline"]
            ),
            [],
        )
        self.assert_error(
            "coverage_proof_mismatch",
            lambda: self.apply(
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1},
                result=forged_proof,
            ),
        )

        leaked_parent = copy.deepcopy(self.result)
        leaked_parent["timeline"]["database_path"] = "/private/library.sqlite3"
        self.assert_error(
            "invalid_parent_timeline",
            lambda: self.apply(
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1},
                result=leaked_parent,
            ),
        )

    def test_revision_limit_fails_closed(self) -> None:
        at_limit = copy.deepcopy(self.result)
        at_limit["timeline"]["revision"] = 1_800_000
        at_limit["timeline"]["parent"] = {
            "revision": 1_799_999,
            "content_sha256": "a" * 64,
        }
        self.assertEqual(validate_canonical_timeline(at_limit["timeline"]), [])
        self.assert_error(
            "revision_limit_exceeded",
            lambda: self.apply(
                {"op": "move_clip", "clip_id": self.image["clip_id"], "to_index": 1},
                result=at_limit,
            ),
        )


if __name__ == "__main__":
    unittest.main()
