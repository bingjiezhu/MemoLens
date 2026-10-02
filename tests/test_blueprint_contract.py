from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from core.blueprint_contract import (
    BLUEPRINT_AUTHORITY_CLAIM,
    BLUEPRINT_AUTHORITY_STATE,
    BLUEPRINT_COMPILER,
    BLUEPRINT_OBJECT,
    BLUEPRINT_SCHEMA_AVAILABLE,
    BLUEPRINT_SCHEMA_SHA256,
    BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256,
    BlueprintContractError,
    blueprint_content_sha256,
    blueprint_decision_unit_digests,
    blueprint_evidence_refs,
    blueprint_semantic_sha256,
    canonical_sha256,
    compile_blueprint_document,
    diff_blueprint_sections,
    validate_blueprint_document,
    validate_blueprint_semantic,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
ASSET_REF = "memolens://evidence/asset/asset_001"
SPAN_REF = "memolens://evidence/span/span_001"


def semantic_fixture() -> dict:
    return {
        "intent": {
            "goal": "把闲置旅行素材剪成一期短视频",
            "stance": "普通记录也值得被认真表达",
            "audience": "个人创作者",
            "platform": "抖音",
        },
        "script": {
            "blocks": [
                {"block_id": "opening", "text": "这些画面差点永远留在相册里。"},
                {"block_id": "body", "text": "现在把那天的海风重新剪回来。"},
            ]
        },
        "direction": {
            "theme": "重新发现旧素材",
            "narrative_arc": "遗忘、重见、珍惜",
            "emotion": "松弛而真诚",
            "tone": "克制",
            "pace": "前快后慢",
        },
        "output": {"duration_target_ms": 45_000, "aspect_ratio": "9:16"},
        "constraints": {
            "must_include": [
                {
                    "constraint_id": "include_sea",
                    "text": None,
                    "evidence_ref": ASSET_REF,
                    "script_block_ids": ["body"],
                }
            ],
            "must_exclude": [
                {
                    "constraint_id": "exclude_shake",
                    "text": "排除严重晃动画面",
                    "evidence_ref": None,
                    "script_block_ids": [],
                }
            ],
        },
        "material_hints": [
            {
                "hint_id": "sea_detail",
                "evidence_ref": SPAN_REF,
                "script_block_ids": ["body"],
                "reason": "承接口播中的海风",
            }
        ],
        "reference_refs": [
            {
                "reference_id": "ref_inline",
                "kind": "user_text",
                "locator": "参考一种留白较多、不过度煽情的表达",
                "note": None,
            },
            {
                "reference_id": "ref_evidence",
                "kind": "evidence",
                "locator": ASSET_REF,
                "note": "参考原片色彩",
            },
        ],
        "technique_refs": [
            {"card_id": "fast_open", "revision": 1, "declared_state": "selected"}
        ],
        "bindings": {
            "creator_context": {
                "profile_id": "creator_default",
                "revision": 2,
                "content_sha256": SHA_B,
            },
            "wiki_generation": None,
        },
        "assumptions": [
            {"assumption_id": "music", "text": "暂按轻音乐设计节奏"}
        ],
        "missing_evidence": [],
        "open_decisions": [
            {
                "decision_id": "cover_title",
                "question": "封面标题是否保留日期？",
                "scope": "other",
                "required_before": "publish",
            }
        ],
    }


def manifest_fixture() -> list[dict]:
    return [
        {"evidence_ref": SPAN_REF, "status": "unresolved", "proof_sha256": None},
        {"evidence_ref": ASSET_REF, "status": "verified", "proof_sha256": SHA_A},
    ]


def compile_fixture(*, revision: int = 1, parent: dict | None = None, semantic: dict | None = None) -> dict:
    return compile_blueprint_document(
        project_id="project_001",
        revision=revision,
        parent=parent,
        created_by_operation_id=f"operation_{revision:03d}",
        semantic=semantic or semantic_fixture(),
        source_candidate_sha256=SHA_B,
        initial_legacy_brief={"revision": 3, "content_sha256": SHA_A},
        evidence_manifest=manifest_fixture(),
    )


class BlueprintSchemaArtifactTests(unittest.TestCase):
    def test_app_and_plugin_artifacts_have_exact_byte_parity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        app = root / "core" / "schemas" / "creative-blueprint-v1.schema.json"
        plugin = (
            root
            / ".agents"
            / "plugins"
            / "plugins"
            / "memolens"
            / "schemas"
            / "creative-blueprint-v1.schema.json"
        )
        app_bytes = app.read_bytes()
        self.assertEqual(app_bytes, plugin.read_bytes())
        self.assertEqual(hashlib.sha256(app_bytes).hexdigest(), BLUEPRINT_SCHEMA_SHA256)
        self.assertEqual(BLUEPRINT_SCHEMA_SHA256, BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256)
        self.assertTrue(BLUEPRINT_SCHEMA_AVAILABLE)
        self.assertEqual(json.loads(app_bytes)["properties"]["object"]["const"], BLUEPRINT_OBJECT)


class BlueprintSemanticTests(unittest.TestCase):
    def test_valid_semantic_and_digests_are_canonical(self) -> None:
        semantic = semantic_fixture()
        reordered = {key: semantic[key] for key in reversed(semantic)}
        self.assertEqual(validate_blueprint_semantic(semantic), [])
        self.assertEqual(blueprint_semantic_sha256(semantic), blueprint_semantic_sha256(reordered))
        self.assertEqual(blueprint_evidence_refs(semantic), (ASSET_REF, SPAN_REF))

    def test_decision_unit_payloads_are_frozen_and_ordered(self) -> None:
        semantic = semantic_fixture()
        digests = blueprint_decision_unit_digests(semantic)
        self.assertEqual(
            list(digests),
            [
                "intent_goal",
                "intent_stance",
                "script",
                "creative_direction",
                "output",
                "material_constraints",
                "references",
                "techniques",
            ],
        )
        self.assertEqual(
            digests["intent_goal"],
            canonical_sha256(
                {
                    "goal": semantic["intent"]["goal"],
                    "audience": semantic["intent"]["audience"],
                    "platform": semantic["intent"]["platform"],
                }
            ),
        )
        self.assertEqual(
            digests["references"],
            canonical_sha256(
                {
                    "reference_refs": semantic["reference_refs"],
                    "bindings": semantic["bindings"],
                }
            ),
        )

    def test_candidate_and_authority_fields_are_closed_world_rejected(self) -> None:
        for mutation in (
            lambda value: value.update({"object": "memolens.creative_blueprint_candidate"}),
            lambda value: value.update({"base": None}),
            lambda value: value["intent"].update({"declared_source": "caller_declared_user_input"}),
            lambda value: value["script"].update({"authority": "user_confirmed"}),
        ):
            semantic = semantic_fixture()
            mutation(semantic)
            errors = validate_blueprint_semantic(semantic)
            self.assertTrue(errors)
            with self.assertRaises(BlueprintContractError):
                compile_fixture(semantic=semantic)
            self.assertNotIn("user_confirmed", json.dumps(errors))

    def test_cross_references_and_constraints_fail_closed(self) -> None:
        semantic = semantic_fixture()
        semantic["material_hints"][0]["script_block_ids"] = ["missing"]
        semantic["constraints"]["must_exclude"][0]["evidence_ref"] = ASSET_REF
        errors = validate_blueprint_semantic(semantic)
        self.assertIn("dangling_script_block", {item["code"] for item in errors})
        self.assertIn("contradictory_evidence_constraint", {item["code"] for item in errors})

    def test_reference_locator_rules_match_candidate_safety(self) -> None:
        for unsafe in ("/Users/person/private.mov", "看这个 /tmp/private.mov", "../private.mov", "file:///tmp/a.mov"):
            semantic = semantic_fixture()
            semantic["reference_refs"][0]["locator"] = unsafe
            errors = validate_blueprint_semantic(semantic)
            self.assertIn("invalid_user_text_reference", {item["code"] for item in errors})

    def test_boolean_revision_duration_and_control_text_are_rejected(self) -> None:
        semantic = semantic_fixture()
        semantic["output"]["duration_target_ms"] = True
        semantic["technique_refs"][0]["revision"] = True
        codes = {item["code"] for item in validate_blueprint_semantic(semantic)}
        self.assertIn("invalid_duration", codes)
        self.assertIn("invalid_revision", codes)
        control = semantic_fixture()
        control["script"]["blocks"][0]["text"] = "unsafe\x00text"
        self.assertIn(
            "forbidden_control_character",
            {item["code"] for item in validate_blueprint_semantic(control)},
        )


class BlueprintDocumentTests(unittest.TestCase):
    def test_compile_derives_identity_lineage_manifest_and_unverified_authority(self) -> None:
        semantic = semantic_fixture()
        original_semantic = deepcopy(semantic)
        original_manifest = manifest_fixture()
        document = compile_fixture(semantic=semantic)
        self.assertEqual(validate_blueprint_document(document), [])
        self.assertEqual(semantic, original_semantic)
        self.assertEqual(original_manifest, manifest_fixture())
        self.assertEqual(document["schema_sha256"], BLUEPRINT_SCHEMA_SHA256)
        self.assertEqual(document["semantic_sha256"], blueprint_semantic_sha256(semantic))
        self.assertEqual(document["lineage"]["compiler"], BLUEPRINT_COMPILER)
        self.assertEqual(
            [item["evidence_ref"] for item in document["evidence_manifest"]],
            [ASSET_REF, SPAN_REF],
        )
        self.assertEqual(document["authority"]["state"], BLUEPRINT_AUTHORITY_STATE)
        for unit in document["authority"]["decision_units"].values():
            self.assertEqual(unit["claim"], BLUEPRINT_AUTHORITY_CLAIM)
            self.assertIs(unit["verified"], False)
        serialized = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("declared_source", serialized)
        self.assertNotIn("creative_blueprint_candidate", serialized)
        self.assertNotIn('"base"', serialized)

    def test_manifest_must_exactly_cover_semantic_evidence(self) -> None:
        for manifest in (
            manifest_fixture()[:1],
            manifest_fixture()
            + [
                {
                    "evidence_ref": "memolens://evidence/asset/extra",
                    "status": "unresolved",
                    "proof_sha256": None,
                }
            ],
        ):
            with self.assertRaises(BlueprintContractError) as raised:
                compile_blueprint_document(
                    project_id="project_001",
                    revision=1,
                    parent=None,
                    created_by_operation_id="operation_001",
                    semantic=semantic_fixture(),
                    source_candidate_sha256=None,
                    initial_legacy_brief={"revision": 3, "content_sha256": SHA_A},
                    evidence_manifest=manifest,
                )
            self.assertIn("evidence_manifest_mismatch", {item["code"] for item in raised.exception.errors})

    def test_manifest_proof_state_is_not_self_proving(self) -> None:
        manifest = manifest_fixture()
        manifest[0]["status"] = "verified"
        manifest[0]["proof_sha256"] = None
        with self.assertRaises(BlueprintContractError) as raised:
            compile_blueprint_document(
                project_id="project_001",
                revision=1,
                parent=None,
                created_by_operation_id="operation_001",
                semantic=semantic_fixture(),
                source_candidate_sha256=None,
                initial_legacy_brief={"revision": 3, "content_sha256": SHA_A},
                evidence_manifest=manifest,
            )
        self.assertIn("evidence_proof_missing", {item["code"] for item in raised.exception.errors})

    def test_invalid_compiler_components_raise_only_contract_errors(self) -> None:
        for parent, manifest in (
            (None, [{"evidence_ref": 7, "status": "unresolved", "proof_sha256": None}]),
            (None, [{"evidence_ref": ASSET_REF, "status": {"bad"}, "proof_sha256": None}]),
            ({"revision": 1, "content_sha256": {"bad"}}, manifest_fixture()),
        ):
            with self.assertRaises(BlueprintContractError):
                compile_blueprint_document(
                    project_id="project_001",
                    revision=2 if parent is not None else 1,
                    parent=parent,
                    created_by_operation_id="operation_002",
                    semantic=semantic_fixture(),
                    source_candidate_sha256=None,
                    initial_legacy_brief={"revision": 3, "content_sha256": SHA_A},
                    evidence_manifest=manifest,
                )

    def test_row_bindings_and_derived_digests_fail_closed(self) -> None:
        document = compile_fixture()
        digest = blueprint_content_sha256(document)
        self.assertEqual(
            validate_blueprint_document(
                document,
                expected_project_id="project_001",
                expected_revision=1,
                expected_content_sha256=digest,
                expected_operation_id="operation_001",
            ),
            [],
        )
        tampered = deepcopy(document)
        tampered["semantic"]["intent"]["stance"] = "被篡改"
        codes = {item["code"] for item in validate_blueprint_document(tampered)}
        self.assertIn("semantic_digest_mismatch", codes)
        self.assertIn("authority_mismatch", codes)
        row_codes = {
            item["code"]
            for item in validate_blueprint_document(
                document,
                expected_project_id="another_project",
                expected_revision=2,
                expected_content_sha256=SHA_A,
                expected_operation_id="another_operation",
            )
        }
        self.assertEqual(row_codes, {"row_identity_mismatch", "content_digest_mismatch"})

    def test_revision_parent_is_linear_and_restore_has_new_content_identity(self) -> None:
        first = compile_fixture()
        first_digest = blueprint_content_sha256(first)
        second = compile_fixture(
            revision=2,
            parent={"revision": 1, "content_sha256": first_digest},
        )
        self.assertEqual(validate_blueprint_document(second), [])
        self.assertEqual(first["semantic_sha256"], second["semantic_sha256"])
        self.assertNotEqual(blueprint_content_sha256(first), blueprint_content_sha256(second))
        broken = deepcopy(second)
        broken["parent"]["revision"] = 2
        self.assertIn("invalid_parent", {item["code"] for item in validate_blueprint_document(broken)})

    def test_authority_cannot_be_upgraded_by_tampering(self) -> None:
        document = compile_fixture()
        document["authority"]["state"] = "verified"
        document["authority"]["decision_units"]["script"]["verified"] = True
        errors = validate_blueprint_document(document)
        self.assertEqual({item["code"] for item in errors}, {"authority_mismatch"})

    def test_section_diff_is_typed_stable_and_omits_unchanged_sections(self) -> None:
        before = semantic_fixture()
        after = deepcopy(before)
        after["intent"]["goal"] = "新的创作目标"
        after["script"]["blocks"][0]["text"] = "新的开场"
        changes = diff_blueprint_sections(before, after)
        self.assertEqual([item["section"] for item in changes], ["intent", "script"])
        self.assertTrue(all(item["change"] == "modified" for item in changes))
        self.assertEqual(diff_blueprint_sections(after, after), [])
        self.assertEqual(len(diff_blueprint_sections(None, after)), 12)


if __name__ == "__main__":
    unittest.main()
