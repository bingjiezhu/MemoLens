from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest

from core.image_analysis_contract import (
    IMAGE_ANALYSIS_MAX_TAGS,
    IMAGE_ANALYSIS_RESULT_OBJECT,
    IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION,
    IMAGE_PROJECTION_CHANGE_OBJECT,
    IMAGE_PROJECTION_CHANGE_SCHEMA_VERSION,
    IMAGE_PROJECTION_CONTRACT,
    IMAGE_PROJECTION_RECEIPT_OBJECT,
    IMAGE_PROJECTION_RECEIPT_SCHEMA_VERSION,
    IMAGE_PUBLISH_RECEIPT_OBJECT,
    IMAGE_PUBLISH_RECEIPT_SCHEMA_VERSION,
    ImageAnalysisContractError,
    analysis_binding_sha256,
    build_canonical_image_combined_text,
    build_projection_manifest,
    canonical_image_analysis_result_json,
    canonical_json,
    canonical_projection_manifest_json,
    image_analysis_result_sha256,
    projection_change_sha256,
    projection_manifest_sha256,
    projection_receipt_sha256,
    publish_receipt_sha256,
    require_valid_image_analysis_result,
    seal_image_analysis_result,
    seal_projection_change,
    seal_projection_receipt,
    seal_publish_receipt,
    source_binding_sha256,
    validate_image_analysis_result,
    validate_projection_change,
    validate_projection_manifest,
    validate_projection_receipt,
    validate_publish_receipt,
)


DATABASE_UUID = "123e4567-e89b-42d3-a456-426614174000"
ASSET_SHA = hashlib.sha256(b"canonical-image").hexdigest()
ASSET_ID = f"asset_{ASSET_SHA[:24]}"
RUN_ID = f"arun_{'1' * 32}"
SOURCE_ID = f"src_{'2' * 24}"
ROOT_ID = f"root_{'3' * 24}"
JOB_ID = f"job_{'4' * 32}"


def provenance(
    *,
    model_id: str | None = None,
    model_version: str | None = None,
    rule_id: str | None = None,
    rule_version: str | None = None,
) -> dict[str, object]:
    return {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": model_id,
        "model_version": model_version,
        "rule_id": rule_id,
        "rule_version": rule_version,
    }


def source_binding() -> dict[str, object]:
    return {
        "source_id": SOURCE_ID,
        "library_root_id": ROOT_ID,
        "observed_size": 12_345,
        "observed_mtime_ns": 1_800_000_000_000_000_000,
        "file_identity_sha256": hashlib.sha256(b"dev:inode:mode").hexdigest(),
    }


def result_payload() -> dict[str, object]:
    return {
        "object": IMAGE_ANALYSIS_RESULT_OBJECT,
        "schema_version": IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION,
        "asset_id": ASSET_ID,
        "asset_sha256": ASSET_SHA,
        "analysis_run_id": RUN_ID,
        "revision": 1,
        "source_binding": source_binding(),
        "analysis_profile": {
            "id": "memolens.image-analysis",
            "version": "1",
            "content_sha256": hashlib.sha256(b"profile").hexdigest(),
        },
        "stages": {
            "metadata": {
                "status": "succeeded",
                "provenance": provenance(rule_id="exif", rule_version="1"),
                "output": {
                    "mime_type": "image/jpeg",
                    "file_size": 12_345,
                    "width": 1_920,
                    "height": 1_080,
                    "taken_at": "2026-08-01T12:30:00",
                    "latitude": 30.25,
                    "longitude": 120.15,
                    "altitude": 18.0,
                },
                "artifact_sha256": hashlib.sha256(b"metadata").hexdigest(),
                "reason_code": None,
            },
            "geocode": {
                "status": "disabled",
                "provenance": provenance(rule_id="network-policy", rule_version="1"),
                "output": None,
                "artifact_sha256": None,
                "reason_code": "network_not_authorized",
            },
            "vision": {
                "status": "succeeded",
                "provenance": provenance(model_id="local/vision", model_version="1"),
                "output": {
                    "description": "山边的日落。",
                    "tags": ["日落", "山"],
                    "location_hint": None,
                },
                "artifact_sha256": hashlib.sha256(b"vision").hexdigest(),
                "reason_code": None,
            },
            "embedding": {
                "status": "succeeded",
                "provenance": provenance(rule_id="embedding-router", rule_version="1"),
                "output": {
                    "combined_text_sha256": hashlib.sha256(
                        build_canonical_image_combined_text(
                            vision_output={
                                "description": "山边的日落。",
                                "tags": ["日落", "山"],
                                "location_hint": None,
                            },
                            geocode_output=None,
                        ).encode("utf-8")
                    ).hexdigest(),
                    "vectors": [
                        {
                            "purpose": "text_search",
                            "signal": "text_derived",
                            "model_id": "bge-m3",
                            "dimensions": 1_024,
                            "artifact_sha256": hashlib.sha256(b"text-vector").hexdigest(),
                        },
                        {
                            "purpose": "image_search",
                            # This explicit signal preserves the current semantic-hash
                            # boundary instead of claiming visual model evidence.
                            "signal": "text_derived",
                            "model_id": "semantic_hash",
                            "dimensions": 384,
                            "artifact_sha256": hashlib.sha256(b"image-vector").hexdigest(),
                        },
                    ],
                },
                "artifact_sha256": hashlib.sha256(b"embedding-stage").hexdigest(),
                "reason_code": None,
            },
            "quality": {
                "status": "succeeded",
                "provenance": provenance(rule_id="local-quality", rule_version="1"),
                "output": {"aesthetic_score": 0.73, "technical_quality_score": 0.81},
                "artifact_sha256": hashlib.sha256(b"quality").hexdigest(),
                "reason_code": None,
            },
        },
    }


