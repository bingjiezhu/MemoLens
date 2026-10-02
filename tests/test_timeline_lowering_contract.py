from __future__ import annotations

import copy
import hashlib
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
from core.timeline_lowering_contract import (
    TIMELINE_LOWERER_COMPILER,
    TimelineLoweringContractError,
    canonical_json,
    canonical_timeline_clip_id,
    canonical_timeline_content_sha256,
    compile_canonical_timeline,
    inspect_canonical_timeline_for_read_only,
    inspect_timeline_source_bindings_for_read_only,
    require_current_canonical_timeline,
    require_current_timeline_source_bindings,
    require_exact_canonical_timeline_derivation,
    timeline_source_bindings_sha256,
    validate_canonical_timeline,
    validate_timeline_source_bindings,
)


IMAGE_SHA = hashlib.sha256(b"timeline-image").hexdigest()
VIDEO_SHA = hashlib.sha256(b"timeline-video").hexdigest()
EXTRA_SHA = hashlib.sha256(b"timeline-extra").hexdigest()
IMAGE_ID = f"asset_{IMAGE_SHA[:24]}"
VIDEO_ID = f"asset_{VIDEO_SHA[:24]}"
EXTRA_ID = f"asset_{EXTRA_SHA[:24]}"
IMAGE_REF = f"memolens://evidence/asset/{IMAGE_ID}"
SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_0"
SPAN_REF = f"memolens://evidence/span/{SPAN_ID}"
SECOND_SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_1"
SECOND_SPAN_REF = f"memolens://evidence/span/{SECOND_SPAN_ID}"
EXTRA_REF = f"memolens://evidence/asset/{EXTRA_ID}"
ANALYSIS_RUN_ID = f"arun_{'3' * 32}"
IMAGE_ANALYSIS_RUN_ID = f"arun_{'4' * 32}"
IMAGE_ANALYSIS_CONTENT_SHA = hashlib.sha256(b"timeline-image-analysis").hexdigest()
IMAGE_SOURCE_BINDING_SHA = hashlib.sha256(b"timeline-image-source-binding").hexdigest()
IMAGE_SOURCE_ID = f"src_{'1' * 24}"
VIDEO_SOURCE_ID = f"src_{'2' * 24}"


