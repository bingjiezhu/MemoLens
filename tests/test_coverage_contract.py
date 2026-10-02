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
    COVERAGE_BASELINE_COMPILER,
    CoverageContractError,
    canonical_sha256,
    compile_baseline_coverage_plan,
    coverage_plan_content_sha256,
    inspect_coverage_plan_for_read_only,
    normalize_evidence_proofs,
    require_current_coverage_plan,
    require_exact_baseline_derivation,
    validate_coverage_plan,
)


IMAGE_SHA = hashlib.sha256(b"image").hexdigest()
VIDEO_SHA = hashlib.sha256(b"video").hexdigest()
EXTRA_SHA = hashlib.sha256(b"extra").hexdigest()
ASSET_ID = f"asset_{IMAGE_SHA[:24]}"
ASSET_REF = f"memolens://evidence/asset/{ASSET_ID}"
VIDEO_ASSET_ID = f"asset_{VIDEO_SHA[:24]}"
SPAN_ID = f"seg_{VIDEO_SHA[:24]}_1_0"
SPAN_REF = f"memolens://evidence/span/{SPAN_ID}"
ANALYSIS_RUN_ID = f"arun_{'3' * 32}"
IMAGE_ANALYSIS_RUN_ID = f"arun_{'4' * 32}"
IMAGE_ANALYSIS_CONTENT_SHA = hashlib.sha256(b"image-analysis").hexdigest()
IMAGE_SOURCE_BINDING_SHA = hashlib.sha256(b"image-source-binding").hexdigest()
EXTRA_ASSET_ID = f"asset_{EXTRA_SHA[:24]}"
EXTRA_ASSET_REF = f"memolens://evidence/asset/{EXTRA_ASSET_ID}"


def asset_proof(
    asset_id: str,
    asset_sha256: str,
    *,
    analysis_run_id: str = IMAGE_ANALYSIS_RUN_ID,
    analysis_revision: int = 1,
    analysis_content_sha256: str = IMAGE_ANALYSIS_CONTENT_SHA,
    source_binding_sha256: str = IMAGE_SOURCE_BINDING_SHA,
) -> dict[str, object]:
    return {
        "kind": "asset",
        "asset_id": asset_id,
        "asset_sha256": asset_sha256,
        "analysis_run_id": analysis_run_id,
        "analysis_revision": analysis_revision,
        "analysis_content_sha256": analysis_content_sha256,
        "source_binding_sha256": source_binding_sha256,
    }