def binding(*, run: str = RUN_ID, revision: int = 1, digest: str | None = None) -> dict[str, object]:
    return {
        "analysis_run_id": run,
        "revision": revision,
        "content_sha256": digest or hashlib.sha256(f"result-{revision}".encode()).hexdigest(),
    }


def change_payload(*, analysis: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "object": IMAGE_PROJECTION_CHANGE_OBJECT,
        "schema_version": IMAGE_PROJECTION_CHANGE_SCHEMA_VERSION,
        "projection_contract": IMAGE_PROJECTION_CONTRACT,
        "database_uuid": DATABASE_UUID,
        "change_position": 7,
        "asset_id": ASSET_ID,
        "analysis_binding": analysis or binding(),
        "source_binding_sha256": source_binding_sha256(source_binding()),
        "operation": "upsert",
    }


def publish_payload(change: dict[str, object]) -> dict[str, object]:
    return {
        "object": IMAGE_PUBLISH_RECEIPT_OBJECT,
        "schema_version": IMAGE_PUBLISH_RECEIPT_SCHEMA_VERSION,
        "database_uuid": DATABASE_UUID,
        "job_id": JOB_ID,
        "request_sha256": hashlib.sha256(b"request").hexdigest(),
        "asset_id": ASSET_ID,
        "source_binding_sha256": source_binding_sha256(source_binding()),
        "expected_prior_head": None,
        "result_head": binding(digest=ASSET_SHA),
        "change_position": 7,
        "change_sha256": str(change["change_sha256"]),
    }


def projection_receipt_payload() -> dict[str, object]:
    return {
        "object": IMAGE_PROJECTION_RECEIPT_OBJECT,
        "schema_version": IMAGE_PROJECTION_RECEIPT_SCHEMA_VERSION,
        "projection_contract": IMAGE_PROJECTION_CONTRACT,
        "projector_version": "memolens.image-projector/v1",
        "database_uuid": DATABASE_UUID,
        "change_position": 7,
        "asset_id": ASSET_ID,
        "analysis_binding": binding(digest=ASSET_SHA),
        "source_binding_sha256": source_binding_sha256(source_binding()),
        "projected_row_sha256": hashlib.sha256(b"legacy-row").hexdigest(),
        "alias_set_sha256": hashlib.sha256(b"aliases").hexdigest(),
        "outcome": "applied",
        "reason_code": None,
    }


def manifest_entry(
    suffix: str,
    status: str,
    *,
    expected: str | None,
    actual: str | None,
    reason: str | None,
    analysis: bool = True,
) -> dict[str, object]:
    return {
        "asset_id": f"asset_{suffix * 24}",
        "analysis_binding": binding(run=f"arun_{suffix * 32}") if analysis else None,
        "expected_row_sha256": expected,
        "actual_row_sha256": actual,
        "status": status,
        "reason_code": reason,
    }


