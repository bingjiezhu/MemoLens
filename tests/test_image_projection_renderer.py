from __future__ import annotations

import copy
import hashlib
import json
import unittest

from core.image_analysis_contract import ImageAnalysisContractError, seal_image_analysis_result
from core.image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    PROJECTED_IMAGE_INDEX_ROW_FIELDS,
    ImageProjectionRenderError,
    materialize_projected_image_index_row,
    materialize_projected_image_index_values,
    projected_image_index_row_sha256,
    render_projected_image_index_row,
    require_valid_projected_image_index_row,
)


ASSET_BYTES = b"canonical still bytes"
ASSET_SHA256 = hashlib.sha256(ASSET_BYTES).hexdigest()
ASSET_ID = f"asset_{ASSET_SHA256[:24]}"
RUN_ID = f"arun_{'1' * 32}"
SOURCE_ID = f"src_{'2' * 24}"
ROOT_ID = f"root_{'3' * 24}"


def _provenance(
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


def _artifact_bytes() -> dict[tuple[str, str], bytes]:
    return {
        ("metadata", "stage_output"): b"metadata-artifact",
        ("geocode", "stage_output"): b"geocode-artifact",
        ("vision", "stage_output"): b"vision-artifact",
        ("embedding", "stage_output"): b"embedding-artifact",
        ("quality", "stage_output"): b"quality-artifact",
        ("embedding", "vector:image_search"): bytes(range(12)),
        ("embedding", "vector:text_search"): bytes(range(8)),
    }


def _combined_text() -> str:
    return "山边的日落。 Tags: 山, 日落. Location: 杭州, 中国."


def _result_payload() -> dict[str, object]:
    payloads = _artifact_bytes()

    def digest(identity: tuple[str, str]) -> str:
        return hashlib.sha256(payloads[identity]).hexdigest()

    return {
        "object": "memolens.image_analysis_result",
        "schema_version": "1",
        "asset_id": ASSET_ID,
        "asset_sha256": ASSET_SHA256,
        "analysis_run_id": RUN_ID,
        "revision": 1,
        "source_binding": {
            "source_id": SOURCE_ID,
            "library_root_id": ROOT_ID,
            "observed_size": 1_234,
            "observed_mtime_ns": 1_800_000_000_000_000_000,
            "file_identity_sha256": hashlib.sha256(b"dev:inode:mode").hexdigest(),
        },
        "analysis_profile": {
            "id": "canonical-image-local-v1",
            "version": "1",
            "content_sha256": hashlib.sha256(b"profile").hexdigest(),
        },
        "stages": {
            "metadata": {
                "status": "succeeded",
                "provenance": _provenance(rule_id="exif", rule_version="1"),
                "output": {
                    "mime_type": "image/jpeg",
                    "file_size": 1_234,
                    "width": 1_920,
                    "height": 1_080,
                    "taken_at": "2026-08-01T12:30:00",
                    "latitude": 30.25,
                    "longitude": 120.15,
                    "altitude": 18.0,
                },
                "artifact_sha256": digest(("metadata", "stage_output")),
                "reason_code": None,
            },
            "geocode": {
                "status": "succeeded",
                "provenance": _provenance(rule_id="offline-geocode", rule_version="1"),
                "output": {"place_name": "杭州", "country": "中国"},
                "artifact_sha256": digest(("geocode", "stage_output")),
                "reason_code": None,
            },
            "vision": {
                "status": "succeeded",
                "provenance": _provenance(model_id="local/vision", model_version="1"),
                "output": {
                    "description": "山边的日落。",
                    "tags": ["日落", "山"],
                    "location_hint": None,
                },
                "artifact_sha256": digest(("vision", "stage_output")),
                "reason_code": None,
            },
            "embedding": {
                "status": "succeeded",
                "provenance": _provenance(rule_id="embedding-router", rule_version="1"),
                "output": {
                    "combined_text_sha256": hashlib.sha256(_combined_text().encode("utf-8")).hexdigest(),
                    "vectors": [
                        {
                            "purpose": "text_search",
                            "signal": "text_derived",
                            "model_id": "bge-m3",
                            "dimensions": 2,
                            "artifact_sha256": digest(("embedding", "vector:text_search")),
                        },
                        {
                            "purpose": "image_search",
                            "signal": "text_derived",
                            "model_id": "semantic_hash",
                            "dimensions": 3,
                            "artifact_sha256": digest(("embedding", "vector:image_search")),
                        },
                    ],
                },
                "artifact_sha256": digest(("embedding", "stage_output")),
                "reason_code": None,
            },
            "quality": {
                "status": "succeeded",
                "provenance": _provenance(model_id="local/quality", model_version="1"),
                "output": {"aesthetic_score": 0.73, "technical_quality_score": 0.81},
                "artifact_sha256": digest(("quality", "stage_output")),
                "reason_code": None,
            },
        },
    }


def _sealed_result() -> dict[str, object]:
    return seal_image_analysis_result(_result_payload())


def _artifacts() -> list[dict[str, object]]:
    payloads = _artifact_bytes()
    result = _sealed_result()
    stages = result["stages"]
    assert type(stages) is dict
    rows: list[dict[str, object]] = []
    for stage in ("metadata", "geocode", "vision", "embedding", "quality"):
        outcome = stages[stage]
        assert type(outcome) is dict
        payload = payloads[(stage, "stage_output")]
        rows.append(
            {
                "stage": stage,
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": payload,
            }
        )
    embedding = stages["embedding"]
    assert type(embedding) is dict and type(embedding["output"]) is dict
    vectors = embedding["output"]["vectors"]
    assert type(vectors) is list
    for vector in vectors:
        assert type(vector) is dict
        identity = ("embedding", f"vector:{vector['purpose']}")
        payload = payloads[identity]
        rows.append(
            {
                "stage": "embedding",
                "name": identity[1],
                "media_type": "application/vnd.memolens.float32-vector",
                "signal": vector["signal"],
                "model_id": vector["model_id"],
                "dimensions": vector["dimensions"],
                "artifact_sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": payload,
            }
        )
    return rows


def _render() -> dict[str, object]:
    return render_projected_image_index_row(
        result=_sealed_result(),
        source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
        artifacts=_artifacts(),
    )


class ImageProjectionRendererTests(unittest.TestCase):
    def test_render_is_closed_json_safe_and_replay_deterministic(self) -> None:
        first = _render()
        reordered = list(reversed(_artifacts()))
        second = render_projected_image_index_row(
            result=_sealed_result(),
            source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
            artifacts=reordered,
        )

        self.assertEqual(first, second)
        json.dumps(first, ensure_ascii=False, allow_nan=False)
        self.assertEqual(set(first["row"]), set(PROJECTED_IMAGE_INDEX_ROW_FIELDS))
        self.assertEqual(first["asset_id"], ASSET_ID)
        self.assertEqual(first["row"]["id"], ASSET_ID)
        self.assertEqual(first["row"]["sha256"], ASSET_SHA256)
        self.assertEqual(first["row"]["combined_text"], _combined_text())
        self.assertEqual(first["row"]["tags_json"], '["山","日落"]')
        require_valid_projected_image_index_row(first)

    def test_text_derived_image_vector_remains_explicit_and_is_never_labeled_visual(self) -> None:
        document = _render()

        binding = document["vector_bindings"]["image_search"]
        self.assertEqual(binding["signal"], "text_derived")
        self.assertEqual(binding["model_id"], "semantic_hash")
        self.assertEqual(binding["backend"], "text_derived:semantic_hash")
        self.assertEqual(document["row"]["embedding_backend"], "text_derived:semantic_hash")

        artifacts = _artifacts()
        image_vector = next(row for row in artifacts if row["name"] == "vector:image_search")
        image_vector["signal"] = "visual"
        with self.assertRaisesRegex(ImageProjectionRenderError, "image_projection_artifact_mismatch"):
            render_projected_image_index_row(
                result=_sealed_result(),
                source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
                artifacts=artifacts,
            )

    def test_missing_image_search_vector_is_a_stable_block_not_a_placeholder(self) -> None:
        payload = _result_payload()
        payload["stages"]["embedding"]["output"]["vectors"] = [payload["stages"]["embedding"]["output"]["vectors"][0]]
        result = seal_image_analysis_result(payload)

        with self.assertRaises(ImageProjectionRenderError) as raised:
            render_projected_image_index_row(
                result=result,
                source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
                artifacts=_artifacts(),
            )
        self.assertEqual(raised.exception.code, "image_projection_image_search_vector_missing")

    def test_artifacts_are_an_exact_byte_digest_and_metadata_set(self) -> None:
        mutations: list[tuple[str, list[dict[str, object]], str]] = []
        changed_bytes = _artifacts()
        changed_bytes[-1]["bytes"] = b"12345678"
        mutations.append(("bytes", changed_bytes, "image_projection_artifact_mismatch"))
        changed_model = _artifacts()
        changed_model[-1]["model_id"] = "another-model"
        mutations.append(("model", changed_model, "image_projection_artifact_mismatch"))
        missing = _artifacts()[:-1]
        mutations.append(("missing", missing, "image_projection_artifact_set_incomplete"))
        extra = _artifacts()
        extra.append(copy.deepcopy(extra[-1]))
        extra[-1]["name"] = "vector:extra"
        mutations.append(("extra", extra, "image_projection_artifact_set_invalid"))

        for label, artifacts, code in mutations:
            with self.subTest(label=label), self.assertRaises(ImageProjectionRenderError) as raised:
                render_projected_image_index_row(
                    result=_sealed_result(),
                    source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
                    artifacts=artifacts,
                )
            self.assertEqual(raised.exception.code, code)

    def test_vector_byte_length_must_match_declared_float32_dimensions(self) -> None:
        payload = _result_payload()
        short = b"short"
        image = next(
            vector
            for vector in payload["stages"]["embedding"]["output"]["vectors"]
            if vector["purpose"] == "image_search"
        )
        image["artifact_sha256"] = hashlib.sha256(short).hexdigest()
        result = seal_image_analysis_result(payload)
        artifacts = _artifacts()
        artifact = next(row for row in artifacts if row["name"] == "vector:image_search")
        artifact["bytes"] = short
        artifact["artifact_sha256"] = hashlib.sha256(short).hexdigest()

        with self.assertRaises(ImageProjectionRenderError) as raised:
            render_projected_image_index_row(
                result=result,
                source_projection={"filename": "sunset.jpg", "relative_path": "trips/2026/sunset.jpg"},
                artifacts=artifacts,
            )
        self.assertEqual(raised.exception.code, "image_projection_vector_shape_mismatch")

    def test_combined_text_is_rejected_before_a_result_can_be_sealed(self) -> None:
        payload = _result_payload()
        payload["stages"]["embedding"]["output"]["combined_text_sha256"] = hashlib.sha256(b"wrong").hexdigest()
        with self.assertRaises(ImageAnalysisContractError) as raised:
            seal_image_analysis_result(payload)
        self.assertIn("combined_text_digest_mismatch", {row["code"] for row in raised.exception.errors})

    def test_source_projection_is_closed_relative_and_filename_bound(self) -> None:
        bad_sources = (
            {"filename": "sunset.jpg", "relative_path": "/private/sunset.jpg"},
            {"filename": "sunset.jpg", "relative_path": "../sunset.jpg"},
            {"filename": "other.jpg", "relative_path": "trips/sunset.jpg"},
            {"filename": "sunset.jpg", "relative_path": "trips/sunset.jpg", "root": "/private"},
        )
        for source in bad_sources:
            with self.subTest(source=source), self.assertRaises(ImageProjectionRenderError) as raised:
                render_projected_image_index_row(
                    result=_sealed_result(),
                    source_projection=source,
                    artifacts=_artifacts(),
                )
            self.assertEqual(raised.exception.code, "image_projection_source_projection_invalid")

    def test_row_digest_excludes_created_and_updated_envelope_only(self) -> None:
        document = _render()
        row = copy.deepcopy(document["row"])
        initial = projected_image_index_row_sha256(row)
        row["created_at"] = "2026-08-29T01:00:00Z"
        row["updated_at"] = "2026-08-29T02:00:00Z"
        self.assertEqual(projected_image_index_row_sha256(row), initial)
        row["relative_path"] = "another/sunset.jpg"
        self.assertNotEqual(projected_image_index_row_sha256(row), initial)

    def test_materialization_adds_only_wall_clock_envelope_and_decodes_blobs(self) -> None:
        document = _render()
        original = copy.deepcopy(document)
        materialized = materialize_projected_image_index_row(
            document,
            created_at="2026-08-29T01:00:00Z",
            updated_at="2026-08-29T02:00:00Z",
        )
        values = materialize_projected_image_index_values(
            document,
            created_at="2026-08-29T01:00:00Z",
            updated_at="2026-08-29T02:00:00Z",
        )

        self.assertEqual(set(materialized), set(IMAGE_INDEX_SQL_COLUMNS))
        self.assertEqual(materialized["created_at"], "2026-08-29T01:00:00Z")
        self.assertEqual(materialized["updated_at"], "2026-08-29T02:00:00Z")
        self.assertEqual(materialized["embedding"], _artifact_bytes()[("embedding", "vector:image_search")])
        self.assertEqual(
            materialized["combined_text_embedding"],
            _artifact_bytes()[("embedding", "vector:text_search")],
        )
        self.assertEqual(values, tuple(materialized[column] for column in IMAGE_INDEX_SQL_COLUMNS))
        self.assertEqual(document, original)

    def test_tampered_projected_row_is_rejected_before_materialization(self) -> None:
        document = _render()
        document["row"]["description"] = "tampered"

        with self.assertRaises(ImageProjectionRenderError) as raised:
            materialize_projected_image_index_row(
                document,
                created_at="2026-08-29T01:00:00Z",
                updated_at="2026-08-29T02:00:00Z",
            )
        self.assertEqual(raised.exception.code, "image_projection_row_digest_mismatch")


if __name__ == "__main__":
    unittest.main()