def semantic() -> dict[str, object]:
    return {
        "intent": {"goal": "把旧素材剪成短片", "stance": None, "audience": "朋友", "platform": "小红书"},
        "script": {
            "blocks": [
                {"block_id": "opening", "text": "这些片段一直留在硬盘里。"},
                {"block_id": "middle", "text": "今天终于把它们重新看了一遍。"},
                {"block_id": "ending", "text": "原来普通的日子也值得被记住。"},
            ]
        },
        "direction": {
            "theme": "记忆",
            "narrative_arc": "重看",
            "emotion": "温暖",
            "tone": "自然",
            "pace": "舒缓",
        },
        "output": {"duration_target_ms": 12_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [
            {
                "hint_id": "hint_open",
                "evidence_ref": ASSET_REF,
                "script_block_ids": ["opening"],
                "reason": "旧照片适合作为开场。",
            },
            {
                "hint_id": "hint_middle",
                "evidence_ref": SPAN_REF,
                "script_block_ids": ["middle"],
                "reason": "视频段落展示重新翻看。",
            },
        ],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def blueprint(value: dict[str, object] | None = None) -> dict[str, object]:
    current_semantic = value if value is not None else semantic()
    evidence_refs = sorted({str(hint["evidence_ref"]) for hint in current_semantic["material_hints"]})
    return compile_blueprint_document(
        project_id="project_coverage",
        revision=1,
        parent=None,
        created_by_operation_id="op_blueprint",
        semantic=current_semantic,
        source_candidate_sha256="a" * 64,
        initial_legacy_brief={"revision": 1, "content_sha256": "d" * 64},
        evidence_manifest=[
            {
                "evidence_ref": evidence_ref,
                "status": "verified",
                "proof_sha256": hashlib.sha256(evidence_ref.encode()).hexdigest(),
            }
            for evidence_ref in evidence_refs
        ],
    )


def evidence_rows() -> list[dict[str, object]]:
    return [
        {
            "evidence_ref": ASSET_REF,
            "proof": asset_proof(ASSET_ID, IMAGE_SHA),
        },
        {
            "evidence_ref": SPAN_REF,
            "proof": {
                "kind": "span",
                "span_id": SPAN_ID,
                "asset_id": VIDEO_ASSET_ID,
                "asset_sha256": VIDEO_SHA,
                "analysis_run_id": ANALYSIS_RUN_ID,
                "analysis_revision": 1,
                "input_asset_sha256": VIDEO_SHA,
                "start_ms": 1_000,
                "end_ms": 8_000,
            },
        },
    ]


def compile_plan(
    *,
    document: dict[str, object] | None = None,
    rows: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    current = document or blueprint()
    return compile_baseline_coverage_plan(
        project_id="project_coverage",
        revision=1,
        parent=None,
        blueprint=current,
        blueprint_binding={
            "revision": 1,
            "content_sha256": blueprint_content_sha256(current),
            "semantic_sha256": blueprint_semantic_sha256(current["semantic"]),
        },
        evidence_rows=rows if rows is not None else evidence_rows(),
    )


class CoverageContractTests(unittest.TestCase):
    def test_compiles_exact_script_coverage_with_honest_gap(self) -> None:
        plan = compile_plan()

        self.assertEqual(plan["compiler"]["id"], COVERAGE_BASELINE_COMPILER)
        self.assertFalse(plan["compiler"]["semantic_matching"])
        self.assertFalse(plan["compiler"]["global_optimization"])
        self.assertEqual(len(plan["beats"]), 3)
        self.assertEqual(
            [beat["script_block_id"] for beat in plan["beats"]],
            ["opening", "middle", "ending"],
        )
        self.assertEqual(len(plan["beats"][0]["selected_assignments"]), 1)
        self.assertEqual(len(plan["beats"][1]["selected_assignments"]), 1)
        self.assertEqual(
            plan["beats"][2]["gap"],
            {"code": "no_material_hint", "blocking": False},
        )
        self.assertEqual(plan["beats"][-1]["timing"]["end_ms"], 12_000)
        self.assertEqual(validate_coverage_plan(plan), [])

    def test_replay_is_deterministic_and_domain_document_has_no_clock(self) -> None:
        first = compile_plan()
        second = compile_plan()

        self.assertEqual(first, second)
        self.assertEqual(
            coverage_plan_content_sha256(first),
            coverage_plan_content_sha256(second),
        )
        encoded = str(first).casefold()
        self.assertNotIn("created_at", encoded)
        self.assertNotIn("timestamp", encoded)

    def test_unresolved_evidence_is_never_selected(self) -> None:
        rows = evidence_rows()
        rows[0] = {
            "evidence_ref": ASSET_REF,
            "proof": None,
        }
        plan = compile_plan(rows=rows)

        opening = plan["beats"][0]
        self.assertEqual(opening["selected_assignments"], [])
        self.assertEqual(opening["gap"]["code"], "evidence_unresolved")
        self.assertEqual(
            opening["alternatives"][0]["not_selected_reason"],
            "evidence_unresolved",
        )

    def test_duplicate_evidence_is_reserved_for_first_beat(self) -> None:
        proposed = semantic()
        proposed["material_hints"].append(
            {
                "hint_id": "hint_repeat",
                "evidence_ref": ASSET_REF,
                "script_block_ids": ["ending"],
                "reason": "不要自动重复。",
            }
        )
        plan = compile_plan(document=blueprint(proposed))

        self.assertEqual(
            plan["beats"][2]["gap"],
            {"code": "duplicate_evidence_reserved", "blocking": False},
        )
        self.assertEqual(
            plan["beats"][2]["alternatives"][0]["not_selected_reason"],
            "duplicate_evidence_reserved",
        )

    def test_non_null_invalid_proof_fails_closed(self) -> None:
        rows = evidence_rows()
        rows[0]["proof"] = {
            **rows[0]["proof"],
            "path": "/Users/example/private.jpg",
        }

        with self.assertRaises(CoverageContractError) as raised:
            normalize_evidence_proofs(rows)
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_evidence_proof")

    def test_canonical_manifest_attestation_cannot_disagree_with_proof(self) -> None:
        manifest = list(normalize_evidence_proofs(evidence_rows()))
        manifest[0]["status"] = "unresolved"
        manifest[0]["proof_sha256"] = None

        with self.assertRaises(CoverageContractError) as raised:
            normalize_evidence_proofs(manifest)
        self.assertEqual(
            raised.exception.errors[0]["code"],
            "invalid_evidence_attestation",
        )

    def test_verified_proof_identity_is_bound_to_content_and_revision(self) -> None:
        wrong_content = evidence_rows()
        wrong_content[0]["proof"]["asset_sha256"] = "f" * 64
        with self.assertRaises(CoverageContractError) as asset_error:
            normalize_evidence_proofs(wrong_content)
        self.assertEqual(
            asset_error.exception.errors[0]["code"],
            "invalid_evidence_proof",
        )

        wrong_revision = evidence_rows()
        wrong_revision[1]["proof"]["analysis_revision"] = 2
        with self.assertRaises(CoverageContractError) as span_error:
            normalize_evidence_proofs(wrong_revision)
        self.assertEqual(
            span_error.exception.errors[0]["code"],
            "invalid_evidence_proof",
        )

    def test_current_image_proof_is_closed_and_exactly_revision_bound(self) -> None:
        required = {
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        }
        for field in sorted(required):
            with self.subTest(missing=field):
                rows = evidence_rows()
                del rows[0]["proof"][field]
                with self.assertRaises(CoverageContractError) as raised:
                    normalize_evidence_proofs(rows)
                self.assertEqual(raised.exception.errors[0]["code"], "invalid_evidence_proof")

        first = list(normalize_evidence_proofs(evidence_rows()))[0]
        revised_rows = evidence_rows()
        revised_rows[0]["proof"].update(
            analysis_revision=2,
            analysis_content_sha256=hashlib.sha256(b"image-analysis-revision-2").hexdigest(),
            source_binding_sha256=hashlib.sha256(b"image-source-binding-revision-2").hexdigest(),
        )
        revised = list(normalize_evidence_proofs(revised_rows))[0]
        self.assertNotEqual(first["proof_sha256"], revised["proof_sha256"])
        self.assertEqual(revised["proof_sha256"], canonical_sha256(revised["proof"]))

    def test_legacy_identity_only_image_plan_is_readable_but_non_current(self) -> None:
        current = compile_plan()
        self.assertEqual(
            inspect_coverage_plan_for_read_only(current)["evidence_currency"],
            "current",
        )

        legacy = copy.deepcopy(current)
        proof = legacy["evidence_manifest"][0]["proof"]
        for field in (
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            proof.pop(field)
        legacy["evidence_manifest"][0]["proof_sha256"] = canonical_sha256(proof)

        self.assertEqual(validate_coverage_plan(legacy), [])
        inspection = inspect_coverage_plan_for_read_only(legacy)
        self.assertIs(inspection["document"], legacy)
        self.assertEqual(inspection["evidence_currency"], "legacy_non_current")
        self.assertFalse(inspection["eligible_for_current_use"])
        self.assertEqual(inspection["reason_code"], "legacy_image_identity_only")
        with self.assertRaises(CoverageContractError) as current_error:
            require_current_coverage_plan(legacy)
        self.assertEqual(
            current_error.exception.errors[0]["code"],
            "legacy_image_evidence_non_current",
        )
        with self.assertRaises(CoverageContractError):
            require_exact_baseline_derivation(legacy, blueprint=blueprint())

        legacy_rows = evidence_rows()
        legacy_rows[0]["proof"] = {
            "kind": "asset",
            "asset_id": ASSET_ID,
            "asset_sha256": IMAGE_SHA,
        }
        with self.assertRaises(CoverageContractError) as lowering_error:
            compile_plan(rows=legacy_rows)
        self.assertEqual(
            lowering_error.exception.errors[0]["code"],
            "legacy_image_evidence_non_current",
        )

    def test_manifest_exactly_matches_blueprint_material_hint_evidence(self) -> None:
        with self.assertRaises(CoverageContractError) as missing:
            compile_plan(rows=[])
        self.assertEqual(
            missing.exception.errors[0]["code"],
            "evidence_manifest_mismatch",
        )

        rows = evidence_rows()
        rows.append(
            {
                "evidence_ref": EXTRA_ASSET_REF,
                "proof": asset_proof(
                    EXTRA_ASSET_ID,
                    EXTRA_SHA,
                    analysis_run_id=f"arun_{'5' * 32}",
                    analysis_content_sha256=hashlib.sha256(b"extra-analysis").hexdigest(),
                    source_binding_sha256=hashlib.sha256(b"extra-source-binding").hexdigest(),
                ),
            }
        )
        with self.assertRaises(CoverageContractError) as extra:
            compile_plan(rows=rows)
        self.assertEqual(
            extra.exception.errors[0]["code"],
            "evidence_manifest_mismatch",
        )

    def test_evidence_references_require_opaque_core_identities(self) -> None:
        rows = evidence_rows()
        rows[0] = {
            "evidence_ref": "memolens://evidence/asset/family-vacation.jpg",
            "proof": {
                **asset_proof(ASSET_ID, IMAGE_SHA),
                "asset_id": "family-vacation.jpg",
            },
        }

        with self.assertRaises(CoverageContractError) as raised:
            normalize_evidence_proofs(rows)
        self.assertEqual(
            raised.exception.errors[0]["code"],
            "invalid_evidence_reference",
        )

        legacy_image_id = f"img_{IMAGE_SHA[:24]}"
        normalized = normalize_evidence_proofs(
            [
                {
                    "evidence_ref": f"memolens://evidence/asset/{legacy_image_id}",
                    "proof": asset_proof(legacy_image_id, IMAGE_SHA),
                }
            ]
        )
        self.assertEqual(normalized[0]["status"], "verified")

    def test_asset_proof_is_closed_and_cannot_claim_a_whole_video(self) -> None:
        rows = evidence_rows()
        rows[0]["proof"] = {
            **rows[0]["proof"],
            "media_kind": "video",
        }

        with self.assertRaises(CoverageContractError) as raised:
            normalize_evidence_proofs(rows)
        self.assertEqual(raised.exception.errors[0]["code"], "invalid_evidence_proof")

    def test_exact_blueprint_binding_is_required(self) -> None:
        current = blueprint()
        with self.assertRaises(CoverageContractError) as raised:
            compile_baseline_coverage_plan(
                project_id="project_coverage",
                revision=1,
                parent=None,
                blueprint=current,
                blueprint_binding={
                    "revision": 1,
                    "content_sha256": "0" * 64,
                    "semantic_sha256": blueprint_semantic_sha256(current["semantic"]),
                },
                evidence_rows=evidence_rows(),
            )
        self.assertEqual(raised.exception.errors[0]["code"], "blueprint_binding_mismatch")

    def test_tamper_is_detected(self) -> None:
        plan = compile_plan()
        tampered = copy.deepcopy(plan)
        tampered["beats"][0]["selected_assignments"][0]["evidence_ref"] = EXTRA_ASSET_REF
        codes = {item["code"] for item in validate_coverage_plan(tampered)}
        self.assertIn("selected_evidence_unverified", codes)
        self.assertIn("dangling_evidence_reference", codes)
        self.assertIn("evidence_manifest_reference_mismatch", codes)

    def test_unused_manifest_evidence_is_rejected(self) -> None:
        plan = compile_plan()
        tampered = copy.deepcopy(plan)
        extra = normalize_evidence_proofs(
            [
                {
                    "evidence_ref": EXTRA_ASSET_REF,
                    "proof": asset_proof(
                        EXTRA_ASSET_ID,
                        EXTRA_SHA,
                        analysis_run_id=f"arun_{'5' * 32}",
                        analysis_content_sha256=hashlib.sha256(b"extra-analysis").hexdigest(),
                        source_binding_sha256=hashlib.sha256(b"extra-source-binding").hexdigest(),
                    ),
                }
            ]
        )[0]
        tampered["evidence_manifest"].append(extra)
        tampered["evidence_manifest"].sort(key=lambda row: row["evidence_ref"])

        codes = {item["code"] for item in validate_coverage_plan(tampered)}
        self.assertIn("evidence_manifest_reference_mismatch", codes)

    def test_alternative_and_gap_reasons_are_derived_from_proof_state(self) -> None:
        rows = evidence_rows()
        rows[0]["proof"] = None
        plan = compile_plan(rows=rows)

        forged_alternative = copy.deepcopy(plan)
        forged_alternative["beats"][0]["alternatives"][0]["not_selected_reason"] = "duplicate_evidence_reserved"
        codes = {item["code"] for item in validate_coverage_plan(forged_alternative)}
        self.assertIn("alternative_reason_mismatch", codes)

        forged_gap = copy.deepcopy(plan)
        forged_gap["beats"][0]["gap"]["code"] = "no_material_hint"
        codes = {item["code"] for item in validate_coverage_plan(forged_gap)}
        self.assertIn("gap_reason_mismatch", codes)

    def test_viable_second_hint_is_a_reproducible_baseline_alternative(self) -> None:
        proposed = semantic()
        proposed["material_hints"].append(
            {
                "hint_id": "hint_open_extra",
                "evidence_ref": EXTRA_ASSET_REF,
                "script_block_ids": ["opening"],
                "reason": "第二个已验证素材保留为替代。",
            }
        )
        rows = evidence_rows()
        rows.append(
            {
                "evidence_ref": EXTRA_ASSET_REF,
                "proof": asset_proof(
                    EXTRA_ASSET_ID,
                    EXTRA_SHA,
                    analysis_run_id=f"arun_{'5' * 32}",
                    analysis_content_sha256=hashlib.sha256(b"extra-analysis").hexdigest(),
                    source_binding_sha256=hashlib.sha256(b"extra-source-binding").hexdigest(),
                ),
            }
        )
        plan = compile_plan(document=blueprint(proposed), rows=rows)
        alternative = plan["beats"][0]["alternatives"][0]
        self.assertEqual(
            alternative["not_selected_reason"],
            "baseline_first_verified_selected",
        )

        forged = copy.deepcopy(plan)
        forged["beats"][0]["alternatives"][0]["not_selected_reason"] = "evidence_unresolved"
        codes = {item["code"] for item in validate_coverage_plan(forged)}
        self.assertIn("alternative_reason_mismatch", codes)

    def test_stable_ids_and_assignment_duration_cannot_be_forged(self) -> None:
        plan = compile_plan()

        forged_beat = copy.deepcopy(plan)
        forged_beat["beats"][0]["beat_id"] = "beat_forged"
        codes = {item["code"] for item in validate_coverage_plan(forged_beat)}
        self.assertIn("unstable_beat_id", codes)

        forged_need = copy.deepcopy(plan)
        forged_need["beats"][0]["needs"][0]["need_id"] = "need_forged"
        forged_need["beats"][0]["selected_assignments"][0]["need_id"] = "need_forged"
        codes = {item["code"] for item in validate_coverage_plan(forged_need)}
        self.assertIn("unstable_need_id", codes)

        forged_assignment = copy.deepcopy(plan)
        assignment = forged_assignment["beats"][0]["selected_assignments"][0]
        assignment["assignment_id"] = "assign_forged"
        assignment["timeline_duration_ms"] -= 1
        codes = {item["code"] for item in validate_coverage_plan(forged_assignment)}
        self.assertIn("unstable_assignment_id", codes)
        self.assertIn("assignment_duration_mismatch", codes)

    def test_short_video_span_remains_an_honest_gap(self) -> None:
        rows = evidence_rows()
        rows[1]["proof"]["end_ms"] = 1_500

        plan = compile_plan(rows=rows)

        middle = plan["beats"][1]
        self.assertEqual(middle["selected_assignments"], [])
        self.assertEqual(
            middle["alternatives"][0]["not_selected_reason"],
            "evidence_span_too_short",
        )
        self.assertEqual(
            middle["gap"],
            {"code": "evidence_span_too_short", "blocking": False},
        )

    def test_exact_derivation_rejects_a_dropped_script_beat(self) -> None:
        source_blueprint = blueprint()
        plan = compile_plan(document=source_blueprint)
        forged = copy.deepcopy(plan)
        forged["beats"].pop(1)
        forged["evidence_manifest"] = [item for item in forged["evidence_manifest"] if item["evidence_ref"] != SPAN_REF]
        forged["beats"][1]["ordinal"] = 1
        forged["beats"][1]["timing"]["start_ms"] = forged["beats"][0]["timing"]["end_ms"]
        forged["beats"][1]["timing"]["end_ms"] = forged["output_binding"]["duration_target_ms"]
        self.assertEqual(validate_coverage_plan(forged), [])
        with self.assertRaises(CoverageContractError) as raised:
            require_exact_baseline_derivation(forged, blueprint=source_blueprint)
        self.assertEqual(
            raised.exception.errors[0]["code"],
            "baseline_derivation_mismatch",
        )

    def test_exact_derivation_rejects_valid_shape_source_digest_forgery(self) -> None:
        source_blueprint = blueprint()
        forged = compile_plan(document=source_blueprint)
        forged["beats"][0]["selected_assignments"][0]["reason_sha256"] = "f" * 64

        self.assertEqual(validate_coverage_plan(forged), [])
        with self.assertRaises(CoverageContractError) as raised:
            require_exact_baseline_derivation(forged, blueprint=source_blueprint)
        self.assertEqual(
            raised.exception.errors[0]["code"],
            "baseline_derivation_mismatch",
        )


if __name__ == "__main__":
    unittest.main()