class ImageAnalysisResultContractTests(unittest.TestCase):
    def test_result_seals_typed_stage_outputs_and_is_replay_stable(self) -> None:
        first = seal_image_analysis_result(result_payload())
        reordered = result_payload()
        reordered["stages"]["vision"]["output"]["tags"] = ["山", "日落"]
        reordered["stages"]["embedding"]["output"]["vectors"].reverse()
        second = seal_image_analysis_result(reordered)

        self.assertEqual(first, second)
        self.assertEqual(first["content_sha256"], image_analysis_result_sha256(first))
        self.assertEqual(validate_image_analysis_result(first), [])
        require_valid_image_analysis_result(first)
        self.assertEqual(first["stages"]["vision"]["output"]["tags"], ["山", "日落"])
        self.assertEqual(
            [row["purpose"] for row in first["stages"]["embedding"]["output"]["vectors"]],
            ["image_search", "text_search"],
        )
        self.assertEqual(
            first["stages"]["embedding"]["output"]["vectors"][0]["signal"],
            "text_derived",
        )
        self.assertEqual(canonical_image_analysis_result_json(first), canonical_json(first))

    def test_result_is_closed_typed_bounded_and_self_authenticating(self) -> None:
        sealed = seal_image_analysis_result(result_payload())
        for field in ("created_at", "updated_at", "timestamp", "generation_id", "absolute_path"):
            candidate = copy.deepcopy(sealed)
            candidate[field] = "2026-08-29T00:00:00Z"
            self.assertIn("unknown_field", {row["code"] for row in validate_image_analysis_result(candidate)})

        tampered = copy.deepcopy(sealed)
        tampered["stages"]["quality"]["output"]["technical_quality_score"] = 0.22
        self.assertIn("digest_mismatch", {row["code"] for row in validate_image_analysis_result(tampered)})
        self.assertNotEqual(tampered["content_sha256"], image_analysis_result_sha256(tampered))

        wrong_asset = result_payload()
        wrong_asset["asset_id"] = f"asset_{'f' * 24}"
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(wrong_asset)
        self.assertIn("asset_identity_mismatch", {row["code"] for row in raised.exception.errors})

        boolean_revision = result_payload()
        boolean_revision["revision"] = True
        self.assertRaises(ImageAnalysisContractError, seal_image_analysis_result, boolean_revision)

    def test_stage_state_machine_and_numeric_limits_fail_closed(self) -> None:
        missing_reason = result_payload()
        missing_reason["stages"]["geocode"]["reason_code"] = None
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(missing_reason)
        self.assertIn("reason_required", {row["code"] for row in raised.exception.errors})

        forbidden_output = result_payload()
        forbidden_output["stages"]["geocode"]["output"] = {"place_name": "杭州", "country": "中国"}
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(forbidden_output)
        self.assertIn("stage_output_forbidden", {row["code"] for row in raised.exception.errors})

        untyped = result_payload()
        untyped["stages"]["quality"]["output"]["confidence"] = 0.9
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(untyped)
        self.assertIn("unknown_field", {row["code"] for row in raised.exception.errors})

        nonfinite = result_payload()
        nonfinite["stages"]["quality"]["output"]["aesthetic_score"] = math.nan
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(nonfinite)
        self.assertIn("invalid_number", {row["code"] for row in raised.exception.errors})

        too_many_tags = result_payload()
        too_many_tags["stages"]["vision"]["output"]["tags"] = [
            f"tag-{index:03d}" for index in range(IMAGE_ANALYSIS_MAX_TAGS + 1)
        ]
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(too_many_tags)
        self.assertIn("array_too_large", {row["code"] for row in raised.exception.errors})

        malformed_tag = result_payload()
        malformed_tag["stages"]["vision"]["output"]["tags"] = [{"provider_payload": True}]
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(malformed_tag)
        self.assertIn("invalid_string", {row["code"] for row in raised.exception.errors})

        wrong_metadata_size = result_payload()
        wrong_metadata_size["stages"]["metadata"]["output"]["file_size"] = 999
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(wrong_metadata_size)
        self.assertIn(
            "metadata_source_size_mismatch",
            {row["code"] for row in raised.exception.errors},
        )

        wrong_combined_text = result_payload()
        wrong_combined_text["stages"]["embedding"]["output"]["combined_text_sha256"] = hashlib.sha256(
            b"wrong"
        ).hexdigest()
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(wrong_combined_text)
        self.assertIn(
            "combined_text_digest_mismatch",
            {row["code"] for row in raised.exception.errors},
        )

        missing_combined_text = result_payload()
        missing_combined_text["stages"]["embedding"]["output"]["combined_text_sha256"] = None
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(missing_combined_text)
        self.assertIn(
            "combined_text_binding_missing",
            {row["code"] for row in raised.exception.errors},
        )

    def test_source_and_analysis_binding_digests_are_closed(self) -> None:
        first = source_binding()
        reordered = dict(reversed(list(first.items())))
        self.assertEqual(source_binding_sha256(first), source_binding_sha256(reordered))
        self.assertEqual(
            analysis_binding_sha256(binding()), analysis_binding_sha256(dict(reversed(list(binding().items()))))
        )

        unsafe = source_binding()
        unsafe["relative_path"] = "private/photo.jpg"
        with self.assertRaises(ImageAnalysisContractError) as raised:
            source_binding_sha256(unsafe)
        self.assertIn("unknown_field", {row["code"] for row in raised.exception.errors})