def semantic(
    *,
    with_span: bool = True,
    with_gap: bool = False,
    aspect_ratio: str | None = "9:16",
) -> dict[str, object]:
    hints: list[dict[str, object]] = [
        {
            "hint_id": "hint_image",
            "evidence_ref": IMAGE_REF,
            "script_block_ids": ["opening"],
            "reason": "Use the pinned image for the opening.",
        }
    ]
    if with_span and not with_gap:
        hints.append(
            {
                "hint_id": "hint_span",
                "evidence_ref": SPAN_REF,
                "script_block_ids": ["ending"],
                "reason": "Use the bounded verified video span.",
            }
        )
    return {
        "intent": {"goal": "Make a small memory film", "stance": None, "audience": None, "platform": None},
        "script": {
            "blocks": [
                {"block_id": "opening", "text": "First memory"},
                {"block_id": "ending", "text": "Last memory"},
            ]
        },
        "direction": {
            "theme": None,
            "narrative_arc": None,
            "emotion": None,
            "tone": None,
            "pace": None,
        },
        "output": {"duration_target_ms": 8_000, "aspect_ratio": aspect_ratio},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": hints,
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def compile_proposed_blueprint(proposed: dict[str, object]) -> dict[str, object]:
    evidence_refs = sorted(str(item["evidence_ref"]) for item in proposed["material_hints"])
    return compile_blueprint_document(
        project_id="project_timeline",
        revision=1,
        parent=None,
        created_by_operation_id="op_blueprint_one",
        semantic=proposed,
        source_candidate_sha256="a" * 64,
        initial_legacy_brief={"revision": 1, "content_sha256": "b" * 64},
        evidence_manifest=[
            {
                "evidence_ref": evidence_ref,
                "status": "verified",
                "proof_sha256": hashlib.sha256(evidence_ref.encode()).hexdigest(),
            }
            for evidence_ref in evidence_refs
        ],
    )


def make_blueprint(
    *,
    with_span: bool = True,
    with_gap: bool = False,
    aspect_ratio: str | None = "9:16",
) -> dict[str, object]:
    return compile_proposed_blueprint(
        semantic(
            with_span=with_span,
            with_gap=with_gap,
            aspect_ratio=aspect_ratio,
        )
    )


def evidence_rows(*, with_span: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = [
        {
            "evidence_ref": IMAGE_REF,
            "proof": {
                "kind": "asset",
                "asset_id": IMAGE_ID,
                "asset_sha256": IMAGE_SHA,
                "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
                "analysis_revision": 1,
                "analysis_content_sha256": IMAGE_ANALYSIS_CONTENT_SHA,
                "source_binding_sha256": IMAGE_SOURCE_BINDING_SHA,
            },
        }
    ]
    if with_span:
        rows.append(
            {
                "evidence_ref": SPAN_REF,
                "proof": {
                    "kind": "span",
                    "span_id": SPAN_ID,
                    "asset_id": VIDEO_ID,
                    "asset_sha256": VIDEO_SHA,
                    "analysis_run_id": ANALYSIS_RUN_ID,
                    "analysis_revision": 1,
                    "input_asset_sha256": VIDEO_SHA,
                    "start_ms": 1_000,
                    "end_ms": 7_000,
                },
            }
        )
    return rows


def make_plan(
    blueprint: dict[str, object],
    *,
    with_span: bool = True,
) -> dict[str, object]:
    return compile_baseline_coverage_plan(
        project_id="project_timeline",
        revision=1,
        parent=None,
        blueprint=blueprint,
        blueprint_binding={
            "revision": 1,
            "content_sha256": blueprint_content_sha256(blueprint),
            "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
        },
        evidence_rows=evidence_rows(with_span=with_span),
    )


def blueprint_binding(blueprint: dict[str, object]) -> dict[str, object]:
    return {
        "revision": blueprint["revision"],
        "content_sha256": blueprint_content_sha256(blueprint),
        "semantic_sha256": blueprint_semantic_sha256(blueprint["semantic"]),
        "operation_id": blueprint["created_by_operation_id"],
    }


def coverage_binding(plan: dict[str, object]) -> dict[str, object]:
    return {
        "revision": plan["revision"],
        "content_sha256": coverage_plan_content_sha256(plan),
        "evidence_manifest_sha256": coverage_canonical_sha256(plan["evidence_manifest"]),
        "operation_id": "op_coverage_one",
    }


def source_rows(
    *,
    image_source_id: str = IMAGE_SOURCE_ID,
    video_source_id: str = VIDEO_SOURCE_ID,
    image_analysis_revision: int = 1,
    image_analysis_content_sha256: str = IMAGE_ANALYSIS_CONTENT_SHA,
    image_source_binding_sha256: str = IMAGE_SOURCE_BINDING_SHA,
    reverse: bool = False,
) -> list[dict[str, object]]:
    rows = [
        {
            "evidence_ref": IMAGE_REF,
            "asset_id": IMAGE_ID,
            "asset_sha256": IMAGE_SHA,
            "asset_source_id": image_source_id,
            "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
            "analysis_revision": image_analysis_revision,
            "analysis_content_sha256": image_analysis_content_sha256,
            "source_binding_sha256": image_source_binding_sha256,
        },
        {
            "evidence_ref": SPAN_REF,
            "asset_id": VIDEO_ID,
            "asset_sha256": VIDEO_SHA,
            "asset_source_id": video_source_id,
        },
    ]
    return list(reversed(rows)) if reverse else rows


def compile_result(
    *,
    blueprint: dict[str, object] | None = None,
    plan: dict[str, object] | None = None,
    sources: list[dict[str, object]] | None = None,
    current_blueprint_binding: dict[str, object] | None = None,
    current_coverage_binding: dict[str, object] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    blueprint_document = blueprint or make_blueprint()
    coverage_plan = plan or make_plan(blueprint_document)
    result = compile_canonical_timeline(
        project_id="project_timeline",
        revision=1,
        parent=None,
        blueprint=blueprint_document,
        blueprint_binding=current_blueprint_binding or blueprint_binding(blueprint_document),
        coverage_plan=coverage_plan,
        coverage_binding=current_coverage_binding or coverage_binding(coverage_plan),
        source_bindings=sources or source_rows(),
    )
    return result, blueprint_document, coverage_plan


class TimelineLoweringContractTests(unittest.TestCase):
    def test_compiles_one_zero_gap_primary_visual_track(self) -> None:
        result, _blueprint, _plan = compile_result()
        timeline = result["timeline"]
        clips = timeline["tracks"][0]["clips"]

        self.assertEqual(timeline["compiler"]["id"], TIMELINE_LOWERER_COMPILER)
        self.assertEqual(timeline["compiler"]["transitions"], "hard_cut")
        self.assertEqual(timeline["compiler"]["audio"], "silent")
        self.assertEqual(timeline["compiler"]["subtitles"], "none")
        self.assertEqual(len(timeline["tracks"]), 1)
        self.assertEqual([clip["media_kind"] for clip in clips], ["image", "video"])
        self.assertEqual([clip["start_ms"] for clip in clips], [0, clips[0]["end_ms"]])
        self.assertEqual(clips[-1]["end_ms"], 8_000)
        self.assertTrue(all(clip["audio_enabled"] is False for clip in clips))
        self.assertEqual(validate_canonical_timeline(timeline), [])
        self.assertEqual(
            validate_timeline_source_bindings(result["source_bindings"], timeline=timeline),
            [],
        )

    def test_image_clip_and_source_preserve_exact_analysis_evidence(self) -> None:
        result, _blueprint, _plan = compile_result()
        clip = result["timeline"]["tracks"][0]["clips"][0]
        binding = result["source_bindings"][0]
        expected = {
            "analysis_run_id": IMAGE_ANALYSIS_RUN_ID,
            "analysis_revision": 1,
            "analysis_content_sha256": IMAGE_ANALYSIS_CONTENT_SHA,
            "source_binding_sha256": IMAGE_SOURCE_BINDING_SHA,
        }
        for field, value in expected.items():
            self.assertEqual(clip[field], value)
            self.assertEqual(binding[field], value)
        self.assertEqual(binding["asset_source_id"], IMAGE_SOURCE_ID)

    def test_image_revision_changes_clip_identity_and_requires_exact_result_bound_source(self) -> None:
        first, blueprint_document, plan = compile_result()
        first_image = first["timeline"]["tracks"][0]["clips"][0]

        revised_plan = copy.deepcopy(plan)
        image_evidence = next(
            item
            for item in revised_plan["evidence_manifest"]
            if item["evidence_ref"] == IMAGE_REF
        )
        revised_content = hashlib.sha256(b"timeline-image-analysis-revision-2").hexdigest()
        revised_source_binding = hashlib.sha256(
            b"timeline-image-source-binding-revision-2"
        ).hexdigest()
        image_evidence["proof"].update(
            analysis_revision=2,
            analysis_content_sha256=revised_content,
            source_binding_sha256=revised_source_binding,
        )
        image_evidence["proof_sha256"] = coverage_canonical_sha256(image_evidence["proof"])
        second, _blueprint, _plan = compile_result(
            blueprint=blueprint_document,
            plan=revised_plan,
            sources=source_rows(
                image_analysis_revision=2,
                image_analysis_content_sha256=revised_content,
                image_source_binding_sha256=revised_source_binding,
            ),
        )
        second_image = second["timeline"]["tracks"][0]["clips"][0]
        self.assertNotEqual(first_image["clip_id"], second_image["clip_id"])

        mismatched = source_rows(image_analysis_revision=2)
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=mismatched)
        self.assertEqual(raised.exception.errors[0]["code"], "source_proof_mismatch")

        missing = source_rows()
        del missing[0]["source_binding_sha256"]
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=missing)
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_source_binding")

    def test_legacy_identity_only_timeline_is_inspectable_but_not_current(self) -> None:
        result, blueprint_document, plan = compile_result()
        legacy = copy.deepcopy(result)
        clip = legacy["timeline"]["tracks"][0]["clips"][0]
        source = legacy["source_bindings"][0]
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            clip.pop(field)
            source.pop(field)
        clip["clip_id"] = canonical_timeline_clip_id(
            timeline_id=legacy["timeline"]["timeline_id"],
            beat_id=clip["beat_id"],
            assignment_id=clip["assignment_id"],
            evidence_ref=clip["evidence_ref"],
            asset_id=clip["asset_id"],
            asset_sha256=clip["asset_sha256"],
            media_kind="image",
        )
        source["clip_id"] = clip["clip_id"]

        self.assertEqual(validate_canonical_timeline(legacy["timeline"]), [])
        self.assertEqual(
            validate_timeline_source_bindings(
                legacy["source_bindings"], timeline=legacy["timeline"]
            ),
            [],
        )
        timeline_inspection = inspect_canonical_timeline_for_read_only(
            legacy["timeline"]
        )
        source_inspection = inspect_timeline_source_bindings_for_read_only(
            legacy["source_bindings"], timeline=legacy["timeline"]
        )
        self.assertIs(timeline_inspection["document"], legacy["timeline"])
        self.assertEqual(timeline_inspection["evidence_currency"], "legacy_non_current")
        self.assertFalse(timeline_inspection["eligible_for_current_use"])
        self.assertEqual(source_inspection["evidence_currency"], "legacy_non_current")
        with self.assertRaises(TimelineLoweringContractError) as timeline_error:
            require_current_canonical_timeline(legacy["timeline"])
        self.assertEqual(
            timeline_error.exception.errors[0]["code"],
            "legacy_image_evidence_non_current",
        )
        with self.assertRaises(TimelineLoweringContractError):
            require_current_timeline_source_bindings(
                legacy["source_bindings"], timeline=legacy["timeline"]
            )
        with self.assertRaises(TimelineLoweringContractError):
            require_exact_canonical_timeline_derivation(
                legacy["timeline"],
                source_manifest=legacy["source_bindings"],
                blueprint=blueprint_document,
                blueprint_binding=blueprint_binding(blueprint_document),
                coverage_plan=plan,
                coverage_binding=coverage_binding(plan),
            )

        partial = copy.deepcopy(result["timeline"])
        del partial["tracks"][0]["clips"][0]["analysis_content_sha256"]
        codes = {item["code"] for item in validate_canonical_timeline(partial)}
        self.assertIn("required_field_missing", codes)

    def test_video_span_is_bounded_and_carries_analysis_lineage(self) -> None:
        result, _blueprint, plan = compile_result()
        clip = result["timeline"]["tracks"][0]["clips"][1]
        binding = result["source_bindings"][1]
        beat_duration = plan["beats"][1]["timing"]["end_ms"] - plan["beats"][1]["timing"]["start_ms"]

        self.assertEqual(clip["span_id"], SPAN_ID)
        self.assertEqual(clip["analysis_run_id"], ANALYSIS_RUN_ID)
        self.assertEqual(clip["analysis_revision"], 1)
        self.assertEqual(clip["input_asset_sha256"], VIDEO_SHA)
        self.assertEqual(clip["source_in_ms"], 1_000)
        self.assertEqual(clip["source_out_ms"], 1_000 + beat_duration)
        for field in (
            "span_id",
            "analysis_run_id",
            "analysis_revision",
            "input_asset_sha256",
            "source_in_ms",
            "source_out_ms",
        ):
            self.assertEqual(binding[field], clip[field])

    def test_distinct_spans_from_one_video_share_one_fixed_source(self) -> None:
        proposed = semantic()
        proposed["material_hints"] = [
            {
                "hint_id": "hint_span_opening",
                "evidence_ref": SPAN_REF,
                "script_block_ids": ["opening"],
                "reason": "Use the first verified range from the video.",
            },
            {
                "hint_id": "hint_span_ending",
                "evidence_ref": SECOND_SPAN_REF,
                "script_block_ids": ["ending"],
                "reason": "Use a distinct verified range from the same video.",
            },
        ]
        blueprint_document = compile_proposed_blueprint(proposed)
        span_proofs = [
            {
                "evidence_ref": evidence_ref,
                "proof": {
                    "kind": "span",
                    "span_id": span_id,
                    "asset_id": VIDEO_ID,
                    "asset_sha256": VIDEO_SHA,
                    "analysis_run_id": ANALYSIS_RUN_ID,
                    "analysis_revision": 1,
                    "input_asset_sha256": VIDEO_SHA,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                },
            }
            for evidence_ref, span_id, start_ms, end_ms in (
                (SPAN_REF, SPAN_ID, 1_000, 7_000),
                (SECOND_SPAN_REF, SECOND_SPAN_ID, 7_000, 13_000),
            )
        ]
        plan = compile_baseline_coverage_plan(
            project_id="project_timeline",
            revision=1,
            parent=None,
            blueprint=blueprint_document,
            blueprint_binding={
                "revision": 1,
                "content_sha256": blueprint_content_sha256(blueprint_document),
                "semantic_sha256": blueprint_semantic_sha256(blueprint_document["semantic"]),
            },
            evidence_rows=span_proofs,
        )
        shared_source_rows = [
            {
                "evidence_ref": evidence_ref,
                "asset_id": VIDEO_ID,
                "asset_sha256": VIDEO_SHA,
                "asset_source_id": VIDEO_SOURCE_ID,
            }
            for evidence_ref in (SPAN_REF, SECOND_SPAN_REF)
        ]

        result, _blueprint, _plan = compile_result(
            blueprint=blueprint_document,
            plan=plan,
            sources=shared_source_rows,
        )

        self.assertEqual(
            [row["asset_source_id"] for row in result["source_bindings"]],
            [VIDEO_SOURCE_ID, VIDEO_SOURCE_ID],
        )
        self.assertEqual(
            validate_timeline_source_bindings(
                result["source_bindings"],
                timeline=result["timeline"],
            ),
            [],
        )

    def test_timeline_bytes_ignore_clock_database_path_source_id_and_input_order(self) -> None:
        first, _blueprint, _plan = compile_result(sources=source_rows())
        relocated_sources = source_rows(
            image_source_id=f"src_{'8' * 24}",
            video_source_id=f"src_{'9' * 24}",
            reverse=True,
        )
        second, _blueprint, _plan = compile_result(sources=relocated_sources)

        first_bytes = canonical_json(first["timeline"]).encode("utf-8")
        second_bytes = canonical_json(second["timeline"]).encode("utf-8")
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(
            canonical_timeline_content_sha256(first["timeline"]),
            canonical_timeline_content_sha256(second["timeline"]),
        )
        self.assertNotEqual(
            timeline_source_bindings_sha256(first["source_bindings"], timeline=first["timeline"]),
            timeline_source_bindings_sha256(second["source_bindings"], timeline=second["timeline"]),
        )
        encoded = first_bytes.decode().casefold()
        for forbidden in ("asset_source_id", "created_at", "timestamp", "/users/", "/tmp/"):
            self.assertNotIn(forbidden, encoded)

    def test_gap_fails_closed(self) -> None:
        blueprint_document = make_blueprint(with_span=False, with_gap=True)
        plan = make_plan(blueprint_document, with_span=False)
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_canonical_timeline(
                project_id="project_timeline",
                revision=1,
                parent=None,
                blueprint=blueprint_document,
                blueprint_binding=blueprint_binding(blueprint_document),
                coverage_plan=plan,
                coverage_binding=coverage_binding(plan),
                source_bindings=source_rows()[:1],
            )
        self.assertEqual(raised.exception.errors[0]["code"], "coverage_gap")

    def test_unresolved_unselected_alternative_does_not_become_a_hidden_gap(self) -> None:
        proposed = semantic()
        proposed["material_hints"].append(
            {
                "hint_id": "hint_unresolved_alternative",
                "evidence_ref": EXTRA_REF,
                "script_block_ids": ["opening"],
                "reason": "Keep this unavailable alternative visible in Coverage only.",
            }
        )
        blueprint_document = compile_proposed_blueprint(proposed)
        rows = evidence_rows()
        rows.append({"evidence_ref": EXTRA_REF, "proof": None})
        plan = compile_baseline_coverage_plan(
            project_id="project_timeline",
            revision=1,
            parent=None,
            blueprint=blueprint_document,
            blueprint_binding={
                "revision": 1,
                "content_sha256": blueprint_content_sha256(blueprint_document),
                "semantic_sha256": blueprint_semantic_sha256(blueprint_document["semantic"]),
            },
            evidence_rows=rows,
        )

        result, _blueprint, _plan = compile_result(
            blueprint=blueprint_document,
            plan=plan,
        )
        self.assertEqual(len(result["timeline"]["tracks"][0]["clips"]), 2)

    def test_non_current_or_inexact_binding_fails_closed(self) -> None:
        blueprint_document = make_blueprint()
        plan = make_plan(blueprint_document)
        stale = coverage_binding(plan)
        stale["content_sha256"] = "f" * 64

        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(
                blueprint=blueprint_document,
                plan=plan,
                current_coverage_binding=stale,
            )
        self.assertEqual(raised.exception.errors[0]["code"], "coverage_binding_mismatch")

    def test_implicit_or_unsupported_output_shape_fails_closed(self) -> None:
        blueprint_document = make_blueprint(aspect_ratio=None)
        plan = make_plan(blueprint_document)

        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(blueprint=blueprint_document, plan=plan)
        self.assertEqual(raised.exception.errors[0]["code"], "unsupported_aspect_ratio")

    def test_source_binding_requires_exact_proof_and_exact_selected_set(self) -> None:
        mismatched = source_rows()
        mismatched[0]["asset_sha256"] = VIDEO_SHA
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=mismatched)
        self.assertEqual(raised.exception.errors[0]["code"], "source_asset_mismatch")

        conflicting_identity = source_rows(video_source_id=IMAGE_SOURCE_ID)
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=conflicting_identity)
        self.assertEqual(raised.exception.errors[0]["code"], "source_asset_mismatch")

        extra = source_rows()
        extra.append(
            {
                "evidence_ref": f"memolens://evidence/asset/asset_{'a' * 24}",
                "asset_id": f"asset_{'a' * 24}",
                "asset_sha256": "a" * 64,
                "asset_source_id": f"src_{'a' * 24}",
                "analysis_run_id": f"arun_{'a' * 32}",
                "analysis_revision": 1,
                "analysis_content_sha256": "b" * 64,
                "source_binding_sha256": "c" * 64,
            }
        )
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=extra)
        self.assertEqual(raised.exception.errors[0]["code"], "source_binding_set_mismatch")

    def test_path_shaped_or_extra_source_fields_fail_closed(self) -> None:
        path_source = source_rows()
        path_source[0]["asset_source_id"] = "/Users/example/private.jpg"
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=path_source)
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_asset_source_id")

        extra_field = source_rows()
        extra_field[0]["path"] = "/tmp/private.jpg"
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(sources=extra_field)
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_source_binding")

    def test_unknown_timeline_fields_and_whole_video_shape_are_rejected(self) -> None:
        result, _blueprint, _plan = compile_result()
        with_path = copy.deepcopy(result["timeline"])
        with_path["database_path"] = "/tmp/private.sqlite3"
        codes = {item["code"] for item in validate_canonical_timeline(with_path)}
        self.assertIn("unknown_field", codes)

        whole_video = copy.deepcopy(result["timeline"])
        whole_video["tracks"][0]["clips"][0]["media_kind"] = "video"
        codes = {item["code"] for item in validate_canonical_timeline(whole_video)}
        self.assertIn("unsupported_evidence", codes)
        self.assertIn("required_field_missing", codes)

    def test_overlap_or_gap_in_timeline_timing_is_rejected(self) -> None:
        result, _blueprint, _plan = compile_result()
        overlap = copy.deepcopy(result["timeline"])
        overlap["tracks"][0]["clips"][1]["start_ms"] -= 1
        codes = {item["code"] for item in validate_canonical_timeline(overlap)}
        self.assertIn("invalid_clip_timing", codes)

    def test_span_lineage_and_source_range_tamper_are_rejected(self) -> None:
        result, _blueprint, _plan = compile_result()
        tampered = copy.deepcopy(result["timeline"])
        tampered["tracks"][0]["clips"][1]["source_out_ms"] += 1
        codes = {item["code"] for item in validate_canonical_timeline(tampered)}
        self.assertIn("invalid_source_range", codes)
        self.assertIn("unstable_clip_id", codes)

        manifest = copy.deepcopy(result["source_bindings"])
        manifest[1]["analysis_run_id"] = f"arun_{'4' * 32}"
        codes = {item["code"] for item in validate_timeline_source_bindings(manifest, timeline=result["timeline"])}
        self.assertIn("source_binding_clip_mismatch", codes)

    def test_source_manifest_requires_exact_clip_order_and_proof_attestation(self) -> None:
        result, _blueprint, _plan = compile_result()
        reordered = list(reversed(copy.deepcopy(result["source_bindings"])))
        codes = {item["code"] for item in validate_timeline_source_bindings(reordered, timeline=result["timeline"])}
        self.assertIn("source_binding_clip_mismatch", codes)

        forged = copy.deepcopy(result["source_bindings"])
        forged[0]["coverage_proof_sha256"] = "f" * 64
        self.assertEqual(
            validate_timeline_source_bindings(forged, timeline=result["timeline"]),
            [],
        )
        with self.assertRaises(TimelineLoweringContractError) as raised:
            require_exact_canonical_timeline_derivation(
                result["timeline"],
                source_manifest=forged,
                blueprint=_blueprint,
                blueprint_binding=blueprint_binding(_blueprint),
                coverage_plan=_plan,
                coverage_binding=coverage_binding(_plan),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "timeline_derivation_mismatch")

    def test_cold_exact_derivation_accepts_replay_and_rejects_semantic_forgery(self) -> None:
        result, blueprint_document, plan = compile_result()
        self.assertIs(
            require_exact_canonical_timeline_derivation(
                result["timeline"],
                source_manifest=result["source_bindings"],
                blueprint=blueprint_document,
                blueprint_binding=blueprint_binding(blueprint_document),
                coverage_plan=plan,
                coverage_binding=coverage_binding(plan),
            ),
            result["timeline"],
        )

        forged = copy.deepcopy(result["timeline"])
        forged["coverage_binding"]["operation_id"] = "op_forged"
        self.assertEqual(validate_canonical_timeline(forged), [])
        with self.assertRaises(TimelineLoweringContractError) as raised:
            require_exact_canonical_timeline_derivation(
                forged,
                source_manifest=result["source_bindings"],
                blueprint=blueprint_document,
                blueprint_binding=blueprint_binding(blueprint_document),
                coverage_plan=plan,
                coverage_binding=coverage_binding(plan),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "timeline_derivation_mismatch")

    def test_bindings_and_source_manifest_are_closed(self) -> None:
        blueprint_document = make_blueprint()
        plan = make_plan(blueprint_document)
        extra_blueprint_binding = blueprint_binding(blueprint_document)
        extra_blueprint_binding["database_uuid"] = "db-local"
        with self.assertRaises(TimelineLoweringContractError) as raised:
            compile_result(
                blueprint=blueprint_document,
                plan=plan,
                current_blueprint_binding=extra_blueprint_binding,
            )
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_blueprint_binding")

        result, _blueprint, _plan = compile_result()
        extra_manifest = copy.deepcopy(result["source_bindings"])
        extra_manifest[0]["filename"] = "private.jpg"
        codes = {
            item["code"] for item in validate_timeline_source_bindings(extra_manifest, timeline=result["timeline"])
        }
        self.assertIn("unknown_field", codes)


if __name__ == "__main__":
    unittest.main()
