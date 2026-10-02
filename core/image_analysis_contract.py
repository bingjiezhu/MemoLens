"""Closed deterministic contracts for canonical image publication.

The values in this module are domain content.  Persistence envelopes may add
wall-clock metadata next to them, but clocks, paths, generation nonces and
provider payloads are deliberately absent from every closed document below.

Each sealed document carries a self digest.  The digest covers the canonical
document with only that self-digest field removed; this avoids recursion while
still making every other field part of the immutable identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Callable


IMAGE_ANALYSIS_RESULT_OBJECT = "memolens.image_analysis_result"
IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION = "1"
IMAGE_PROJECTION_CHANGE_OBJECT = "memolens.image_projection_change"
IMAGE_PROJECTION_CHANGE_SCHEMA_VERSION = "1"
IMAGE_PUBLISH_RECEIPT_OBJECT = "memolens.image_publish_receipt"
IMAGE_PUBLISH_RECEIPT_SCHEMA_VERSION = "1"
IMAGE_PROJECTION_RECEIPT_OBJECT = "memolens.image_projection_receipt"
IMAGE_PROJECTION_RECEIPT_SCHEMA_VERSION = "1"
IMAGE_PROJECTION_MANIFEST_OBJECT = "memolens.image_projection_manifest"
IMAGE_PROJECTION_MANIFEST_SCHEMA_VERSION = "1"
IMAGE_PROJECTION_CONTRACT = "legacy-image-index-shadow/v1"

IMAGE_ANALYSIS_STAGES = (
    "metadata",
    "geocode",
    "vision",
    "embedding",
    "quality",
)
IMAGE_ANALYSIS_STAGE_STATUSES = frozenset({"succeeded", "partial", "unsupported", "disabled", "failed", "unknown"})
IMAGE_PROJECTION_OUTCOMES = frozenset({"applied", "no_change", "superseded", "removed", "blocked"})
IMAGE_MANIFEST_ENTRY_STATUSES = frozenset({"matched", "missing", "unexpected", "mismatched", "blocked"})

IMAGE_ANALYSIS_MAX_CANONICAL_BYTES = 1_048_576
IMAGE_RECEIPT_MAX_CANONICAL_BYTES = 65_536
IMAGE_MANIFEST_MAX_CANONICAL_BYTES = 16_777_216
IMAGE_MANIFEST_MAX_ENTRIES = 100_000
IMAGE_ANALYSIS_MAX_TAGS = 128
IMAGE_ANALYSIS_MAX_VECTORS = 4
IMAGE_CONTRACT_MAX_ERRORS = 64
IMAGE_CONTRACT_MAX_DEPTH = 24
IMAGE_CONTRACT_MAX_NODES = 500_000

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ROOT_ID = re.compile(r"^root_[0-9a-f]{24}$")
_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_DATABASE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MIME_TYPE = re.compile(r"^image/[A-Za-z0-9.+-]{1,100}$")


def build_canonical_image_combined_text(
    *,
    vision_output: Mapping[str, object] | None,
    geocode_output: Mapping[str, object] | None,
) -> str:
    """Build the frozen v1 text used by text-derived image embeddings.

    The function is deliberately pure and tolerant of absent stage output so
    the Result validator and the projection renderer share one byte-exact
    algorithm.  The surrounding Result contract validates the input fields.
    """

    description_value = vision_output.get("description") if vision_output is not None else None
    description = description_value.strip() if type(description_value) is str else ""
    tags_value = vision_output.get("tags") if vision_output is not None else None
    tags = (
        sorted({tag.strip() for tag in tags_value if type(tag) is str and tag.strip()})
        if type(tags_value) is list
        else []
    )
    place_name_value = geocode_output.get("place_name") if geocode_output is not None else None
    country_value = geocode_output.get("country") if geocode_output is not None else None
    place_name = place_name_value.strip() if type(place_name_value) is str and place_name_value.strip() else None
    country = country_value.strip() if type(country_value) is str and country_value.strip() else None
    location_hint_value = vision_output.get("location_hint") if vision_output is not None else None
    location_hint = location_hint_value.strip() if type(location_hint_value) is str else ""

    parts: list[str] = []
    if description:
        parts.append(description)
    if tags:
        parts.append(f"Tags: {', '.join(tags)}.")
    location = [part for part in (place_name, country) if part is not None]
    if location:
        parts.append(f"Location: {', '.join(location)}.")
    elif location_hint:
        parts.append(f"Possible location or landmark: {location_hint}.")
        parts.append(f"Location hint keywords: {location_hint}.")
    return " ".join(parts)


@dataclass
class ImageAnalysisContractError(ValueError):
    """Raised when a canonical image document is invalid."""

    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_image_contract"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Image contract is invalid."),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Image contract is invalid.",
        )


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) < IMAGE_CONTRACT_MAX_ERRORS:
            self.items.append({"code": code, "path": path, "message": message})


def canonical_json(value: object) -> str:
    """Return the repository-wide stable JSON encoding used for A0.1 digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _closed(
    value: object,
    *,
    path: str,
    fields: set[str],
    issues: _Issues,
) -> dict[str, Any] | None:
    if type(value) is not dict:
        issues.add("schema_type", path, "Value must be an object.")
        return None
    if any(type(key) is not str for key in value):
        issues.add("invalid_object_key", path, "Object keys must be strings.")
        return None
    row = value
    missing = fields - set(row)
    unknown = set(row) - fields
    for field in sorted(missing):
        issues.add("required_field_missing", f"{path}/{field}", "A required field is missing.")
    if unknown:
        issues.add("unknown_field", f"{path}/*", "Object contains an unsupported field.")
    return row


def _text(
    value: object,
    path: str,
    issues: _Issues,
    *,
    minimum: int = 1,
    maximum: int = 200,
    pattern: re.Pattern[str] | None = None,
    code: str = "invalid_string",
) -> bool:
    if type(value) is not str or not minimum <= len(value) <= maximum:
        issues.add(code, path, "Value must be a bounded string.")
        return False
    if any((ord(char) < 0x20 and char not in {"\t", "\n", "\r"}) or ord(char) == 0x7F for char in value):
        issues.add(code, path, "String contains a forbidden control character.")
        return False
    if pattern is not None and pattern.fullmatch(value) is None:
        issues.add(code, path, "String does not satisfy its closed format.")
        return False
    return True


def _nullable_text(
    value: object,
    path: str,
    issues: _Issues,
    *,
    maximum: int,
    minimum: int = 1,
) -> bool:
    return value is None or _text(value, path, issues, minimum=minimum, maximum=maximum)


def _enum(value: object, path: str, allowed: frozenset[str], issues: _Issues) -> bool:
    if type(value) is not str or value not in allowed:
        issues.add("invalid_enum", path, "Value is outside the closed registry.")
        return False
    return True