class ImagePublicationReceiptContractTests(unittest.TestCase):
    def test_change_and_publish_receipts_bind_exact_head_without_clock(self) -> None:
        change = seal_projection_change(change_payload())
        receipt = seal_publish_receipt(publish_payload(change))

        self.assertEqual(change["change_sha256"], projection_change_sha256(change))
        self.assertEqual(receipt["publish_receipt_sha256"], publish_receipt_sha256(receipt))
        self.assertEqual(validate_projection_change(change), [])
        self.assertEqual(validate_publish_receipt(receipt), [])
        self.assertFalse({"created_at", "timestamp", "generation_id"} & set(change))
        self.assertFalse({"created_at", "timestamp", "generation_id"} & set(receipt))

        wrong_change = copy.deepcopy(change)
        wrong_change["change_position"] = 8
        self.assertIn("digest_mismatch", {row["code"] for row in validate_projection_change(wrong_change)})

        nonlinear = publish_payload(change)
        nonlinear["result_head"]["revision"] = 2
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_publish_receipt(nonlinear)
        self.assertIn("nonlinear_revision", {row["code"] for row in raised.exception.errors})

    def test_change_operation_and_publish_keys_are_closed(self) -> None:
        removal = change_payload()
        removal["operation"] = "remove"
        removal["source_binding_sha256"] = None
        self.assertEqual(validate_projection_change(seal_projection_change(removal)), [])

        invalid_removal = change_payload()
        invalid_removal["operation"] = "remove"
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_projection_change(invalid_removal)
        self.assertIn("source_binding_forbidden", {row["code"] for row in raised.exception.errors})

        change = seal_projection_change(change_payload())
        unsafe_receipt = publish_payload(change)
        unsafe_receipt["finished_at"] = "2026-08-29T00:00:00Z"
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_publish_receipt(unsafe_receipt)
        self.assertIn("unknown_field", {row["code"] for row in raised.exception.errors})

    def test_projection_receipt_outcomes_are_typed_and_digest_bound(self) -> None:
        applied = seal_projection_receipt(projection_receipt_payload())
        self.assertEqual(validate_projection_receipt(applied), [])
        self.assertEqual(applied["receipt_sha256"], projection_receipt_sha256(applied))

        blocked = projection_receipt_payload()
        blocked.update(
            {
                "source_binding_sha256": None,
                "projected_row_sha256": None,
                "outcome": "blocked",
                "reason_code": "source_unavailable",
            }
        )
        self.assertEqual(validate_projection_receipt(seal_projection_receipt(blocked)), [])

        false_success = projection_receipt_payload()
        false_success["projected_row_sha256"] = None
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_projection_receipt(false_success)
        self.assertIn("projection_binding_missing", {row["code"] for row in raised.exception.errors})

        wall_clock = projection_receipt_payload()
        wall_clock["recorded_at"] = "2026-08-29T00:00:00Z"
        self.assertRaises(ImageAnalysisContractError, seal_projection_receipt, wall_clock)


class ImageProjectionManifestContractTests(unittest.TestCase):
    def _entries(self) -> list[dict[str, object]]:
        digest_a = hashlib.sha256(b"row-a").hexdigest()
        digest_b = hashlib.sha256(b"row-b").hexdigest()
        digest_c = hashlib.sha256(b"row-c").hexdigest()
        return [
            manifest_entry("5", "blocked", expected=digest_a, actual=None, reason="source_unavailable"),
            manifest_entry("2", "missing", expected=digest_a, actual=None, reason="row_missing"),
            manifest_entry("4", "mismatched", expected=digest_a, actual=digest_b, reason="row_digest_mismatch"),
            manifest_entry("1", "matched", expected=digest_a, actual=digest_a, reason=None),
            manifest_entry("3", "unexpected", expected=None, actual=digest_c, reason="orphan_row", analysis=False),
        ]

    def _build(self, entries: list[dict[str, object]]) -> dict[str, object]:
        return build_projection_manifest(
            projector_version="memolens.image-projector/v1",
            compiler_version="memolens.image-manifest/v1",
            database_uuid=DATABASE_UUID,
            canonical_high_water_mark=91,
            entries=entries,
            alias_count=3,
            alias_set_sha256=hashlib.sha256(b"alias-set").hexdigest(),
        )

    def test_manifest_build_is_sorted_path_free_and_replay_stable(self) -> None:
        first = self._build(self._entries())
        second = self._build(list(reversed(self._entries())))

        self.assertEqual(first, second)
        self.assertEqual(validate_projection_manifest(first), [])
        self.assertEqual(first["manifest_sha256"], projection_manifest_sha256(first))
        self.assertEqual(
            [row["asset_id"] for row in first["entries"]], sorted(row["asset_id"] for row in first["entries"])
        )
        self.assertEqual(
            first["counts"],
            {"eligible": 4, "projected": 3, "aliases": 3, "missing": 1, "unexpected": 1, "mismatched": 1, "blocked": 1},
        )
        self.assertEqual(
            first["reason_counts"],
            [
                {"reason_code": "orphan_row", "count": 1},
                {"reason_code": "row_digest_mismatch", "count": 1},
                {"reason_code": "row_missing", "count": 1},
                {"reason_code": "source_unavailable", "count": 1},
            ],
        )
        encoded = canonical_projection_manifest_json(first)
        self.assertEqual(encoded, canonical_json(first))
        self.assertNotIn("created_at", encoded)
        self.assertNotIn("generation_id", encoded)
        self.assertNotIn("path", encoded.casefold())

    def test_manifest_counts_rows_reasons_and_self_digest_cannot_diverge(self) -> None:
        manifest = self._build(self._entries())

        bad_count = copy.deepcopy(manifest)
        bad_count["counts"]["missing"] = 0
        codes = {row["code"] for row in validate_projection_manifest(bad_count)}
        self.assertTrue({"manifest_count_mismatch", "digest_mismatch"} <= codes)

        bad_reason = copy.deepcopy(manifest)
        bad_reason["reason_counts"][0]["count"] = 2
        codes = {row["code"] for row in validate_projection_manifest(bad_reason)}
        self.assertTrue({"reason_count_mismatch", "digest_mismatch"} <= codes)

        bad_row_set = copy.deepcopy(manifest)
        bad_row_set["row_set_sha256"] = "f" * 64
        codes = {row["code"] for row in validate_projection_manifest(bad_row_set)}
        self.assertTrue({"row_set_digest_mismatch", "digest_mismatch"} <= codes)

        reordered = copy.deepcopy(manifest)
        reordered["entries"].reverse()
        codes = {row["code"] for row in validate_projection_manifest(reordered)}
        self.assertIn("noncanonical_order", codes)

    def test_manifest_rejects_envelope_fields_duplicate_assets_and_bad_shapes(self) -> None:
        manifest = self._build(self._entries())
        manifest["generated_at"] = "2026-08-29T00:00:00Z"
        self.assertIn("unknown_field", {row["code"] for row in validate_projection_manifest(manifest)})

        duplicate = self._entries()
        duplicate.append(copy.deepcopy(duplicate[-1]))
        with self.assertRaises(ImageAnalysisContractError) as raised:
            self._build(duplicate)
        self.assertIn("noncanonical_order", {row["code"] for row in raised.exception.errors})

        inconsistent = self._entries()
        inconsistent[0]["status"] = "matched"
        with self.assertRaises(ImageAnalysisContractError) as raised:
            self._build(inconsistent)
        self.assertIn("manifest_entry_inconsistent", {row["code"] for row in raised.exception.errors})

        with self.assertRaises(ImageAnalysisContractError) as raised:
            self._build([{"asset_id": ASSET_ID}])
        self.assertIn("required_field_missing", {row["code"] for row in raised.exception.errors})

    def test_canonical_json_rejects_nonfinite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            json.loads(canonical_json({"value": math.inf}))


if __name__ == "__main__":
    unittest.main()