def _integer(
    value: object,
    path: str,
    issues: _Issues,
    *,
    minimum: int,
    maximum: int,
    nullable: bool = False,
) -> bool:
    if nullable and value is None:
        return True
    if type(value) is not int or not minimum <= value <= maximum:
        issues.add("invalid_integer", path, "Value must be a bounded integer.")
        return False
    return True


def _finite_float(
    value: object,
    path: str,
    issues: _Issues,
    *,
    minimum: float,
    maximum: float,
    nullable: bool = False,
) -> bool:
    if nullable and value is None:
        return True
    if type(value) is not float or not math.isfinite(value) or not minimum <= value <= maximum:
        issues.add("invalid_number", path, "Value must be a bounded finite number.")
        return False
    return True


def _sha(value: object, path: str, issues: _Issues, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return _text(value, path, issues, minimum=64, maximum=64, pattern=_SHA256, code="invalid_sha256")


def _resource_id(value: object, path: str, pattern: re.Pattern[str], issues: _Issues) -> bool:
    return _text(value, path, issues, pattern=pattern, code="invalid_identifier")


def _token(value: object, path: str, issues: _Issues, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return _text(value, path, issues, pattern=_TOKEN, code="invalid_token")


def _reason(value: object, path: str, issues: _Issues, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    return _text(value, path, issues, pattern=_REASON_CODE, code="invalid_reason_code")


def _validate_resource_bounds(value: object, *, maximum_bytes: int, issues: _Issues) -> None:
    nodes = 0
    active: set[int] = set()
    exhausted = False

    def visit(current: object, path: str, depth: int) -> None:
        nonlocal exhausted, nodes
        if exhausted:
            return
        nodes += 1
        if nodes > IMAGE_CONTRACT_MAX_NODES:
            issues.add("resource_too_large", path, "Document exceeds the node limit.")
            exhausted = True
            return
        if depth > IMAGE_CONTRACT_MAX_DEPTH:
            issues.add("resource_too_deep", path, "Document exceeds the nesting limit.")
            return
        if type(current) is float and not math.isfinite(current):
            issues.add("invalid_number", path, "Non-finite numbers are forbidden.")
            return
        if type(current) in {dict, list}:
            identity = id(current)
            if identity in active:
                issues.add("cyclic_value", path, "Cyclic values are forbidden.")
                return
            active.add(identity)
            if type(current) is dict:
                for key, child in current.items():
                    visit(child, f"{path}/{key}", depth + 1)
            else:
                for index, child in enumerate(current):
                    visit(child, f"{path}/{index}", depth + 1)
            active.remove(identity)
        elif current is not None and type(current) not in {str, int, float, bool}:
            issues.add("invalid_json_type", path, "Value is not a built-in JSON type.")

    visit(value, "", 0)
    if exhausted:
        return
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        issues.add("invalid_json", "", "Document cannot be represented as canonical JSON.")
        return
    if len(encoded) > maximum_bytes:
        issues.add("document_too_large", "", "Document exceeds its canonical byte limit.")


def _validate_database_uuid(value: object, path: str, issues: _Issues) -> bool:
    return _text(value, path, issues, minimum=36, maximum=36, pattern=_DATABASE_UUID, code="invalid_database_uuid")


def _validate_analysis_binding(value: object, path: str, issues: _Issues, *, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    row = _closed(
        value,
        path=path,
        fields={"analysis_run_id", "revision", "content_sha256"},
        issues=issues,
    )
    if row is None:
        return False
    valid = _resource_id(row.get("analysis_run_id"), f"{path}/analysis_run_id", _ANALYSIS_RUN_ID, issues)
    valid = _integer(row.get("revision"), f"{path}/revision", issues, minimum=1, maximum=1_800_000) and valid
    valid = _sha(row.get("content_sha256"), f"{path}/content_sha256", issues) and valid
    return valid


def _validate_source_binding(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(
        value,
        path=path,
        fields={
            "source_id",
            "library_root_id",
            "observed_size",
            "observed_mtime_ns",
            "file_identity_sha256",
        },
        issues=issues,
    )
    if row is None:
        return False
    valid = _resource_id(row.get("source_id"), f"{path}/source_id", _SOURCE_ID, issues)
    valid = _resource_id(row.get("library_root_id"), f"{path}/library_root_id", _ROOT_ID, issues) and valid
    valid = (
        _integer(row.get("observed_size"), f"{path}/observed_size", issues, minimum=1, maximum=8_589_934_592) and valid
    )
    valid = (
        _integer(
            row.get("observed_mtime_ns"),
            f"{path}/observed_mtime_ns",
            issues,
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        and valid
    )
    valid = _sha(row.get("file_identity_sha256"), f"{path}/file_identity_sha256", issues) and valid
    return valid


def _validate_profile(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields={"id", "version", "content_sha256"}, issues=issues)
    if row is None:
        return False
    valid = _token(row.get("id"), f"{path}/id", issues)
    valid = _token(row.get("version"), f"{path}/version", issues) and valid
    valid = _sha(row.get("content_sha256"), f"{path}/content_sha256", issues) and valid
    return valid


def _validate_provenance(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(
        value,
        path=path,
        fields={
            "producer_id",
            "producer_version",
            "model_id",
            "model_version",
            "rule_id",
            "rule_version",
        },
        issues=issues,
    )
    if row is None:
        return False
    valid = _token(row.get("producer_id"), f"{path}/producer_id", issues)
    valid = _token(row.get("producer_version"), f"{path}/producer_version", issues) and valid
    valid = _token(row.get("model_id"), f"{path}/model_id", issues, nullable=True) and valid
    valid = _token(row.get("model_version"), f"{path}/model_version", issues, nullable=True) and valid
    valid = _token(row.get("rule_id"), f"{path}/rule_id", issues, nullable=True) and valid
    valid = _token(row.get("rule_version"), f"{path}/rule_version", issues, nullable=True) and valid
    if (row.get("model_id") is None) != (row.get("model_version") is None):
        issues.add("incomplete_model_provenance", path, "Model ID and version must be both present or both null.")
        valid = False
    if (row.get("rule_id") is None) != (row.get("rule_version") is None):
        issues.add("incomplete_rule_provenance", path, "Rule ID and version must be both present or both null.")
        valid = False
    return valid


def _validate_metadata_output(value: object, path: str, status: object, issues: _Issues) -> None:
    row = _closed(
        value,
        path=path,
        fields={"mime_type", "file_size", "width", "height", "taken_at", "latitude", "longitude", "altitude"},
        issues=issues,
    )
    if row is None:
        return
    mime_valid = row.get("mime_type") is None or _text(
        row.get("mime_type"), f"{path}/mime_type", issues, pattern=_MIME_TYPE, maximum=106, code="invalid_mime_type"
    )
    size_valid = _integer(
        row.get("file_size"), f"{path}/file_size", issues, minimum=1, maximum=8_589_934_592, nullable=True
    )
    width_valid = _integer(row.get("width"), f"{path}/width", issues, minimum=1, maximum=100_000, nullable=True)
    height_valid = _integer(row.get("height"), f"{path}/height", issues, minimum=1, maximum=100_000, nullable=True)
    _nullable_text(row.get("taken_at"), f"{path}/taken_at", issues, maximum=128)
    latitude_valid = _finite_float(
        row.get("latitude"), f"{path}/latitude", issues, minimum=-90.0, maximum=90.0, nullable=True
    )
    longitude_valid = _finite_float(
        row.get("longitude"), f"{path}/longitude", issues, minimum=-180.0, maximum=180.0, nullable=True
    )
    _finite_float(
        row.get("altitude"), f"{path}/altitude", issues, minimum=-20_000.0, maximum=1_000_000.0, nullable=True
    )
    if (row.get("latitude") is None) != (row.get("longitude") is None):
        issues.add("incomplete_coordinates", path, "Latitude and longitude must be both present or both null.")
    if status == "succeeded" and not all(
        (
            mime_valid,
            size_valid,
            width_valid,
            height_valid,
            row.get("mime_type") is not None,
            row.get("file_size") is not None,
            row.get("width") is not None,
            row.get("height") is not None,
            latitude_valid,
            longitude_valid,
        )
    ):
        issues.add("incomplete_stage_output", path, "Succeeded metadata must contain the probed image identity.")


def _validate_geocode_output(value: object, path: str, status: object, issues: _Issues) -> None:
    row = _closed(value, path=path, fields={"place_name", "country"}, issues=issues)
    if row is None:
        return
    _nullable_text(row.get("place_name"), f"{path}/place_name", issues, maximum=512)
    _nullable_text(row.get("country"), f"{path}/country", issues, maximum=256)
    if status == "succeeded" and row.get("place_name") is None and row.get("country") is None:
        issues.add("empty_stage_output", path, "Succeeded geocode output must contain a location value.")


def _validate_vision_output(value: object, path: str, status: object, issues: _Issues) -> None:
    row = _closed(value, path=path, fields={"description", "tags", "location_hint"}, issues=issues)
    if row is None:
        return
    _nullable_text(row.get("description"), f"{path}/description", issues, maximum=32_768, minimum=0)
    tags = row.get("tags")
    if type(tags) is not list:
        issues.add("schema_type", f"{path}/tags", "Tags must be an array.")
        tags = []
    elif len(tags) > IMAGE_ANALYSIS_MAX_TAGS:
        issues.add("array_too_large", f"{path}/tags", "Tags exceed the item limit.")
    if type(tags) is list:
        for index, tag in enumerate(tags[: IMAGE_ANALYSIS_MAX_TAGS + 1]):
            _text(tag, f"{path}/tags/{index}", issues, maximum=256)
        if all(type(tag) is str for tag in tags) and tags != sorted(set(tags)):
            issues.add("noncanonical_order", f"{path}/tags", "Tags must be unique and sorted.")
    _nullable_text(row.get("location_hint"), f"{path}/location_hint", issues, maximum=2_048)
    if status == "succeeded" and not (row.get("description") or tags or row.get("location_hint")):
        issues.add("empty_stage_output", path, "Succeeded vision output must contain evidence.")


def _validate_embedding_output(value: object, path: str, status: object, issues: _Issues) -> None:
    row = _closed(value, path=path, fields={"combined_text_sha256", "vectors"}, issues=issues)
    if row is None:
        return
    _sha(row.get("combined_text_sha256"), f"{path}/combined_text_sha256", issues, nullable=True)
    vectors = row.get("vectors")
    if type(vectors) is not list:
        issues.add("schema_type", f"{path}/vectors", "Embedding vectors must be an array.")
        return
    if len(vectors) > IMAGE_ANALYSIS_MAX_VECTORS:
        issues.add("array_too_large", f"{path}/vectors", "Embedding vectors exceed the item limit.")
    purposes: list[str] = []
    for index, value_row in enumerate(vectors[: IMAGE_ANALYSIS_MAX_VECTORS + 1]):
        vector = _closed(
            value_row,
            path=f"{path}/vectors/{index}",
            fields={"purpose", "signal", "model_id", "dimensions", "artifact_sha256"},
            issues=issues,
        )
        if vector is None:
            continue
        _enum(
            vector.get("purpose"), f"{path}/vectors/{index}/purpose", frozenset({"image_search", "text_search"}), issues
        )
        _enum(vector.get("signal"), f"{path}/vectors/{index}/signal", frozenset({"visual", "text_derived"}), issues)
        _token(vector.get("model_id"), f"{path}/vectors/{index}/model_id", issues)
        _integer(vector.get("dimensions"), f"{path}/vectors/{index}/dimensions", issues, minimum=1, maximum=65_536)
        _sha(vector.get("artifact_sha256"), f"{path}/vectors/{index}/artifact_sha256", issues)
        if type(vector.get("purpose")) is str:
            purposes.append(vector["purpose"])
    if purposes != sorted(set(purposes)):
        issues.add("noncanonical_order", f"{path}/vectors", "Embedding purposes must be unique and sorted.")
    if status == "succeeded" and not vectors:
        issues.add("empty_stage_output", path, "Succeeded embedding output must contain a vector artifact.")


def _validate_quality_output(value: object, path: str, status: object, issues: _Issues) -> None:
    row = _closed(value, path=path, fields={"aesthetic_score", "technical_quality_score"}, issues=issues)
    if row is None:
        return
    _finite_float(
        row.get("aesthetic_score"), f"{path}/aesthetic_score", issues, minimum=0.0, maximum=1.0, nullable=True
    )
    _finite_float(
        row.get("technical_quality_score"),
        f"{path}/technical_quality_score",
        issues,
        minimum=0.0,
        maximum=1.0,
        nullable=True,
    )
    if status == "succeeded" and row.get("aesthetic_score") is None:
        issues.add("empty_stage_output", path, "Succeeded quality output must contain an aesthetic score.")


_STAGE_OUTPUT_VALIDATORS: dict[str, Callable[[object, str, object, _Issues], None]] = {
    "metadata": _validate_metadata_output,
    "geocode": _validate_geocode_output,
    "vision": _validate_vision_output,
    "embedding": _validate_embedding_output,
    "quality": _validate_quality_output,
}


def _validate_stage_outcome(stage: str, value: object, path: str, issues: _Issues) -> None:
    row = _closed(
        value,
        path=path,
        fields={"status", "provenance", "output", "artifact_sha256", "reason_code"},
        issues=issues,
    )
    if row is None:
        return
    status = row.get("status")
    _enum(status, f"{path}/status", IMAGE_ANALYSIS_STAGE_STATUSES, issues)
    _validate_provenance(row.get("provenance"), f"{path}/provenance", issues)
    _sha(row.get("artifact_sha256"), f"{path}/artifact_sha256", issues, nullable=True)
    _reason(row.get("reason_code"), f"{path}/reason_code", issues, nullable=True)
    output = row.get("output")
    if status in {"succeeded", "partial"}:
        if output is None:
            issues.add("stage_output_missing", f"{path}/output", "Successful or partial stages require typed output.")
        else:
            _STAGE_OUTPUT_VALIDATORS[stage](output, f"{path}/output", status, issues)
    elif output is not None:
        issues.add("stage_output_forbidden", f"{path}/output", "Non-producing stage outcomes cannot carry output.")
    if status == "succeeded" and row.get("reason_code") is not None:
        issues.add("unexpected_reason", f"{path}/reason_code", "Succeeded stage cannot carry a reason code.")
    if status != "succeeded" and row.get("reason_code") is None:
        issues.add("reason_required", f"{path}/reason_code", "Non-succeeded stage requires a stable reason code.")


def _normalized(value: object, kind: str, digest_field: str | None = None) -> object:
    copied = deepcopy(value)
    if type(copied) is dict and digest_field is not None:
        copied.pop(digest_field, None)
    if kind == "result" and type(copied) is dict:
        stages = copied.get("stages")
        if type(stages) is dict:
            vision = stages.get("vision")
            if (
                type(vision) is dict
                and type(vision.get("output")) is dict
                and type(vision["output"].get("tags")) is list
            ):
                tags = vision["output"]["tags"]
                if all(type(tag) is str for tag in tags):
                    vision["output"]["tags"] = sorted(set(tags))
            embedding = stages.get("embedding")
            if (
                type(embedding) is dict
                and type(embedding.get("output")) is dict
                and type(embedding["output"].get("vectors")) is list
            ):
                vectors = embedding["output"]["vectors"]
                if all(type(row) is dict and type(row.get("purpose")) is str for row in vectors):
                    embedding["output"]["vectors"] = sorted(vectors, key=lambda row: row["purpose"])
    if kind == "manifest" and type(copied) is dict:
        entries = copied.get("entries")
        if type(entries) is list and all(type(row) is dict and type(row.get("asset_id")) is str for row in entries):
            copied["entries"] = sorted(entries, key=lambda row: row["asset_id"])
        reason_counts = copied.get("reason_counts")
        if type(reason_counts) is list and all(
            type(row) is dict and type(row.get("reason_code")) is str for row in reason_counts
        ):
            copied["reason_counts"] = sorted(reason_counts, key=lambda row: row["reason_code"])
    return copied


def _validate_digest(
    root: dict[str, Any],
    *,
    field: str,
    kind: str,
    issues: _Issues,
    required: bool,
) -> None:
    if required:
        if _sha(root.get(field), f"/{field}", issues):
            expected = canonical_sha256(_normalized(root, kind, field))
            if root.get(field) != expected:
                issues.add("digest_mismatch", f"/{field}", "Stored digest does not match canonical content.")
    elif field in root:
        issues.add("unexpected_digest", f"/{field}", "Unsealed content must not carry its self digest.")


def _validate_result(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = {
        "object",
        "schema_version",
        "asset_id",
        "asset_sha256",
        "analysis_run_id",
        "revision",
        "source_binding",
        "analysis_profile",
        "stages",
    }
    if sealed:
        fields.add("content_sha256")
    root = _closed(value, path="", fields=fields, issues=issues)
    if root is None:
        _validate_resource_bounds(value, maximum_bytes=IMAGE_ANALYSIS_MAX_CANONICAL_BYTES, issues=issues)
        return issues.items
    if root.get("object") != IMAGE_ANALYSIS_RESULT_OBJECT:
        issues.add("invalid_object", "/object", "Image Analysis Result object is invalid.")
    if root.get("schema_version") != IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Image Analysis Result schema is unsupported.")
    asset_valid = _resource_id(root.get("asset_id"), "/asset_id", _ASSET_ID, issues)
    sha_valid = _sha(root.get("asset_sha256"), "/asset_sha256", issues)
    if asset_valid and sha_valid and root["asset_id"] != f"asset_{root['asset_sha256'][:24]}":
        issues.add("asset_identity_mismatch", "/asset_id", "Asset ID must match the content digest.")
    _resource_id(root.get("analysis_run_id"), "/analysis_run_id", _ANALYSIS_RUN_ID, issues)
    _integer(root.get("revision"), "/revision", issues, minimum=1, maximum=1_800_000)
    _validate_source_binding(root.get("source_binding"), "/source_binding", issues)
    _validate_profile(root.get("analysis_profile"), "/analysis_profile", issues)
    stages = _closed(root.get("stages"), path="/stages", fields=set(IMAGE_ANALYSIS_STAGES), issues=issues)
    if stages is not None:
        for stage in IMAGE_ANALYSIS_STAGES:
            _validate_stage_outcome(stage, stages.get(stage), f"/stages/{stage}", issues)
        source = root.get("source_binding")
        metadata = stages.get("metadata")
        if (
            type(source) is dict
            and type(metadata) is dict
            and metadata.get("status") == "succeeded"
            and type(metadata.get("output")) is dict
            and metadata["output"].get("file_size") != source.get("observed_size")
        ):
            issues.add(
                "metadata_source_size_mismatch",
                "/stages/metadata/output/file_size",
                "Succeeded metadata size must equal the exact admitted source size.",
            )
        vision = stages.get("vision")
        geocode = stages.get("geocode")
        embedding = stages.get("embedding")
        vision_output = (
            vision.get("output")
            if type(vision) is dict
            and vision.get("status") in {"succeeded", "partial"}
            and type(vision.get("output")) is dict
            else None
        )
        geocode_output = (
            geocode.get("output")
            if type(geocode) is dict
            and geocode.get("status") in {"succeeded", "partial"}
            and type(geocode.get("output")) is dict
            else None
        )
        embedding_output = (
            embedding.get("output")
            if type(embedding) is dict
            and embedding.get("status") in {"succeeded", "partial"}
            and type(embedding.get("output")) is dict
            else None
        )
        if type(embedding_output) is dict:
            vectors = embedding_output.get("vectors")
            text_bound = type(vectors) is list and any(
                type(vector) is dict
                and (
                    vector.get("purpose") == "text_search"
                    or (
                        vector.get("purpose") == "image_search"
                        and vector.get("signal") == "text_derived"
                    )
                )
                for vector in vectors
            )
            combined_text_digest = embedding_output.get("combined_text_sha256")
            if text_bound and combined_text_digest is None:
                issues.add(
                    "combined_text_binding_missing",
                    "/stages/embedding/output/combined_text_sha256",
                    "Text-derived embeddings require the canonical combined-text digest.",
                )
            if type(combined_text_digest) is str and _SHA256.fullmatch(combined_text_digest):
                combined_text = build_canonical_image_combined_text(
                    vision_output=vision_output,
                    geocode_output=geocode_output,
                )
                expected_digest = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()
                if combined_text_digest != expected_digest:
                    issues.add(
                        "combined_text_digest_mismatch",
                        "/stages/embedding/output/combined_text_sha256",
                        "Embedding text digest must match the frozen combined-text rendering.",
                    )
    _validate_digest(root, field="content_sha256", kind="result", issues=issues, required=sealed)
    _validate_resource_bounds(value, maximum_bytes=IMAGE_ANALYSIS_MAX_CANONICAL_BYTES, issues=issues)
    return issues.items


def _validate_change(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = {
        "object",
        "schema_version",
        "projection_contract",
        "database_uuid",
        "change_position",
        "asset_id",
        "analysis_binding",
        "source_binding_sha256",
        "operation",
    }
    if sealed:
        fields.add("change_sha256")
    root = _closed(value, path="", fields=fields, issues=issues)
    if root is not None:
        if root.get("object") != IMAGE_PROJECTION_CHANGE_OBJECT:
            issues.add("invalid_object", "/object", "Image Projection Change object is invalid.")
        if root.get("schema_version") != IMAGE_PROJECTION_CHANGE_SCHEMA_VERSION:
            issues.add(
                "unsupported_schema_version", "/schema_version", "Image Projection Change schema is unsupported."
            )
        if root.get("projection_contract") != IMAGE_PROJECTION_CONTRACT:
            issues.add("unsupported_projection_contract", "/projection_contract", "Projection contract is unsupported.")
        _validate_database_uuid(root.get("database_uuid"), "/database_uuid", issues)
        _integer(root.get("change_position"), "/change_position", issues, minimum=1, maximum=9_223_372_036_854_775_807)
        _resource_id(root.get("asset_id"), "/asset_id", _ASSET_ID, issues)
        _validate_analysis_binding(root.get("analysis_binding"), "/analysis_binding", issues)
        _sha(root.get("source_binding_sha256"), "/source_binding_sha256", issues, nullable=True)
        _enum(root.get("operation"), "/operation", frozenset({"upsert", "remove"}), issues)
        if root.get("operation") == "upsert" and root.get("source_binding_sha256") is None:
            issues.add(
                "source_binding_required", "/source_binding_sha256", "Upsert change requires exact source binding."
            )
        if root.get("operation") == "remove" and root.get("source_binding_sha256") is not None:
            issues.add(
                "source_binding_forbidden", "/source_binding_sha256", "Removal change cannot claim an available source."
            )
        _validate_digest(root, field="change_sha256", kind="change", issues=issues, required=sealed)
    _validate_resource_bounds(value, maximum_bytes=IMAGE_RECEIPT_MAX_CANONICAL_BYTES, issues=issues)
    return issues.items


def _validate_publish_receipt(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = {
        "object",
        "schema_version",
        "database_uuid",
        "job_id",
        "request_sha256",
        "asset_id",
        "source_binding_sha256",
        "expected_prior_head",
        "result_head",
        "change_position",
        "change_sha256",
    }
    if sealed:
        fields.add("publish_receipt_sha256")
    root = _closed(value, path="", fields=fields, issues=issues)
    if root is not None:
        if root.get("object") != IMAGE_PUBLISH_RECEIPT_OBJECT:
            issues.add("invalid_object", "/object", "Image Publish Receipt object is invalid.")
        if root.get("schema_version") != IMAGE_PUBLISH_RECEIPT_SCHEMA_VERSION:
            issues.add("unsupported_schema_version", "/schema_version", "Image Publish Receipt schema is unsupported.")
        _validate_database_uuid(root.get("database_uuid"), "/database_uuid", issues)
        _resource_id(root.get("job_id"), "/job_id", _JOB_ID, issues)
        _sha(root.get("request_sha256"), "/request_sha256", issues)
        _resource_id(root.get("asset_id"), "/asset_id", _ASSET_ID, issues)
        _sha(root.get("source_binding_sha256"), "/source_binding_sha256", issues)
        prior_valid = _validate_analysis_binding(
            root.get("expected_prior_head"), "/expected_prior_head", issues, nullable=True
        )
        result_valid = _validate_analysis_binding(root.get("result_head"), "/result_head", issues)
        _integer(root.get("change_position"), "/change_position", issues, minimum=1, maximum=9_223_372_036_854_775_807)
        _sha(root.get("change_sha256"), "/change_sha256", issues)
        prior = root.get("expected_prior_head")
        result = root.get("result_head")
        if prior_valid and result_valid and type(result) is dict:
            expected_revision = 1 if prior is None else prior["revision"] + 1
            if result.get("revision") != expected_revision:
                issues.add(
                    "nonlinear_revision",
                    "/result_head/revision",
                    "Published revision must advance the expected head by one.",
                )
        _validate_digest(root, field="publish_receipt_sha256", kind="publish", issues=issues, required=sealed)
    _validate_resource_bounds(value, maximum_bytes=IMAGE_RECEIPT_MAX_CANONICAL_BYTES, issues=issues)
    return issues.items


def _validate_projection_receipt(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = {
        "object",
        "schema_version",
        "projection_contract",
        "projector_version",
        "database_uuid",
        "change_position",
        "asset_id",
        "analysis_binding",
        "source_binding_sha256",
        "projected_row_sha256",
        "alias_set_sha256",
        "outcome",
        "reason_code",
    }
    if sealed:
        fields.add("receipt_sha256")
    root = _closed(value, path="", fields=fields, issues=issues)
    if root is not None:
        if root.get("object") != IMAGE_PROJECTION_RECEIPT_OBJECT:
            issues.add("invalid_object", "/object", "Image Projection Receipt object is invalid.")
        if root.get("schema_version") != IMAGE_PROJECTION_RECEIPT_SCHEMA_VERSION:
            issues.add(
                "unsupported_schema_version", "/schema_version", "Image Projection Receipt schema is unsupported."
            )
        if root.get("projection_contract") != IMAGE_PROJECTION_CONTRACT:
            issues.add("unsupported_projection_contract", "/projection_contract", "Projection contract is unsupported.")
        _token(root.get("projector_version"), "/projector_version", issues)
        _validate_database_uuid(root.get("database_uuid"), "/database_uuid", issues)
        _integer(root.get("change_position"), "/change_position", issues, minimum=1, maximum=9_223_372_036_854_775_807)
        _resource_id(root.get("asset_id"), "/asset_id", _ASSET_ID, issues)
        _validate_analysis_binding(root.get("analysis_binding"), "/analysis_binding", issues)
        _sha(root.get("source_binding_sha256"), "/source_binding_sha256", issues, nullable=True)
        _sha(root.get("projected_row_sha256"), "/projected_row_sha256", issues, nullable=True)
        _sha(root.get("alias_set_sha256"), "/alias_set_sha256", issues)
        _enum(root.get("outcome"), "/outcome", IMAGE_PROJECTION_OUTCOMES, issues)
        _reason(root.get("reason_code"), "/reason_code", issues, nullable=True)
        outcome = root.get("outcome")
        if outcome in {"applied", "no_change", "superseded"}:
            if root.get("source_binding_sha256") is None or root.get("projected_row_sha256") is None:
                issues.add("projection_binding_missing", "", "Row-bearing outcome requires source and row digests.")
        elif outcome in {"removed", "blocked"} and root.get("projected_row_sha256") is not None:
            issues.add(
                "projected_row_forbidden",
                "/projected_row_sha256",
                "Removed or blocked outcome cannot claim a projected row.",
            )
        if outcome in {"applied", "no_change"} and root.get("reason_code") is not None:
            issues.add("unexpected_reason", "/reason_code", "Successful projection outcome cannot carry a reason.")
        if outcome in {"superseded", "removed", "blocked"} and root.get("reason_code") is None:
            issues.add("reason_required", "/reason_code", "Non-current projection outcome requires a reason.")
        _validate_digest(root, field="receipt_sha256", kind="projection_receipt", issues=issues, required=sealed)
    _validate_resource_bounds(value, maximum_bytes=IMAGE_RECEIPT_MAX_CANONICAL_BYTES, issues=issues)
    return issues.items


def _validate_manifest_entry(value: object, path: str, issues: _Issues) -> None:
    row = _closed(
        value,
        path=path,
        fields={"asset_id", "analysis_binding", "expected_row_sha256", "actual_row_sha256", "status", "reason_code"},
        issues=issues,
    )
    if row is None:
        return
    _resource_id(row.get("asset_id"), f"{path}/asset_id", _ASSET_ID, issues)
    _validate_analysis_binding(row.get("analysis_binding"), f"{path}/analysis_binding", issues, nullable=True)
    _sha(row.get("expected_row_sha256"), f"{path}/expected_row_sha256", issues, nullable=True)
    _sha(row.get("actual_row_sha256"), f"{path}/actual_row_sha256", issues, nullable=True)
    _enum(row.get("status"), f"{path}/status", IMAGE_MANIFEST_ENTRY_STATUSES, issues)
    _reason(row.get("reason_code"), f"{path}/reason_code", issues, nullable=True)
    status = row.get("status")
    expected = row.get("expected_row_sha256")
    actual = row.get("actual_row_sha256")
    binding = row.get("analysis_binding")
    reason = row.get("reason_code")
    valid_shape = {
        "matched": binding is not None and expected is not None and actual == expected and reason is None,
        "missing": binding is not None and expected is not None and actual is None and reason is not None,
        "unexpected": expected is None and actual is not None and reason is not None,
        "mismatched": binding is not None
        and expected is not None
        and actual is not None
        and actual != expected
        and reason is not None,
        "blocked": binding is not None and actual is None and reason is not None,
    }.get(status, False)
    if not valid_shape:
        issues.add("manifest_entry_inconsistent", path, "Manifest entry fields do not match its status.")


def _manifest_row_set_sha256(entries: Sequence[Mapping[str, object]]) -> str:
    rows = [
        {"asset_id": row["asset_id"], "projected_row_sha256": row["actual_row_sha256"]}
        for row in entries
        if row.get("actual_row_sha256") is not None
    ]
    rows.sort(key=lambda row: str(row["asset_id"]))
    return canonical_sha256(rows)


def _validate_manifest_entries(value: object, issues: _Issues) -> list[object]:
    if type(value) is not list:
        issues.add("schema_type", "/entries", "Manifest entries must be an array.")
        return []
    if len(value) > IMAGE_MANIFEST_MAX_ENTRIES:
        issues.add("array_too_large", "/entries", "Manifest exceeds the entry limit.")
    bounded = value[: IMAGE_MANIFEST_MAX_ENTRIES + 1]
    for index, entry in enumerate(bounded):
        _validate_manifest_entry(entry, f"/entries/{index}", issues)
    if all(type(entry) is dict and type(entry.get("asset_id")) is str for entry in bounded):
        asset_ids = [entry["asset_id"] for entry in bounded]
        if asset_ids != sorted(set(asset_ids)):
            issues.add("noncanonical_order", "/entries", "Manifest entries must have unique sorted asset IDs.")
    return bounded


def _validate_manifest_reason_counts(
    value: object,
    entries: Sequence[object],
    issues: _Issues,
) -> None:
    if type(value) is not list:
        issues.add("schema_type", "/reason_counts", "Reason counts must be an array.")
        value = []
    elif len(value) > IMAGE_MANIFEST_MAX_ENTRIES:
        issues.add("array_too_large", "/reason_counts", "Reason counts exceed the item limit.")
    observed: dict[str, int] = {}
    for entry in entries:
        if type(entry) is dict and type(entry.get("reason_code")) is str:
            code = entry["reason_code"]
            observed[code] = observed.get(code, 0) + 1
    parsed: dict[str, int] = {}
    for index, item in enumerate(value[: IMAGE_MANIFEST_MAX_ENTRIES + 1]):
        row = _closed(item, path=f"/reason_counts/{index}", fields={"reason_code", "count"}, issues=issues)
        if row is None or not _reason(row.get("reason_code"), f"/reason_counts/{index}/reason_code", issues):
            continue
        code = row["reason_code"]
        if code in parsed:
            issues.add("duplicate_reason", f"/reason_counts/{index}/reason_code", "Reason count is duplicated.")
        elif _integer(
            row.get("count"),
            f"/reason_counts/{index}/count",
            issues,
            minimum=1,
            maximum=IMAGE_MANIFEST_MAX_ENTRIES,
        ):
            parsed[code] = row["count"]
    if all(type(item) is dict and type(item.get("reason_code")) is str for item in value):
        codes = [item["reason_code"] for item in value]
        if codes != sorted(set(codes)):
            issues.add("noncanonical_order", "/reason_counts", "Reason counts must be unique and sorted.")
    if parsed != observed:
        issues.add("reason_count_mismatch", "/reason_counts", "Reason counts do not match manifest entries.")


def _validate_manifest_counts(
    counts: dict[str, Any] | None,
    entries: Sequence[object],
    issues: _Issues,
) -> None:
    if counts is None:
        return
    for key in counts:
        _integer(counts[key], f"/counts/{key}", issues, minimum=0, maximum=IMAGE_MANIFEST_MAX_ENTRIES)
    statuses = [entry.get("status") for entry in entries if type(entry) is dict]
    expected = {
        "eligible": sum(status != "unexpected" for status in statuses),
        "projected": sum(type(entry) is dict and entry.get("actual_row_sha256") is not None for entry in entries),
        "aliases": counts.get("aliases"),
        "missing": statuses.count("missing"),
        "unexpected": statuses.count("unexpected"),
        "mismatched": statuses.count("mismatched"),
        "blocked": statuses.count("blocked"),
    }
    if counts != expected:
        issues.add("manifest_count_mismatch", "/counts", "Manifest counts do not match its entries.")


def _validate_projection_manifest(value: object, *, sealed: bool) -> list[dict[str, str]]:
    issues = _Issues()
    fields = {
        "object",
        "schema_version",
        "projection_contract",
        "projector_version",
        "compiler_version",
        "database_uuid",
        "canonical_high_water_mark",
        "counts",
        "entries",
        "reason_counts",
        "alias_set_sha256",
        "row_set_sha256",
    }
    if sealed:
        fields.add("manifest_sha256")
    root = _closed(value, path="", fields=fields, issues=issues)
    if root is not None:
        if root.get("object") != IMAGE_PROJECTION_MANIFEST_OBJECT:
            issues.add("invalid_object", "/object", "Image Projection Manifest object is invalid.")
        if root.get("schema_version") != IMAGE_PROJECTION_MANIFEST_SCHEMA_VERSION:
            issues.add(
                "unsupported_schema_version", "/schema_version", "Image Projection Manifest schema is unsupported."
            )
        if root.get("projection_contract") != IMAGE_PROJECTION_CONTRACT:
            issues.add("unsupported_projection_contract", "/projection_contract", "Projection contract is unsupported.")
        _token(root.get("projector_version"), "/projector_version", issues)
        _token(root.get("compiler_version"), "/compiler_version", issues)
        _validate_database_uuid(root.get("database_uuid"), "/database_uuid", issues)
        _integer(
            root.get("canonical_high_water_mark"),
            "/canonical_high_water_mark",
            issues,
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        counts = _closed(
            root.get("counts"),
            path="/counts",
            fields={"eligible", "projected", "aliases", "missing", "unexpected", "mismatched", "blocked"},
            issues=issues,
        )
        entries = _validate_manifest_entries(root.get("entries"), issues)
        _validate_manifest_reason_counts(root.get("reason_counts"), entries, issues)
        _validate_manifest_counts(counts, entries, issues)
        _sha(root.get("alias_set_sha256"), "/alias_set_sha256", issues)
        if _sha(root.get("row_set_sha256"), "/row_set_sha256", issues):
            valid_entries = [entry for entry in entries if type(entry) is dict]
            try:
                expected_row_set = _manifest_row_set_sha256(valid_entries)
            except (KeyError, TypeError, ValueError):
                pass
            else:
                if root.get("row_set_sha256") != expected_row_set:
                    issues.add("row_set_digest_mismatch", "/row_set_sha256", "Row-set digest does not match entries.")
        _validate_digest(root, field="manifest_sha256", kind="manifest", issues=issues, required=sealed)
    _validate_resource_bounds(value, maximum_bytes=IMAGE_MANIFEST_MAX_CANONICAL_BYTES, issues=issues)
    return issues.items


def _raise_if_invalid(errors: list[dict[str, str]]) -> None:
    if errors:
        raise ImageAnalysisContractError(errors)


def _seal(
    value: Mapping[str, object],
    *,
    kind: str,
    digest_field: str,
    validator: Callable[[object], list[dict[str, str]]],
    sealed_validator: Callable[[object], list[dict[str, str]]],
) -> dict[str, object]:
    normalized = _normalized(value, kind)
    _raise_if_invalid(validator(normalized))
    assert type(normalized) is dict
    normalized[digest_field] = canonical_sha256(normalized)
    _raise_if_invalid(sealed_validator(normalized))
    return normalized


def validate_image_analysis_result(value: object) -> list[dict[str, str]]:
    return _validate_result(value, sealed=True)


def require_valid_image_analysis_result(value: object) -> None:
    _raise_if_invalid(validate_image_analysis_result(value))


def image_analysis_result_sha256(value: Mapping[str, object]) -> str:
    normalized = _normalized(value, "result", "content_sha256")
    _raise_if_invalid(_validate_result(normalized, sealed=False))
    return canonical_sha256(normalized)


def seal_image_analysis_result(value: Mapping[str, object]) -> dict[str, object]:
    return _seal(
        value,
        kind="result",
        digest_field="content_sha256",
        validator=lambda item: _validate_result(item, sealed=False),
        sealed_validator=validate_image_analysis_result,
    )


def canonical_image_analysis_result_json(value: object) -> str:
    require_valid_image_analysis_result(value)
    return canonical_json(_normalized(value, "result"))


def validate_projection_change(value: object) -> list[dict[str, str]]:
    return _validate_change(value, sealed=True)


def require_valid_projection_change(value: object) -> None:
    _raise_if_invalid(validate_projection_change(value))


def projection_change_sha256(value: Mapping[str, object]) -> str:
    normalized = _normalized(value, "change", "change_sha256")
    _raise_if_invalid(_validate_change(normalized, sealed=False))
    return canonical_sha256(normalized)


def seal_projection_change(value: Mapping[str, object]) -> dict[str, object]:
    return _seal(
        value,
        kind="change",
        digest_field="change_sha256",
        validator=lambda item: _validate_change(item, sealed=False),
        sealed_validator=validate_projection_change,
    )


def canonical_projection_change_json(value: object) -> str:
    require_valid_projection_change(value)
    return canonical_json(_normalized(value, "change"))


def validate_publish_receipt(value: object) -> list[dict[str, str]]:
    return _validate_publish_receipt(value, sealed=True)


def require_valid_publish_receipt(value: object) -> None:
    _raise_if_invalid(validate_publish_receipt(value))


def publish_receipt_sha256(value: Mapping[str, object]) -> str:
    normalized = _normalized(value, "publish", "publish_receipt_sha256")
    _raise_if_invalid(_validate_publish_receipt(normalized, sealed=False))
    return canonical_sha256(normalized)


def seal_publish_receipt(value: Mapping[str, object]) -> dict[str, object]:
    return _seal(
        value,
        kind="publish",
        digest_field="publish_receipt_sha256",
        validator=lambda item: _validate_publish_receipt(item, sealed=False),
        sealed_validator=validate_publish_receipt,
    )


def canonical_publish_receipt_json(value: object) -> str:
    require_valid_publish_receipt(value)
    return canonical_json(_normalized(value, "publish"))


def validate_projection_receipt(value: object) -> list[dict[str, str]]:
    return _validate_projection_receipt(value, sealed=True)


def require_valid_projection_receipt(value: object) -> None:
    _raise_if_invalid(validate_projection_receipt(value))


def projection_receipt_sha256(value: Mapping[str, object]) -> str:
    normalized = _normalized(value, "projection_receipt", "receipt_sha256")
    _raise_if_invalid(_validate_projection_receipt(normalized, sealed=False))
    return canonical_sha256(normalized)


def seal_projection_receipt(value: Mapping[str, object]) -> dict[str, object]:
    return _seal(
        value,
        kind="projection_receipt",
        digest_field="receipt_sha256",
        validator=lambda item: _validate_projection_receipt(item, sealed=False),
        sealed_validator=validate_projection_receipt,
    )


def canonical_projection_receipt_json(value: object) -> str:
    require_valid_projection_receipt(value)
    return canonical_json(_normalized(value, "projection_receipt"))


def validate_projection_manifest(value: object) -> list[dict[str, str]]:
    return _validate_projection_manifest(value, sealed=True)


def require_valid_projection_manifest(value: object) -> None:
    _raise_if_invalid(validate_projection_manifest(value))


def projection_manifest_sha256(value: Mapping[str, object]) -> str:
    normalized = _normalized(value, "manifest", "manifest_sha256")
    _raise_if_invalid(_validate_projection_manifest(normalized, sealed=False))
    return canonical_sha256(normalized)


def seal_projection_manifest(value: Mapping[str, object]) -> dict[str, object]:
    return _seal(
        value,
        kind="manifest",
        digest_field="manifest_sha256",
        validator=lambda item: _validate_projection_manifest(item, sealed=False),
        sealed_validator=validate_projection_manifest,
    )


def canonical_projection_manifest_json(value: object) -> str:
    require_valid_projection_manifest(value)
    return canonical_json(_normalized(value, "manifest"))


def analysis_binding_sha256(value: Mapping[str, object]) -> str:
    issues = _Issues()
    _validate_analysis_binding(value, "", issues)
    _validate_resource_bounds(value, maximum_bytes=4_096, issues=issues)
    _raise_if_invalid(issues.items)
    return canonical_sha256(value)


def source_binding_sha256(value: Mapping[str, object]) -> str:
    issues = _Issues()
    _validate_source_binding(value, "", issues)
    _validate_resource_bounds(value, maximum_bytes=4_096, issues=issues)
    _raise_if_invalid(issues.items)
    return canonical_sha256(value)


def build_projection_manifest(
    *,
    projector_version: str,
    compiler_version: str,
    database_uuid: str,
    canonical_high_water_mark: int,
    entries: Sequence[Mapping[str, object]],
    alias_count: int,
    alias_set_sha256: str,
) -> dict[str, object]:
    """Build and seal one sorted, path-free manifest from parity entries."""

    detached_entries = deepcopy(list(entries))
    entry_issues = _Issues()
    for index, entry in enumerate(detached_entries[: IMAGE_MANIFEST_MAX_ENTRIES + 1]):
        _validate_manifest_entry(entry, f"/entries/{index}", entry_issues)
    if len(detached_entries) > IMAGE_MANIFEST_MAX_ENTRIES:
        entry_issues.add("array_too_large", "/entries", "Manifest exceeds the entry limit.")
    _raise_if_invalid(entry_issues.items)
    detached_entries.sort(key=lambda row: str(row["asset_id"]))
    statuses = [row.get("status") for row in detached_entries]
    reasons: dict[str, int] = {}
    for row in detached_entries:
        reason = row.get("reason_code")
        if type(reason) is str:
            reasons[reason] = reasons.get(reason, 0) + 1
    payload: dict[str, object] = {
        "object": IMAGE_PROJECTION_MANIFEST_OBJECT,
        "schema_version": IMAGE_PROJECTION_MANIFEST_SCHEMA_VERSION,
        "projection_contract": IMAGE_PROJECTION_CONTRACT,
        "projector_version": projector_version,
        "compiler_version": compiler_version,
        "database_uuid": database_uuid,
        "canonical_high_water_mark": canonical_high_water_mark,
        "counts": {
            "eligible": sum(status != "unexpected" for status in statuses),
            "projected": sum(row.get("actual_row_sha256") is not None for row in detached_entries),
            "aliases": alias_count,
            "missing": statuses.count("missing"),
            "unexpected": statuses.count("unexpected"),
            "mismatched": statuses.count("mismatched"),
            "blocked": statuses.count("blocked"),
        },
        "entries": detached_entries,
        "reason_counts": [{"reason_code": code, "count": reasons[code]} for code in sorted(reasons)],
        "alias_set_sha256": alias_set_sha256,
        "row_set_sha256": _manifest_row_set_sha256(detached_entries),
    }
    return seal_projection_manifest(payload)


__all__ = [
    "IMAGE_ANALYSIS_MAX_CANONICAL_BYTES",
    "IMAGE_ANALYSIS_MAX_TAGS",
    "IMAGE_ANALYSIS_MAX_VECTORS",
    "IMAGE_ANALYSIS_RESULT_OBJECT",
    "IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION",
    "IMAGE_ANALYSIS_STAGES",
    "IMAGE_ANALYSIS_STAGE_STATUSES",
    "IMAGE_CONTRACT_MAX_ERRORS",
    "IMAGE_MANIFEST_MAX_CANONICAL_BYTES",
    "IMAGE_MANIFEST_MAX_ENTRIES",
    "IMAGE_PROJECTION_CHANGE_OBJECT",
    "IMAGE_PROJECTION_CHANGE_SCHEMA_VERSION",
    "IMAGE_PROJECTION_CONTRACT",
    "IMAGE_PROJECTION_MANIFEST_OBJECT",
    "IMAGE_PROJECTION_MANIFEST_SCHEMA_VERSION",
    "IMAGE_PROJECTION_OUTCOMES",
    "IMAGE_PROJECTION_RECEIPT_OBJECT",
    "IMAGE_PROJECTION_RECEIPT_SCHEMA_VERSION",
    "IMAGE_PUBLISH_RECEIPT_OBJECT",
    "IMAGE_PUBLISH_RECEIPT_SCHEMA_VERSION",
    "IMAGE_RECEIPT_MAX_CANONICAL_BYTES",
    "ImageAnalysisContractError",
    "analysis_binding_sha256",
    "build_canonical_image_combined_text",
    "build_projection_manifest",
    "canonical_image_analysis_result_json",
    "canonical_json",
    "canonical_projection_change_json",
    "canonical_projection_manifest_json",
    "canonical_projection_receipt_json",
    "canonical_publish_receipt_json",
    "canonical_sha256",
    "image_analysis_result_sha256",
    "projection_change_sha256",
    "projection_manifest_sha256",
    "projection_receipt_sha256",
    "publish_receipt_sha256",
    "require_valid_image_analysis_result",
    "require_valid_projection_change",
    "require_valid_projection_manifest",
    "require_valid_projection_receipt",
    "require_valid_publish_receipt",
    "seal_image_analysis_result",
    "seal_projection_change",
    "seal_projection_manifest",
    "seal_projection_receipt",
    "seal_publish_receipt",
    "source_binding_sha256",
    "validate_image_analysis_result",
    "validate_projection_change",
    "validate_projection_manifest",
    "validate_projection_receipt",
    "validate_publish_receipt",
]
