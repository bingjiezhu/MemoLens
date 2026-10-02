"""Fail-closed canonical image observation reads for the Agent plugin.

The plugin is shipped independently from the MemoLens Python package, so this
module deliberately carries the small deterministic verifier needed by the
read surface.  It never resolves a library path and never treats ``image_index``
text as analysis authority.  The compatibility table is accepted only after
an exact canonical result has been re-rendered and matched to its active clean
projection generation.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
import sqlite3
from typing import Any, Mapping
import unicodedata

from memolens_blueprint_authority import validate_exact_managed_schema_manifest
from memolens_contracts import MemoLensError
from memolens_strict_json import StrictJsonError, StrictJsonLimits, decode_strict_json


_PROJECTION_CONTRACT = "legacy-image-index-shadow/v1"
_PROJECTED_ROW_OBJECT = "memolens.projected_image_index_row"
_STAGES = ("metadata", "geocode", "vision", "embedding", "quality")
_STAGE_STATUSES = {
    "succeeded",
    "partial",
    "unsupported",
    "disabled",
    "failed",
    "unknown",
}
_MANIFEST_ENTRY_STATUSES = {
    "matched",
    "missing",
    "unexpected",
    "mismatched",
    "blocked",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ROOT_ID = re.compile(r"^root_[0-9a-f]{24}$")
_DATABASE_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_MIME_TYPE = re.compile(r"^image/[A-Za-z0-9.+-]{1,100}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,255}$")
_RESULT_LIMITS = StrictJsonLimits(
    max_bytes=1_048_576,
    max_depth=24,
    max_nodes=500_000,
    max_object_items=2_000,
    max_array_items=100_000,
    max_string_chars=262_144,
)
_RECEIPT_LIMITS = StrictJsonLimits(
    max_bytes=65_536,
    max_depth=24,
    max_nodes=100_000,
    max_object_items=2_000,
    max_array_items=10_000,
    max_string_chars=65_536,
)
_MANIFEST_LIMITS = StrictJsonLimits(
    max_bytes=16_777_216,
    max_depth=24,
    max_nodes=500_000,
    max_object_items=2_000,
    max_array_items=100_000,
    max_string_chars=262_144,
)
_REQUEST_LIMITS = StrictJsonLimits(
    max_bytes=65_536,
    max_depth=16,
    max_nodes=4_096,
    max_object_items=128,
    max_array_items=128,
    max_string_chars=65_536,
)
_PROJECTED_ROW_FIELDS = (
    "id",
    "sha256",
    "filename",
    "relative_path",
    "mime_type",
    "file_size",
    "width",
    "height",
    "taken_at",
    "lat",
    "lon",
    "altitude",
    "place_name",
    "country",
    "description",
    "tags_json",
    "combined_text",
    "text_embedding_model",
    "combined_text_embedding_b64",
    "embedding_backend",
    "embedding_b64",
    "aesthetic_score",
    "aesthetic_model",
    "technical_quality_score",
    "aesthetic_updated_at",
    "created_at",
    "updated_at",
)
_IMAGE_INDEX_COLUMNS = (
    *_PROJECTED_ROW_FIELDS[:18],
    "combined_text_embedding",
    "embedding_backend",
    "embedding",
    *_PROJECTED_ROW_FIELDS[21:],
)
_PROJECTED_ROW_DOCUMENT_FIELDS = {
    "object",
    "schema_version",
    "projection_contract",
    "projector_version",
    "asset_id",
    "analysis_binding",
    "source_binding_sha256",
    "row",
    "vector_bindings",
    "row_sha256",
}
_MANIFEST_FIELDS = {
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
    "manifest_sha256",
}


@dataclass(frozen=True, slots=True)
class _AuthorityFailure(RuntimeError):
    code: str

    def __str__(self) -> str:
        return self.code


def _fail(code: str = "canonical_image_projection_corrupt") -> None:
    raise _AuthorityFailure(code)


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _fail()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strict_object(value: object, limits: StrictJsonLimits) -> dict[str, Any]:
    try:
        parsed = decode_strict_json(value, limits=limits, require_object=True)
    except StrictJsonError:
        _fail()
    assert isinstance(parsed, dict)
    return parsed


def _closed(value: object, fields: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != fields:
        _fail()
    return value


def _text(
    value: object,
    *,
    minimum: int = 1,
    maximum: int = 200,
    pattern: re.Pattern[str] | None = None,
    nullable: bool = False,
) -> str | None:
    if value is None and nullable:
        return None
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or any(
            (ord(character) < 0x20 and character not in {"\t", "\n", "\r"})
            or ord(character) == 0x7F
            for character in value
        )
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail()
    return value


def _integer(
    value: object,
    *,
    minimum: int,
    maximum: int,
    nullable: bool = False,
) -> int | None:
    if value is None and nullable:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        _fail()
    return value


def _number(
    value: object,
    *,
    minimum: float,
    maximum: float,
    nullable: bool = False,
) -> float | None:
    if value is None and nullable:
        return None
    if (
        type(value) is not float
        or not math.isfinite(value)
        or not minimum <= value <= maximum
    ):
        _fail()
    return value


def _job_text(
    value: object,
    *,
    minimum: int = 1,
    maximum: int = 200,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if (
        type(value) is not str
        or not minimum <= len(value) <= maximum
        or not unicodedata.is_normalized("NFC", value)
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
        or (pattern is not None and pattern.fullmatch(value) is None)
    ):
        _fail("canonical_image_request_binding_invalid")
    return value


def _job_integer(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        _fail("canonical_image_request_binding_invalid")
    return value


def _job_sha(value: object) -> str:
    return _job_text(value, minimum=64, maximum=64, pattern=_SHA256)


def _job_relative_path(value: object) -> str:
    path = _job_text(value, maximum=4_096)
    if (
        path.startswith(("/", "~"))
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path) is not None
        or "://" in path
    ):
        _fail("canonical_image_request_binding_invalid")
    parsed = PurePosixPath(path)
    if (
        not parsed.parts
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or any(len(part.encode("utf-8")) > 255 for part in parsed.parts)
        or parsed.as_posix() != path
    ):
        _fail("canonical_image_request_binding_invalid")
    return path


def _sha(value: object, *, nullable: bool = False) -> str | None:
    return _text(
        value,
        minimum=64,
        maximum=64,
        pattern=_SHA256,
        nullable=nullable,
    )


def _binding(value: object) -> dict[str, object]:
    row = _closed(
        value,
        {"analysis_run_id", "revision", "content_sha256"},
    )
    _text(
        row.get("analysis_run_id"),
        pattern=_ANALYSIS_RUN_ID,
        minimum=37,
        maximum=37,
    )
    _integer(row.get("revision"), minimum=1, maximum=1_800_000)
    _sha(row.get("content_sha256"))
    return row


def _request_document(value: object) -> dict[str, Any]:
    request = _strict_object(value, _REQUEST_LIMITS)
    _closed(
        request,
        {
            "object",
            "schema_version",
            "worker_kind",
            "database_binding",
            "asset_binding",
            "source_binding",
            "analysis_profile",
            "expected_head",
            "intended_publication",
            "idempotency",
        },
    )
    if (
        request.get("object") != "memolens.image_analysis_request"
        or request.get("schema_version") != "1"
        or request.get("worker_kind") != "image_analysis"
    ):
        _fail("canonical_image_request_binding_invalid")

    database = _closed(
        request.get("database_binding"),
        {"database_uuid", "database_device", "database_inode"},
    )
    _job_text(
        database.get("database_uuid"),
        minimum=36,
        maximum=36,
        pattern=_DATABASE_UUID,
    )
    _job_integer(
        database.get("database_device"),
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    _job_integer(
        database.get("database_inode"),
        minimum=1,
        maximum=9_223_372_036_854_775_807,
    )

    asset = _closed(
        request.get("asset_binding"),
        {"asset_id", "input_asset_sha256"},
    )
    asset_id = _job_text(
        asset.get("asset_id"),
        minimum=30,
        maximum=30,
        pattern=_ASSET_ID,
    )
    asset_sha256 = _job_sha(asset.get("input_asset_sha256"))
    if asset_id != f"asset_{asset_sha256[:24]}":
        _fail("canonical_image_request_binding_invalid")

    source = _closed(
        request.get("source_binding"),
        {
            "source_id",
            "library_root_id",
            "relative_path",
            "observed_size",
            "observed_mtime_ns",
            "observed_ctime_ns",
            "source_device",
            "source_inode",
            "file_identity_sha256",
        },
    )
    _job_text(
        source.get("source_id"),
        minimum=28,
        maximum=28,
        pattern=_SOURCE_ID,
    )
    _job_text(
        source.get("library_root_id"),
        minimum=29,
        maximum=29,
        pattern=_ROOT_ID,
    )
    _job_relative_path(source.get("relative_path"))
    for field, minimum, maximum in (
        ("observed_size", 1, 8_589_934_592),
        ("observed_mtime_ns", 0, 9_223_372_036_854_775_807),
        ("observed_ctime_ns", 0, 9_223_372_036_854_775_807),
        ("source_device", 0, 9_223_372_036_854_775_807),
        ("source_inode", 1, 9_223_372_036_854_775_807),
    ):
        _job_integer(source.get(field), minimum=minimum, maximum=maximum)
    _job_sha(source.get("file_identity_sha256"))

    profile = _closed(
        request.get("analysis_profile"),
        {"profile_id", "profile_version", "profile_sha256"},
    )
    _job_text(profile.get("profile_id"), pattern=_TOKEN)
    _job_text(profile.get("profile_version"), pattern=_TOKEN)
    _job_sha(profile.get("profile_sha256"))

    expected_head = request.get("expected_head")
    if expected_head is not None:
        _binding(expected_head)
    intended = _closed(
        request.get("intended_publication"),
        {"analysis_run_id", "revision"},
    )
    _job_text(
        intended.get("analysis_run_id"),
        minimum=37,
        maximum=37,
        pattern=_ANALYSIS_RUN_ID,
    )
    intended_revision = _job_integer(
        intended.get("revision"), minimum=1, maximum=1_800_000
    )
    expected_revision = (
        1
        if expected_head is None
        else int(expected_head["revision"]) + 1
    )
    if intended_revision != expected_revision:
        _fail("canonical_image_request_binding_invalid")

    idempotency = _closed(request.get("idempotency"), {"scope", "key"})
    _job_text(idempotency.get("scope"), maximum=256, pattern=_IDEMPOTENCY_KEY)
    _job_text(idempotency.get("key"), maximum=256, pattern=_IDEMPOTENCY_KEY)
    return request


def _profile_document(value: object) -> dict[str, Any]:
    profile = _strict_object(value, _REQUEST_LIMITS)
    _closed(profile, {"profile_id", "profile_version", "profile_sha256"})
    _job_text(profile.get("profile_id"), pattern=_TOKEN)
    _job_text(profile.get("profile_version"), pattern=_TOKEN)
    _job_sha(profile.get("profile_sha256"))
    return profile


def _manifest_entry(value: object) -> dict[str, object]:
    row = _closed(
        value,
        {
            "asset_id",
            "analysis_binding",
            "expected_row_sha256",
            "actual_row_sha256",
            "status",
            "reason_code",
        },
    )
    _text(row.get("asset_id"), pattern=_ASSET_ID, minimum=30, maximum=30)
    analysis_binding = row.get("analysis_binding")
    if analysis_binding is not None:
        _binding(analysis_binding)
    expected = _sha(row.get("expected_row_sha256"), nullable=True)
    actual = _sha(row.get("actual_row_sha256"), nullable=True)
    status = row.get("status")
    if status not in _MANIFEST_ENTRY_STATUSES:
        _fail()
    reason = _text(
        row.get("reason_code"),
        pattern=_REASON_CODE,
        maximum=120,
        nullable=True,
    )
    valid_shape = {
        "matched": (
            analysis_binding is not None
            and expected is not None
            and actual == expected
            and reason is None
        ),
        "missing": (
            analysis_binding is not None
            and expected is not None
            and actual is None
            and reason is not None
        ),
        "unexpected": expected is None and actual is not None and reason is not None,
        "mismatched": (
            analysis_binding is not None
            and expected is not None
            and actual is not None
            and actual != expected
            and reason is not None
        ),
        "blocked": (
            analysis_binding is not None
            and actual is None
            and reason is not None
        ),
    }[str(status)]
    if not valid_shape:
        _fail()
    return row


def _manifest_entries(value: object) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > 100_000:
        _fail()
    entries = [_manifest_entry(item) for item in value]
    asset_ids = [str(entry["asset_id"]) for entry in entries]
    if asset_ids != sorted(set(asset_ids)):
        _fail()
    return entries


def _manifest_reason_counts(
    value: object,
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    if type(value) is not list or len(value) > 100_000:
        _fail()
    observed: dict[str, int] = {}
    for entry in entries:
        reason = entry.get("reason_code")
        if type(reason) is str:
            observed[reason] = observed.get(reason, 0) + 1
    parsed: dict[str, int] = {}
    rows: list[dict[str, object]] = []
    for item in value:
        row = _closed(item, {"reason_code", "count"})
        reason = _text(
            row.get("reason_code"),
            pattern=_REASON_CODE,
            maximum=120,
        )
        assert isinstance(reason, str)
        if reason in parsed:
            _fail()
        count = _integer(row.get("count"), minimum=1, maximum=100_000)
        assert isinstance(count, int)
        parsed[reason] = count
        rows.append(row)
    if [str(row["reason_code"]) for row in rows] != sorted(parsed):
        _fail()
    if parsed != observed:
        _fail()
    return rows


def _manifest_counts(
    value: object,
    entries: list[dict[str, object]],
) -> dict[str, object]:
    counts = _closed(
        value,
        {
            "eligible",
            "projected",
            "aliases",
            "missing",
            "unexpected",
            "mismatched",
            "blocked",
        },
    )
    for count in counts.values():
        _integer(count, minimum=0, maximum=100_000)
    statuses = [str(entry["status"]) for entry in entries]
    expected = {
        "eligible": sum(status != "unexpected" for status in statuses),
        "projected": sum(
            entry.get("actual_row_sha256") is not None for entry in entries
        ),
        "aliases": counts["aliases"],
        "missing": statuses.count("missing"),
        "unexpected": statuses.count("unexpected"),
        "mismatched": statuses.count("mismatched"),
        "blocked": statuses.count("blocked"),
    }
    if counts != expected:
        _fail()
    return counts


def _manifest_row_set(
    entries: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        {
            "asset_id": entry["asset_id"],
            "projected_row_sha256": entry["actual_row_sha256"],
        }
        for entry in entries
        if entry.get("actual_row_sha256") is not None
    ]


def _projection_alias_set(
    connection: sqlite3.Connection,
    *,
    generation_id: object,
    database_uuid: str,
) -> list[dict[str, str]]:
    rows = connection.execute(
        """SELECT legacy_id,canonical_asset_id,library_root_id,source_id,
                  alias_scope,alias_sha256
             FROM image_projection_aliases
            WHERE generation_id=? ORDER BY legacy_id""",
        (generation_id,),
    ).fetchall()
    alias_set: list[dict[str, str]] = []
    for item in rows:
        legacy_id = _text(item["legacy_id"], maximum=256)
        canonical_asset_id = _text(
            item["canonical_asset_id"],
            pattern=_ASSET_ID,
            minimum=30,
            maximum=30,
        )
        library_root_id = _text(
            item["library_root_id"],
            pattern=_ROOT_ID,
            minimum=29,
            maximum=29,
        )
        source_id = _text(
            item["source_id"],
            pattern=_SOURCE_ID,
            minimum=28,
            maximum=28,
        )
        alias_scope = item["alias_scope"]
        if alias_scope != "legacy-image-index/v1":
            _fail()
        identity = {
            "database_uuid": database_uuid,
            "legacy_id": legacy_id,
            "canonical_asset_id": canonical_asset_id,
            "library_root_id": library_root_id,
            "source_id": source_id,
            "alias_scope": alias_scope,
        }
        alias_sha256 = _sha(item["alias_sha256"])
        if _canonical_sha256(identity) != alias_sha256:
            _fail()
        alias_set.append(
            {"legacy_id": legacy_id, "alias_sha256": alias_sha256}
        )
    return alias_set


def _normalized_document(
    value: Mapping[str, object],
    *,
    digest_field: str,
    kind: str,
) -> dict[str, object]:
    copied = deepcopy(dict(value))
    copied.pop(digest_field, None)
    if kind == "result":
        stages = copied.get("stages")
        if type(stages) is dict:
            vision = stages.get("vision")
            if type(vision) is dict and type(vision.get("output")) is dict:
                tags = vision["output"].get("tags")
                if type(tags) is list and all(type(tag) is str for tag in tags):
                    vision["output"]["tags"] = sorted(set(tags))
            embedding = stages.get("embedding")
            if type(embedding) is dict and type(embedding.get("output")) is dict:
                vectors = embedding["output"].get("vectors")
                if type(vectors) is list and all(
                    type(row) is dict and type(row.get("purpose")) is str
                    for row in vectors
                ):
                    embedding["output"]["vectors"] = sorted(
                        vectors,
                        key=lambda row: row["purpose"],
                    )
    elif kind == "manifest":
        entries = copied.get("entries")
        if type(entries) is list and all(
            type(row) is dict and type(row.get("asset_id")) is str
            for row in entries
        ):
            copied["entries"] = sorted(entries, key=lambda row: row["asset_id"])
        reasons = copied.get("reason_counts")
        if type(reasons) is list and all(
            type(row) is dict and type(row.get("reason_code")) is str
            for row in reasons
        ):
            copied["reason_counts"] = sorted(
                reasons,
                key=lambda row: row["reason_code"],
            )
    return copied


def _require_self_digest(
    value: Mapping[str, object],
    *,
    digest_field: str,
    kind: str,
) -> None:
    digest = _sha(value.get(digest_field))
    normalized = _normalized_document(
        value,
        digest_field=digest_field,
        kind=kind,
    )
    if digest != _canonical_sha256(normalized):
        _fail()


def _provenance(value: object) -> dict[str, object]:
    row = _closed(
        value,
        {
            "producer_id",
            "producer_version",
            "model_id",
            "model_version",
            "rule_id",
            "rule_version",
        },
    )
    for field in ("producer_id", "producer_version"):
        _text(row.get(field), pattern=_TOKEN)
    for field in ("model_id", "model_version", "rule_id", "rule_version"):
        _text(row.get(field), pattern=_TOKEN, nullable=True)
    if (row.get("model_id") is None) != (row.get("model_version") is None):
        _fail()
    if (row.get("rule_id") is None) != (row.get("rule_version") is None):
        _fail()
    return row


def _metadata_output(value: object, status: str) -> dict[str, object]:
    row = _closed(
        value,
        {
            "mime_type",
            "file_size",
            "width",
            "height",
            "taken_at",
            "latitude",
            "longitude",
            "altitude",
        },
    )
    mime = _text(
        row.get("mime_type"),
        maximum=106,
        pattern=_MIME_TYPE,
        nullable=True,
    )
    size = _integer(
        row.get("file_size"), minimum=1, maximum=8_589_934_592, nullable=True
    )
    width = _integer(row.get("width"), minimum=1, maximum=100_000, nullable=True)
    height = _integer(row.get("height"), minimum=1, maximum=100_000, nullable=True)
    _text(row.get("taken_at"), maximum=128, nullable=True)
    latitude = _number(
        row.get("latitude"), minimum=-90, maximum=90, nullable=True
    )
    longitude = _number(
        row.get("longitude"), minimum=-180, maximum=180, nullable=True
    )
    _number(row.get("altitude"), minimum=-20_000, maximum=1_000_000, nullable=True)
    if (latitude is None) != (longitude is None):
        _fail()
    if status == "succeeded" and None in {mime, size, width, height}:
        _fail()
    return row


def _geocode_output(value: object, status: str) -> dict[str, object]:
    row = _closed(value, {"place_name", "country"})
    _text(row.get("place_name"), maximum=512, nullable=True)
    _text(row.get("country"), maximum=256, nullable=True)
    if status == "succeeded" and not (row.get("place_name") or row.get("country")):
        _fail()
    return row


def _vision_output(value: object, status: str) -> dict[str, object]:
    row = _closed(value, {"description", "tags", "location_hint"})
    _text(row.get("description"), minimum=0, maximum=32_768, nullable=True)
    _text(row.get("location_hint"), maximum=2_048, nullable=True)
    tags = row.get("tags")
    if (
        type(tags) is not list
        or len(tags) > 128
        or any(
            type(tag) is not str
            or not 1 <= len(tag) <= 256
            or any(
                (ord(character) < 0x20 and character not in {"\t", "\n", "\r"})
                or ord(character) == 0x7F
                for character in tag
            )
            for tag in tags
        )
        or tags != sorted(set(tags))
    ):
        _fail()
    if status == "succeeded" and not (
        row.get("description") or tags or row.get("location_hint")
    ):
        _fail()
    return row


def _embedding_output(value: object, status: str) -> dict[str, object]:
    row = _closed(value, {"combined_text_sha256", "vectors"})
    _sha(row.get("combined_text_sha256"), nullable=True)
    vectors = row.get("vectors")
    if type(vectors) is not list or len(vectors) > 4:
        _fail()
    purposes: list[str] = []
    for value_row in vectors:
        vector = _closed(
            value_row,
            {"purpose", "signal", "model_id", "dimensions", "artifact_sha256"},
        )
        if vector.get("purpose") not in {"image_search", "text_search"}:
            _fail()
        if vector.get("signal") not in {"visual", "text_derived"}:
            _fail()
        _text(vector.get("model_id"), pattern=_TOKEN)
        _integer(vector.get("dimensions"), minimum=1, maximum=65_536)
        _sha(vector.get("artifact_sha256"))
        purposes.append(str(vector["purpose"]))
    if purposes != sorted(set(purposes)) or (status == "succeeded" and not vectors):
        _fail()
    return row


def _quality_output(value: object, status: str) -> dict[str, object]:
    row = _closed(value, {"aesthetic_score", "technical_quality_score"})
    aesthetic = _number(
        row.get("aesthetic_score"), minimum=0, maximum=1, nullable=True
    )
    _number(
        row.get("technical_quality_score"),
        minimum=0,
        maximum=1,
        nullable=True,
    )
    if status == "succeeded" and aesthetic is None:
        _fail()
    return row


_OUTPUT_VALIDATORS = {
    "metadata": _metadata_output,
    "geocode": _geocode_output,
    "vision": _vision_output,
    "embedding": _embedding_output,
    "quality": _quality_output,
}


def _result_document(value: object) -> dict[str, Any]:
    result = _strict_object(value, _RESULT_LIMITS)
    _closed(
        result,
        {
            "object",
            "schema_version",
            "asset_id",
            "asset_sha256",
            "analysis_run_id",
            "revision",
            "source_binding",
            "analysis_profile",
            "stages",
            "content_sha256",
        },
    )
    if result.get("object") != "memolens.image_analysis_result" or result.get(
        "schema_version"
    ) != "1":
        _fail()
    asset_sha256 = _sha(result.get("asset_sha256"))
    asset_id = _text(
        result.get("asset_id"), pattern=_ASSET_ID, minimum=30, maximum=30
    )
    if asset_id != f"asset_{str(asset_sha256)[:24]}":
        _fail()
    _text(
        result.get("analysis_run_id"),
        pattern=_ANALYSIS_RUN_ID,
        minimum=37,
        maximum=37,
    )
    _integer(result.get("revision"), minimum=1, maximum=1_800_000)
    source = _closed(
        result.get("source_binding"),
        {
            "source_id",
            "library_root_id",
            "observed_size",
            "observed_mtime_ns",
            "file_identity_sha256",
        },
    )
    _text(source.get("source_id"), pattern=_SOURCE_ID, minimum=28, maximum=28)
    _text(
        source.get("library_root_id"), pattern=_ROOT_ID, minimum=29, maximum=29
    )
    _integer(source.get("observed_size"), minimum=1, maximum=8_589_934_592)
    _integer(
        source.get("observed_mtime_ns"),
        minimum=0,
        maximum=9_223_372_036_854_775_807,
    )
    _sha(source.get("file_identity_sha256"))
    profile = _closed(
        result.get("analysis_profile"), {"id", "version", "content_sha256"}
    )
    _text(profile.get("id"), pattern=_TOKEN)
    _text(profile.get("version"), pattern=_TOKEN)
    _sha(profile.get("content_sha256"))
    stages = _closed(result.get("stages"), set(_STAGES))
    for stage_name in _STAGES:
        stage = _closed(
            stages.get(stage_name),
            {"status", "provenance", "output", "artifact_sha256", "reason_code"},
        )
        status = stage.get("status")
        if status not in _STAGE_STATUSES:
            _fail()
        _provenance(stage.get("provenance"))
        _sha(stage.get("artifact_sha256"), nullable=True)
        _text(
            stage.get("reason_code"),
            pattern=_REASON_CODE,
            maximum=120,
            nullable=True,
        )
        if status in {"succeeded", "partial"}:
            if stage.get("output") is None:
                _fail()
            _OUTPUT_VALIDATORS[stage_name](stage["output"], str(status))
        elif stage.get("output") is not None:
            _fail()
        if status == "succeeded" and stage.get("reason_code") is not None:
            _fail()
        if status != "succeeded" and stage.get("reason_code") is None:
            _fail()
    metadata = stages["metadata"]
    assert isinstance(metadata, dict)
    if (
        metadata["status"] == "succeeded"
        and isinstance(metadata["output"], dict)
        and metadata["output"]["file_size"] != source["observed_size"]
    ):
        _fail()
    _require_self_digest(result, digest_field="content_sha256", kind="result")
    return result


def _sealed_document(
    value: object,
    *,
    fields: set[str],
    digest_field: str,
    kind: str,
    limits: StrictJsonLimits = _RECEIPT_LIMITS,
) -> dict[str, Any]:
    document = _strict_object(value, limits)
    _closed(document, fields)
    _require_self_digest(document, digest_field=digest_field, kind=kind)
    return document


def _publish_document(value: object) -> dict[str, Any]:
    document = _sealed_document(
        value,
        fields={
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
            "publish_receipt_sha256",
        },
        digest_field="publish_receipt_sha256",
        kind="publish",
    )
    if (
        document.get("object") != "memolens.image_publish_receipt"
        or document.get("schema_version") != "1"
    ):
        _fail()
    _text(
        document.get("database_uuid"),
        pattern=_DATABASE_UUID,
        minimum=36,
        maximum=36,
    )
    _text(document.get("job_id"), pattern=_JOB_ID, minimum=36, maximum=36)
    _sha(document.get("request_sha256"))
    _text(document.get("asset_id"), pattern=_ASSET_ID, minimum=30, maximum=30)
    _sha(document.get("source_binding_sha256"))
    prior = document.get("expected_prior_head")
    if prior is not None:
        _binding(prior)
    result = _binding(document.get("result_head"))
    _integer(
        document.get("change_position"),
        minimum=1,
        maximum=9_223_372_036_854_775_807,
    )
    _sha(document.get("change_sha256"))
    expected_revision = 1 if prior is None else int(prior["revision"]) + 1
    if result.get("revision") != expected_revision:
        _fail()
    return document


def _projection_receipt_document(value: object) -> dict[str, Any]:
    document = _sealed_document(
        value,
        fields={
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
            "receipt_sha256",
        },
        digest_field="receipt_sha256",
        kind="projection_receipt",
    )
    if (
        document.get("object") != "memolens.image_projection_receipt"
        or document.get("schema_version") != "1"
        or document.get("projection_contract") != _PROJECTION_CONTRACT
    ):
        _fail()
    _text(document.get("projector_version"), pattern=_TOKEN)
    _text(
        document.get("database_uuid"),
        pattern=_DATABASE_UUID,
        minimum=36,
        maximum=36,
    )
    _integer(
        document.get("change_position"),
        minimum=1,
        maximum=9_223_372_036_854_775_807,
    )
    _text(document.get("asset_id"), pattern=_ASSET_ID, minimum=30, maximum=30)
    _binding(document.get("analysis_binding"))
    source_digest = _sha(document.get("source_binding_sha256"), nullable=True)
    row_digest = _sha(document.get("projected_row_sha256"), nullable=True)
    _sha(document.get("alias_set_sha256"))
    outcome = document.get("outcome")
    if outcome not in {"applied", "no_change", "superseded", "removed", "blocked"}:
        _fail()
    reason = _text(
        document.get("reason_code"),
        pattern=_REASON_CODE,
        maximum=120,
        nullable=True,
    )
    if outcome in {"applied", "no_change", "superseded"} and (
        source_digest is None or row_digest is None
    ):
        _fail()
    if outcome in {"removed", "blocked"} and row_digest is not None:
        _fail()
    if outcome in {"applied", "no_change"} and reason is not None:
        _fail()
    if outcome in {"superseded", "removed", "blocked"} and reason is None:
        _fail()
    return document


def _stage_output(result: Mapping[str, object], stage_name: str) -> dict[str, Any] | None:
    stages = result["stages"]
    assert isinstance(stages, dict)
    stage = stages[stage_name]
    assert isinstance(stage, dict)
    if stage["status"] not in {"succeeded", "partial"}:
        return None
    output = stage["output"]
    assert isinstance(output, dict)
    return output


def _combined_text(
    vision: Mapping[str, object] | None,
    geocode: Mapping[str, object] | None,
) -> str:
    description_value = vision.get("description") if vision is not None else None
    description = (
        description_value.strip() if type(description_value) is str else ""
    )
    tags_value = vision.get("tags") if vision is not None else None
    tags = (
        sorted(
            {
                tag.strip()
                for tag in tags_value
                if type(tag) is str and tag.strip()
            }
        )
        if type(tags_value) is list
        else []
    )
    place_value = geocode.get("place_name") if geocode is not None else None
    country_value = geocode.get("country") if geocode is not None else None
    place = place_value.strip() if type(place_value) is str and place_value.strip() else None
    country = (
        country_value.strip()
        if type(country_value) is str and country_value.strip()
        else None
    )
    hint_value = vision.get("location_hint") if vision is not None else None
    hint = hint_value.strip() if type(hint_value) is str else ""
    parts: list[str] = []
    if description:
        parts.append(description)
    if tags:
        parts.append(f"Tags: {', '.join(tags)}.")
    location = [item for item in (place, country) if item is not None]
    if location:
        parts.append(f"Location: {', '.join(location)}.")
    elif hint:
        parts.append(f"Possible location or landmark: {hint}.")
        parts.append(f"Location hint keywords: {hint}.")
    return " ".join(parts)


def _decode_b64(value: object, *, nullable: bool) -> bytes | None:
    if value is None and nullable:
        return None
    if type(value) is not str or not value:
        _fail()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        _fail()
    if not decoded or base64.b64encode(decoded).decode("ascii") != value:
        _fail()
    return decoded


def _expected_artifacts(result: Mapping[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    stages = result["stages"]
    assert isinstance(stages, dict)
    expected: dict[tuple[str, str], dict[str, object]] = {}
    for stage_name in _STAGES:
        stage = stages[stage_name]
        assert isinstance(stage, dict)
        digest = stage.get("artifact_sha256")
        if type(digest) is str:
            expected[(stage_name, "stage_output")] = {
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": digest,
            }
    embedding = _stage_output(result, "embedding")
    if embedding is not None:
        vectors = embedding["vectors"]
        assert isinstance(vectors, list)
        for vector in vectors:
            assert isinstance(vector, dict)
            expected[("embedding", f"vector:{vector['purpose']}")] = {
                "signal": vector["signal"],
                "model_id": vector["model_id"],
                "dimensions": vector["dimensions"],
                "artifact_sha256": vector["artifact_sha256"],
            }
    return expected


def _artifacts(
    connection: sqlite3.Connection,
    *,
    asset_id: str,
    analysis_run_id: str,
    result: Mapping[str, object],
) -> dict[tuple[str, str], dict[str, object]]:
    rows = connection.execute(
        """SELECT stage,name,media_type,signal,model_id,dimensions,
                  artifact_sha256,size_bytes,artifact_blob
             FROM image_analysis_artifacts
            WHERE asset_id=? AND analysis_run_id=? ORDER BY stage,name""",
        (asset_id, analysis_run_id),
    ).fetchall()
    expected = _expected_artifacts(result)
    observed: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        stage = row["stage"]
        name = row["name"]
        media_type = row["media_type"]
        payload_value = row["artifact_blob"]
        size_bytes = row["size_bytes"]
        if (
            type(stage) is not str
            or type(name) is not str
            or type(media_type) is not str
            or not 1 <= len(media_type) <= 200
            or type(payload_value) is not bytes
            or type(size_bytes) is not int
            or not 1 <= size_bytes <= 64 * 1024 * 1024
            or not 1 <= len(payload_value) <= 64 * 1024 * 1024
        ):
            _fail()
        identity = (stage, name)
        payload = payload_value
        if identity in observed or identity not in expected:
            _fail()
        required = expected[identity]
        if (
            len(payload) != size_bytes
            or hashlib.sha256(payload).hexdigest() != row["artifact_sha256"]
            or row["artifact_sha256"] != required["artifact_sha256"]
            or row["signal"] != required["signal"]
            or row["model_id"] != required["model_id"]
            or row["dimensions"] != required["dimensions"]
        ):
            _fail()
        if name.startswith("vector:"):
            dimensions = row["dimensions"]
            if type(dimensions) is not int or len(payload) != dimensions * 4:
                _fail()
        observed[identity] = {
            "media_type": media_type,
            "signal": row["signal"],
            "model_id": row["model_id"],
            "dimensions": row["dimensions"],
            "artifact_sha256": str(row["artifact_sha256"]),
            "bytes": payload,
        }
    if set(observed) != set(expected):
        _fail()
    return observed


def _result_vector(
    result: Mapping[str, object], purpose: str
) -> dict[str, Any] | None:
    embedding = _stage_output(result, "embedding")
    if embedding is None:
        return None
    vectors = embedding["vectors"]
    assert isinstance(vectors, list)
    for vector in vectors:
        assert isinstance(vector, dict)
        if vector["purpose"] == purpose:
            return vector
    return None


def _vector_binding(
    purpose: str,
    vector: Mapping[str, object],
    artifact: Mapping[str, object],
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "signal": vector["signal"],
        "model_id": vector["model_id"],
        "backend": f"{vector['signal']}:{vector['model_id']}",
        "dimensions": vector["dimensions"],
        "media_type": artifact["media_type"],
        "artifact_sha256": artifact["artifact_sha256"],
    }


def _render_projected_row(
    connection: sqlite3.Connection,
    *,
    result: Mapping[str, object],
    relative_path: str,
    projector_version: str,
) -> dict[str, object]:
    if (
        type(relative_path) is not str
        or not 1 <= len(relative_path) <= 8_192
        or "\x00" in relative_path
    ):
        _fail()
    path = PurePosixPath(relative_path)
    filename = path.name
    if (
        path.is_absolute()
        or not 1 <= len(filename) <= 1_024
        or filename in {".", ".."}
        or "/" in filename
        or "\x00" in filename
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != relative_path
    ):
        _fail()
    metadata = _stage_output(result, "metadata")
    embedding = _stage_output(result, "embedding")
    image_vector = _result_vector(result, "image_search")
    if metadata is None or embedding is None or image_vector is None:
        _fail()
    vision = _stage_output(result, "vision")
    geocode = _stage_output(result, "geocode")
    quality = _stage_output(result, "quality")
    text_vector = _result_vector(result, "text_search")
    if text_vector is not None and text_vector["signal"] != "text_derived":
        _fail()
    artifacts = _artifacts(
        connection,
        asset_id=str(result["asset_id"]),
        analysis_run_id=str(result["analysis_run_id"]),
        result=result,
    )
    image_artifact = artifacts[("embedding", "vector:image_search")]
    text_artifact = artifacts.get(("embedding", "vector:text_search"))
    combined_text = _combined_text(vision, geocode)
    combined_digest = embedding.get("combined_text_sha256")
    if (
        (text_vector is not None or image_vector["signal"] == "text_derived")
        and combined_digest is None
    ) or (
        type(combined_digest) is str
        and hashlib.sha256(combined_text.encode("utf-8")).hexdigest()
        != combined_digest
    ):
        _fail()
    image_payload = image_artifact["bytes"]
    assert isinstance(image_payload, bytes)
    text_payload = text_artifact["bytes"] if text_artifact is not None else None
    assert text_payload is None or isinstance(text_payload, bytes)
    vision_tags = vision.get("tags") if vision is not None else []
    tags = sorted(
        {
            tag.strip()
            for tag in vision_tags
            if type(tag) is str and tag.strip()
        }
    )
    description_value = vision.get("description") if vision is not None else None
    description = (
        description_value.strip() if type(description_value) is str else ""
    )
    place_value = geocode.get("place_name") if geocode is not None else None
    country_value = geocode.get("country") if geocode is not None else None
    place = place_value.strip() if type(place_value) is str and place_value.strip() else None
    country = (
        country_value.strip()
        if type(country_value) is str and country_value.strip()
        else None
    )
    quality_stage = result["stages"]["quality"]  # type: ignore[index]
    assert isinstance(quality_stage, dict)
    quality_provenance = quality_stage["provenance"]
    assert isinstance(quality_provenance, dict)
    row: dict[str, object] = {
        "id": result["asset_id"],
        "sha256": result["asset_sha256"],
        "filename": filename,
        "relative_path": relative_path,
        "mime_type": metadata["mime_type"],
        "file_size": metadata["file_size"],
        "width": metadata["width"],
        "height": metadata["height"],
        "taken_at": metadata["taken_at"],
        "lat": metadata["latitude"],
        "lon": metadata["longitude"],
        "altitude": metadata["altitude"],
        "place_name": place,
        "country": country,
        "description": description,
        "tags_json": json.dumps(
            tags,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ),
        "combined_text": combined_text,
        "text_embedding_model": (
            text_vector["model_id"] if text_vector is not None else None
        ),
        "combined_text_embedding_b64": (
            base64.b64encode(text_payload).decode("ascii")
            if text_payload is not None
            else None
        ),
        "embedding_backend": f"{image_vector['signal']}:{image_vector['model_id']}",
        "embedding_b64": base64.b64encode(image_payload).decode("ascii"),
        "aesthetic_score": (
            quality.get("aesthetic_score") if quality is not None else None
        ),
        "aesthetic_model": (
            quality_provenance.get("model_id") if quality is not None else None
        ),
        "technical_quality_score": (
            quality.get("technical_quality_score") if quality is not None else None
        ),
        "aesthetic_updated_at": None,
        "created_at": None,
        "updated_at": None,
    }
    normalized_row = deepcopy(row)
    normalized_row.pop("created_at")
    normalized_row.pop("updated_at")
    row_sha256 = _canonical_sha256(normalized_row)
    source = result["source_binding"]
    assert isinstance(source, dict)
    return {
        "object": _PROJECTED_ROW_OBJECT,
        "schema_version": "1",
        "projection_contract": _PROJECTION_CONTRACT,
        "projector_version": projector_version,
        "asset_id": result["asset_id"],
        "analysis_binding": {
            "analysis_run_id": result["analysis_run_id"],
            "revision": result["revision"],
            "content_sha256": result["content_sha256"],
        },
        "source_binding_sha256": _canonical_sha256(source),
        "row": row,
        "vector_bindings": {
            "image_search": _vector_binding(
                "image_search", image_vector, image_artifact
            ),
            "text_search": (
                _vector_binding("text_search", text_vector, text_artifact)
                if text_vector is not None and text_artifact is not None
                else None
            ),
        },
        "row_sha256": row_sha256,
    }


def _typed_stages(result: Mapping[str, object]) -> dict[str, object]:
    stages = result["stages"]
    assert isinstance(stages, dict)
    projected: dict[str, object] = {}
    for name in _STAGES:
        stage = stages[name]
        assert isinstance(stage, dict)
        output = stage.get("output")
        if name == "embedding" and isinstance(output, dict):
            raw_vectors = output.get("vectors")
            vectors = raw_vectors if isinstance(raw_vectors, list) else []
            safe_output: dict[str, object] | None = {
                "combined_text_sha256": output.get("combined_text_sha256"),
                "vectors": [
                    {
                        "purpose": vector.get("purpose"),
                        "signal": vector.get("signal"),
                        "model_id": vector.get("model_id"),
                        "dimensions": vector.get("dimensions"),
                    }
                    for vector in vectors
                    if isinstance(vector, dict)
                ],
            }
        else:
            safe_output = deepcopy(output) if isinstance(output, dict) else None
        projected[name] = {
            "status": stage["status"],
            "provenance": deepcopy(stage["provenance"]),
            "output": safe_output,
            "reason_code": stage["reason_code"],
        }
    return projected


def _require_active_read_manifest(
    connection: sqlite3.Connection,
    *,
    generation_id: str,
    projection: sqlite3.Row,
    canonical_manifest: Mapping[str, object],
) -> None:
    """Require the V17 physical-read seal to equal the canonical manifest."""

    rows = connection.execute(
        """SELECT generation_id,previous_generation_id,database_uuid,
                  canonical_high_water_position,compiler_id,projector_version,
                  eligible_count,projected_count,alias_count,missing_count,
                  unexpected_count,mismatched_count,blocked_count,
                  alias_set_sha256,row_set_sha256,manifest_json,
                  manifest_sha256,created_at
             FROM image_projection_read_manifests
            WHERE generation_id=? LIMIT 2""",
        (generation_id,),
    ).fetchall()
    if len(rows) != 1:
        _fail()
    read = rows[0]
    read_manifest = _sealed_document(
        read["manifest_json"],
        fields=_MANIFEST_FIELDS,
        digest_field="manifest_sha256",
        kind="manifest",
        limits=_MANIFEST_LIMITS,
    )
    canonical_columns = {
        "database_uuid": "manifest_database_uuid",
        "canonical_high_water_position": "manifest_high_water",
        "compiler_id": "manifest_compiler_id",
        "projector_version": "manifest_projector_version",
        "eligible_count": "eligible_count",
        "projected_count": "projected_count",
        "alias_count": "alias_count",
        "missing_count": "missing_count",
        "unexpected_count": "unexpected_count",
        "mismatched_count": "mismatched_count",
        "blocked_count": "blocked_count",
        "alias_set_sha256": "manifest_alias_set_sha256",
        "row_set_sha256": "manifest_row_set_sha256",
        "manifest_json": "manifest_json",
        "manifest_sha256": "manifest_sha256",
    }
    if (
        read["generation_id"] != generation_id
        or type(read["created_at"]) is not str
        or not read["created_at"]
        or any(
            read[read_column] != projection[canonical_column]
            for read_column, canonical_column in canonical_columns.items()
        )
        or read_manifest != canonical_manifest
        or read["eligible_count"] != read["projected_count"]
        or any(
            int(read[field] or 0) != 0
            for field in (
                "missing_count",
                "unexpected_count",
                "mismatched_count",
                "blocked_count",
            )
        )
    ):
        _fail()

    previous_generation_id = read["previous_generation_id"]
    if previous_generation_id is None:
        return
    if (
        type(previous_generation_id) is not str
        or not previous_generation_id
        or previous_generation_id == generation_id
    ):
        _fail()
    predecessors = connection.execute(
        """SELECT database_uuid,projection_contract,status
             FROM image_projection_generations WHERE id=? LIMIT 2""",
        (previous_generation_id,),
    ).fetchall()
    if len(predecessors) != 1:
        _fail()
    predecessor = predecessors[0]
    if (
        predecessor["database_uuid"] != read["database_uuid"]
        or predecessor["projection_contract"] != _PROJECTION_CONTRACT
        or predecessor["status"] != "complete"
    ):
        _fail()


def _require_full_physical_census(
    connection: sqlite3.Connection,
    *,
    stored_rows: list[sqlite3.Row],
    projector_version: str,
    alias_set_sha256: str,
) -> None:
    """Match the complete mutable compatibility table to one sealed generation."""

    physical_columns = ",".join(_IMAGE_INDEX_COLUMNS)
    physical_rows = connection.execute(
        f"SELECT {physical_columns} FROM image_index ORDER BY id"
    ).fetchall()
    if [row["id"] for row in physical_rows] != [
        row["asset_id"] for row in stored_rows
    ]:
        _fail()

    for stored, physical in zip(stored_rows, physical_rows, strict=True):
        projected = _strict_object(stored["row_json"], _MANIFEST_LIMITS)
        _closed(projected, _PROJECTED_ROW_DOCUMENT_FIELDS)
        binding = _binding(projected.get("analysis_binding"))
        projected_row = _closed(projected.get("row"), set(_PROJECTED_ROW_FIELDS))
        normalized_row = deepcopy(projected_row)
        normalized_row.pop("created_at")
        normalized_row.pop("updated_at")
        if (
            projected.get("object") != _PROJECTED_ROW_OBJECT
            or projected.get("schema_version") != "1"
            or projected.get("projection_contract") != _PROJECTION_CONTRACT
            or projected.get("projector_version") != projector_version
            or projected.get("asset_id") != stored["asset_id"]
            or binding
            != {
                "analysis_run_id": stored["analysis_run_id"],
                "revision": stored["revision"],
                "content_sha256": stored["content_sha256"],
            }
            or projected.get("source_binding_sha256")
            != stored["source_binding_sha256"]
            or projected.get("row_sha256") != stored["row_sha256"]
            or stored["alias_set_sha256"] != alias_set_sha256
            or projected_row.get("id") != stored["asset_id"]
            or projected_row.get("created_at") is not None
            or projected_row.get("updated_at") is not None
            or _canonical_sha256(normalized_row) != stored["row_sha256"]
        ):
            _fail()
        _sha(projected.get("source_binding_sha256"))
        _sha(projected.get("row_sha256"))

        expected_physical: dict[str, object] = {}
        for column in _IMAGE_INDEX_COLUMNS:
            if column == "combined_text_embedding":
                expected_physical[column] = _decode_b64(
                    projected_row["combined_text_embedding_b64"],
                    nullable=True,
                )
            elif column == "embedding":
                expected_physical[column] = _decode_b64(
                    projected_row["embedding_b64"],
                    nullable=False,
                )
            elif column in {"created_at", "updated_at"}:
                expected_physical[column] = physical[column]
                if type(physical[column]) is not str or not physical[column]:
                    _fail()
            else:
                expected_physical[column] = projected_row[column]
        if any(
            physical[column] != expected_physical[column]
            for column in _IMAGE_INDEX_COLUMNS
        ):
            _fail()


class CanonicalImageObservationReader:
    """Verify one canonical image observation inside a SQLite snapshot."""

    @staticmethod
    def _unavailable(
        asset_id: object,
        reason_code: str,
        *,
        analysis_binding: Mapping[str, object] | None = None,
        source_binding_sha256: str | None = None,
    ) -> dict[str, object]:
        return {
            "object": "memolens.canonical_image_observation",
            "schema_version": "1",
            "status": "unavailable",
            "authority": "canonical_image_analysis",
            "provenance_status": "unavailable",
            "asset_id": asset_id,
            "analysis_binding": (
                dict(analysis_binding) if analysis_binding is not None else None
            ),
            "source_binding_sha256": source_binding_sha256,
            "projection": {
                "status": "unavailable",
                "generation_id": None,
                "processing_generation_id": None,
                "receipt_sha256": None,
                "row_sha256": None,
                "reason_code": reason_code,
            },
            "stages": None,
            "reason_code": reason_code,
        }

    @staticmethod
    def _pending(
        asset_id: str,
        analysis_binding: Mapping[str, object],
        source_binding_sha256: str,
        *,
        generation_id: object = None,
        processing_generation_id: object = None,
        receipt_sha256: object = None,
        reason_code: str = "projection_not_published",
    ) -> dict[str, object]:
        return {
            "object": "memolens.canonical_image_observation",
            "schema_version": "1",
            "status": "pending",
            "authority": "canonical_image_analysis",
            "provenance_status": "verified_pending",
            "asset_id": asset_id,
            "analysis_binding": dict(analysis_binding),
            "source_binding_sha256": source_binding_sha256,
            "projection": {
                "status": "pending",
                "generation_id": generation_id,
                "processing_generation_id": processing_generation_id,
                "receipt_sha256": receipt_sha256,
                "row_sha256": None,
                "reason_code": reason_code,
            },
            "stages": None,
            "reason_code": reason_code,
        }

    @staticmethod
    def _verified_generation(
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        database_uuid: str,
        minimum_high_water_position: int,
        asset_id: str,
        analysis_binding: Mapping[str, object],
        source_binding_sha256: str,
        source_relative_path: str,
        result: Mapping[str, object],
        expected_projector_version: str | None,
        expected_row_sha256: str | None,
        expected_alias_set_sha256: str | None,
        require_active: bool,
        require_physical: bool,
    ) -> dict[str, object] | None:
        generation_rows = connection.execute(
            """SELECT generation.database_uuid,
                      generation.projection_contract,
                      generation.canonical_high_water_position,
                      generation.compiler_id,generation.projector_version,
                      generation.status,generation.is_active,
                      projected.analysis_run_id AS projected_run_id,
                      projected.revision AS projected_revision,
                      projected.content_sha256 AS projected_content_sha256,
                      projected.source_id AS projected_source_id,
                      projected.source_binding_sha256 AS projected_source_sha256,
                      projected.row_json,projected.row_sha256,
                      projected.alias_set_sha256 AS projected_alias_set_sha256,
                      manifest.database_uuid AS manifest_database_uuid,
                      manifest.canonical_high_water_position AS manifest_high_water,
                      manifest.compiler_id AS manifest_compiler_id,
                      manifest.projector_version AS manifest_projector_version,
                      manifest.eligible_count,manifest.projected_count,
                      manifest.alias_count,manifest.missing_count,
                      manifest.unexpected_count,manifest.mismatched_count,
                      manifest.blocked_count,
                      manifest.alias_set_sha256 AS manifest_alias_set_sha256,
                      manifest.row_set_sha256 AS manifest_row_set_sha256,
                      manifest.manifest_json,manifest.manifest_sha256
                 FROM image_projection_generations generation
                 LEFT JOIN image_projection_rows projected
                   ON projected.generation_id=generation.id
                  AND projected.asset_id=?
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=generation.id
                WHERE generation.id=? LIMIT 2""",
            (asset_id, generation_id),
        ).fetchall()
        if len(generation_rows) != 1:
            _fail()
        projection = generation_rows[0]
        if (
            projection["database_uuid"] != database_uuid
            or projection["projection_contract"] != _PROJECTION_CONTRACT
            or int(projection["canonical_high_water_position"] or -1)
            < minimum_high_water_position
            or (
                expected_projector_version is not None
                and projection["projector_version"]
                != expected_projector_version
            )
        ):
            _fail()
        if projection["status"] != "complete":
            return None
        if require_active and int(projection["is_active"] or 0) != 1:
            return None
        result_source = result.get("source_binding")
        if (
            not isinstance(result_source, dict)
            or projection["manifest_json"] is None
            or projection["row_json"] is None
            or projection["manifest_database_uuid"] != database_uuid
            or projection["manifest_high_water"]
            != projection["canonical_high_water_position"]
            or projection["manifest_compiler_id"] != projection["compiler_id"]
            or projection["manifest_projector_version"]
            != projection["projector_version"]
            or projection["eligible_count"] != projection["projected_count"]
            or any(
                int(projection[field] or 0) != 0
                for field in (
                    "missing_count",
                    "unexpected_count",
                    "mismatched_count",
                    "blocked_count",
                )
            )
            or projection["projected_run_id"]
            != analysis_binding["analysis_run_id"]
            or projection["projected_revision"] != analysis_binding["revision"]
            or projection["projected_content_sha256"]
            != analysis_binding["content_sha256"]
            or projection["projected_source_id"] != result_source.get("source_id")
            or projection["projected_source_sha256"] != source_binding_sha256
            or (
                expected_row_sha256 is not None
                and projection["row_sha256"] != expected_row_sha256
            )
            or (
                expected_alias_set_sha256 is not None
                and projection["projected_alias_set_sha256"]
                != expected_alias_set_sha256
            )
        ):
            _fail()

        manifest = _sealed_document(
            projection["manifest_json"],
            fields=_MANIFEST_FIELDS,
            digest_field="manifest_sha256",
            kind="manifest",
            limits=_MANIFEST_LIMITS,
        )
        _text(manifest.get("projector_version"), pattern=_TOKEN, maximum=200)
        _text(manifest.get("compiler_version"), pattern=_TOKEN, maximum=200)
        _text(
            manifest.get("database_uuid"),
            pattern=_DATABASE_UUID,
            minimum=36,
            maximum=36,
        )
        _integer(
            manifest.get("canonical_high_water_mark"),
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        _sha(manifest.get("alias_set_sha256"))
        _sha(manifest.get("row_set_sha256"))
        entries = _manifest_entries(manifest.get("entries"))
        counts = _manifest_counts(manifest.get("counts"), entries)
        _manifest_reason_counts(manifest.get("reason_counts"), entries)
        manifest_row_set = _manifest_row_set(entries)
        projected = _strict_object(projection["row_json"], _MANIFEST_LIMITS)
        expected_projected = _render_projected_row(
            connection,
            result=result,
            relative_path=source_relative_path,
            projector_version=str(projection["projector_version"]),
        )
        if projected != expected_projected:
            _fail()
        if (
            manifest.get("object") != "memolens.image_projection_manifest"
            or manifest.get("schema_version") != "1"
            or manifest.get("projection_contract") != _PROJECTION_CONTRACT
            or manifest.get("projector_version")
            != projection["projector_version"]
            or manifest.get("compiler_version") != projection["compiler_id"]
            or manifest.get("database_uuid") != database_uuid
            or manifest.get("canonical_high_water_mark")
            != projection["canonical_high_water_position"]
            or manifest.get("manifest_sha256") != projection["manifest_sha256"]
            or manifest.get("alias_set_sha256")
            != projection["manifest_alias_set_sha256"]
            or manifest.get("row_set_sha256")
            != projection["manifest_row_set_sha256"]
            or projected.get("asset_id") != asset_id
            or projected.get("analysis_binding") != analysis_binding
            or projected.get("source_binding_sha256") != source_binding_sha256
            or projected.get("row_sha256") != projection["row_sha256"]
        ):
            _fail()
        stored_rows = connection.execute(
            """SELECT asset_id,analysis_run_id,revision,content_sha256,
                      source_binding_sha256,row_json,row_sha256,alias_set_sha256
                 FROM image_projection_rows
                WHERE generation_id=? ORDER BY asset_id""",
            (generation_id,),
        ).fetchall()
        row_set = [
            {
                "asset_id": str(item["asset_id"]),
                "projected_row_sha256": str(item["row_sha256"]),
            }
            for item in stored_rows
        ]
        expected_entries = [
            {
                "asset_id": str(item["asset_id"]),
                "analysis_binding": {
                    "analysis_run_id": str(item["analysis_run_id"]),
                    "revision": int(item["revision"]),
                    "content_sha256": str(item["content_sha256"]),
                },
                "expected_row_sha256": str(item["row_sha256"]),
                "actual_row_sha256": str(item["row_sha256"]),
                "status": "matched",
                "reason_code": None,
            }
            for item in stored_rows
        ]
        alias_set = _projection_alias_set(
            connection,
            generation_id=generation_id,
            database_uuid=database_uuid,
        )
        stored_counts = {
            "eligible": projection["eligible_count"],
            "projected": projection["projected_count"],
            "aliases": projection["alias_count"],
            "missing": projection["missing_count"],
            "unexpected": projection["unexpected_count"],
            "mismatched": projection["mismatched_count"],
            "blocked": projection["blocked_count"],
        }
        if (
            counts != stored_counts
            or counts["eligible"] != counts["projected"]
            or counts["projected"] != len(row_set)
            or counts["aliases"] != len(alias_set)
            or any(
                counts[key] != 0
                for key in ("missing", "unexpected", "mismatched", "blocked")
            )
            or manifest_row_set != row_set
            or _canonical_sha256(row_set)
            != projection["manifest_row_set_sha256"]
            or _canonical_sha256(alias_set)
            != projection["manifest_alias_set_sha256"]
            or projection["projected_alias_set_sha256"]
            != projection["manifest_alias_set_sha256"]
            or entries != expected_entries
        ):
            _fail()

        if require_active:
            _require_active_read_manifest(
                connection,
                generation_id=generation_id,
                projection=projection,
                canonical_manifest=manifest,
            )
        if require_physical:
            _require_full_physical_census(
                connection,
                stored_rows=stored_rows,
                projector_version=str(projection["projector_version"]),
                alias_set_sha256=str(projection["manifest_alias_set_sha256"]),
            )
        return {
            "generation_id": generation_id,
            "row_sha256": str(projection["row_sha256"]),
            "canonical_high_water_position": int(
                projection["canonical_high_water_position"]
            ),
        }

    def read(
        self,
        connection: sqlite3.Connection,
        *,
        raw: Mapping[str, object],
    ) -> dict[str, object] | None:
        if raw.get("kind") != "image":
            return None
        asset_id = raw.get("id") or raw.get("asset_id")
        try:
            validate_exact_managed_schema_manifest(connection)
        except (MemoLensError, sqlite3.Error):
            return self._unavailable(
                asset_id,
                "canonical_image_schema_unavailable",
            )
        try:
            return self._read_exact(connection, asset_id=str(asset_id))
        except _AuthorityFailure as exc:
            return self._unavailable(asset_id, exc.code)
        except (sqlite3.Error, TypeError, ValueError, UnicodeError):
            return self._unavailable(
                asset_id,
                "canonical_image_projection_corrupt",
            )

    def _read_exact(
        self,
        connection: sqlite3.Connection,
        *,
        asset_id: str,
    ) -> dict[str, object]:
        publication = connection.execute(
            """SELECT meta.database_uuid,
                      head.analysis_run_id AS head_run_id,
                      head.revision AS head_revision,
                      head.content_sha256 AS head_content_sha256,
                      result.job_id,result.source_id AS result_source_id,
                      result.input_asset_sha256,result.source_binding_sha256,
                      result.analysis_profile_id,result.analysis_profile_version,
                      result.analysis_profile_sha256,
                      result.parent_analysis_run_id,result.parent_revision,
                      result.parent_content_sha256,result.result_json,
                      binding.database_uuid AS binding_database_uuid,
                      binding.database_device,binding.database_inode,
                      binding.asset_id AS binding_asset_id,
                      binding.analysis_run_id AS binding_run_id,
                      binding.intended_revision,binding.library_root_id,
                      binding.root_permission_fingerprint,
                      binding.source_id AS binding_source_id,
                      binding.relative_path,binding.observed_size,
                      binding.observed_mtime_ns,binding.observed_ctime_ns,
                      binding.source_device,binding.source_inode,
                      binding.file_identity_sha256,
                      binding.input_asset_sha256 AS binding_asset_sha256,
                      binding.source_binding_sha256 AS binding_source_sha256,
                      binding.analysis_profile_id AS binding_profile_id,
                      binding.analysis_profile_version AS binding_profile_version,
                      binding.analysis_profile_json,
                      binding.analysis_profile_sha256 AS binding_profile_sha256,
                      binding.expected_head_state,binding.expected_head_run_id,
                      binding.expected_head_revision,binding.expected_head_content_sha256,
                      binding.enqueue_scope,binding.idempotency_key,
                      binding.request_json,binding.request_sha256,
                      source.asset_id AS source_asset_id,
                      source.library_root_id AS source_root_id,
                      source.relative_path AS source_relative_path,
                      source.observed_size AS source_observed_size,
                      source.observed_mtime_ns AS source_observed_mtime_ns,
                      source.source_file_id,
                      source.availability AS source_availability,
                      root.status AS root_status,
                      root.permission_fingerprint AS root_permission_fingerprint_current,
                      asset.kind AS asset_kind,asset.sha256 AS asset_sha256,
                      asset.file_size AS asset_file_size,
                      job.kind AS job_kind,job.database_uuid AS job_database_uuid,
                      job.asset_id AS job_asset_id,
                      job.analysis_run_id AS job_analysis_run_id,
                      run.asset_id AS run_asset_id,run.parent_run_id,
                      run.analysis_profile_id AS run_profile_id,
                      run.analysis_profile_json AS run_profile_json,
                      run.input_asset_sha256 AS run_asset_sha256,
                      run.status AS run_status,
                      (SELECT COUNT(*) FROM image_analysis_attempt_authorities authority
                        WHERE authority.job_id=binding.job_id) AS attempt_authority_count,
                      (SELECT COUNT(*) FROM image_analysis_attempt_authorities authority
                        WHERE authority.job_id=binding.job_id
                          AND authority.request_sha256=binding.request_sha256
                          AND authority.database_uuid=binding.database_uuid
                          AND authority.database_device=binding.database_device
                          AND authority.database_inode=binding.database_inode)
                        AS exact_attempt_authority_count,
                      publish.database_uuid AS publish_database_uuid,
                      publish.asset_id AS publish_asset_id,
                      publish.analysis_run_id AS publish_run_id,
                      publish.revision AS publish_revision,
                      publish.content_sha256 AS publish_content_sha256,
                      publish.change_position,publish.request_sha256 AS publish_request_sha256,
                      publish.publish_receipt_json,publish.publish_receipt_sha256,
                      change.database_uuid AS change_database_uuid,
                      change.projection_contract,change.asset_id AS change_asset_id,
                      change.analysis_run_id AS change_run_id,
                      change.revision AS change_revision,
                      change.content_sha256 AS change_content_sha256,
                      change.source_id AS change_source_id,
                      change.source_binding_sha256 AS change_source_sha256,
                      change.operation,change.change_json,change.change_sha256
                 FROM image_analysis_heads head
                 JOIN image_analysis_results result
                   ON result.asset_id=head.asset_id
                  AND result.analysis_run_id=head.analysis_run_id
                  AND result.revision=head.revision
                  AND result.content_sha256=head.content_sha256
                 JOIN image_analysis_job_bindings binding ON binding.job_id=result.job_id
                 JOIN image_publish_receipts publish ON publish.job_id=result.job_id
                 JOIN image_projection_changes change ON change.position=publish.change_position
                 JOIN asset_sources source ON source.id=result.source_id
                 JOIN library_roots root ON root.id=source.library_root_id
                 JOIN assets asset ON asset.id=result.asset_id
                 JOIN media_jobs job ON job.id=result.job_id
                 JOIN analysis_runs run
                   ON run.id=result.analysis_run_id AND run.asset_id=result.asset_id
                 JOIN database_meta meta ON meta.singleton=1
                WHERE head.asset_id=? LIMIT 2""",
            (asset_id,),
        ).fetchall()
        if not publication:
            return self._unavailable(
                asset_id,
                "canonical_image_head_unavailable",
            )
        if len(publication) != 1:
            _fail()
        row = publication[0]
        database_uuid = _text(
            row["database_uuid"],
            pattern=_DATABASE_UUID,
            minimum=36,
            maximum=36,
        )
        result = _result_document(row["result_json"])
        analysis_binding = {
            "analysis_run_id": row["head_run_id"],
            "revision": row["head_revision"],
            "content_sha256": row["head_content_sha256"],
        }
        _binding(analysis_binding)
        source_binding = result["source_binding"]
        profile = result["analysis_profile"]
        assert isinstance(source_binding, dict)
        assert isinstance(profile, dict)
        source_digest = _canonical_sha256(source_binding)
        request = _request_document(row["request_json"])
        stored_profile = _profile_document(row["analysis_profile_json"])
        request_database = request["database_binding"]
        request_asset = request["asset_binding"]
        request_source = request["source_binding"]
        request_profile = request["analysis_profile"]
        request_intended = request["intended_publication"]
        request_idempotency = request["idempotency"]
        assert isinstance(request_database, dict)
        assert isinstance(request_asset, dict)
        assert isinstance(request_source, dict)
        assert isinstance(request_profile, dict)
        assert isinstance(request_intended, dict)
        assert isinstance(request_idempotency, dict)
        _sha(row["root_permission_fingerprint"])
        if (
            result["asset_id"] != asset_id
            or result["analysis_run_id"] != row["head_run_id"]
            or result["revision"] != row["head_revision"]
            or result["content_sha256"] != row["head_content_sha256"]
            or result["asset_sha256"] != row["input_asset_sha256"]
            or result["asset_sha256"] != row["binding_asset_sha256"]
            or result["asset_sha256"] != row["asset_sha256"]
            or result["source_binding"]["source_id"] != row["result_source_id"]
            or source_binding["source_id"] != row["binding_source_id"]
            or source_binding["library_root_id"] != row["library_root_id"]
            or source_binding["observed_size"] != row["observed_size"]
            or source_binding["observed_mtime_ns"] != row["observed_mtime_ns"]
            or source_binding["file_identity_sha256"] != row["file_identity_sha256"]
            or source_digest != row["source_binding_sha256"]
            or source_digest != row["binding_source_sha256"]
            or profile["id"] != row["analysis_profile_id"]
            or profile["version"] != row["analysis_profile_version"]
            or profile["content_sha256"] != row["analysis_profile_sha256"]
            or profile["id"] != row["binding_profile_id"]
            or profile["version"] != row["binding_profile_version"]
            or profile["content_sha256"] != row["binding_profile_sha256"]
            or database_uuid != row["binding_database_uuid"]
            or row["binding_asset_id"] != asset_id
            or row["binding_run_id"] != row["head_run_id"]
            or row["intended_revision"] != row["head_revision"]
            or row["source_asset_id"] != asset_id
            or row["source_root_id"] != row["library_root_id"]
            or row["source_relative_path"] != row["relative_path"]
            or row["source_observed_size"] != row["observed_size"]
            or row["source_observed_mtime_ns"] != row["observed_mtime_ns"]
            or str(row["source_file_id"] or "") != str(row["source_inode"])
            or row["source_availability"] != "available"
            or row["root_status"] != "active"
            or row["root_permission_fingerprint_current"]
            != row["root_permission_fingerprint"]
            or row["asset_kind"] != "image"
            or row["asset_file_size"] != row["observed_size"]
            or row["job_kind"] != "image_analysis"
            or row["job_database_uuid"] != database_uuid
            or row["job_asset_id"] != asset_id
            or row["job_analysis_run_id"] != row["binding_run_id"]
            or row["run_asset_id"] != asset_id
            or row["run_profile_id"] != row["binding_profile_id"]
            or row["run_asset_sha256"] != row["binding_asset_sha256"]
            or row["run_status"] != "succeeded"
            or int(row["attempt_authority_count"]) < 1
            or row["exact_attempt_authority_count"]
            != row["attempt_authority_count"]
            or str(row["request_json"]) != _canonical_json(request)
            or _canonical_sha256(request) != row["request_sha256"]
            or str(row["analysis_profile_json"]) != _canonical_json(stored_profile)
            or str(row["run_profile_json"]) != _canonical_json(stored_profile)
            or request_database
            != {
                "database_uuid": row["binding_database_uuid"],
                "database_device": row["database_device"],
                "database_inode": row["database_inode"],
            }
            or request_asset
            != {
                "asset_id": row["binding_asset_id"],
                "input_asset_sha256": row["binding_asset_sha256"],
            }
            or request_source
            != {
                "source_id": row["binding_source_id"],
                "library_root_id": row["library_root_id"],
                "relative_path": row["relative_path"],
                "observed_size": row["observed_size"],
                "observed_mtime_ns": row["observed_mtime_ns"],
                "observed_ctime_ns": row["observed_ctime_ns"],
                "source_device": row["source_device"],
                "source_inode": row["source_inode"],
                "file_identity_sha256": row["file_identity_sha256"],
            }
            or request_profile != stored_profile
            or request_profile
            != {
                "profile_id": row["binding_profile_id"],
                "profile_version": row["binding_profile_version"],
                "profile_sha256": row["binding_profile_sha256"],
            }
            or request_intended
            != {
                "analysis_run_id": row["binding_run_id"],
                "revision": row["intended_revision"],
            }
            or request_idempotency
            != {"scope": row["enqueue_scope"], "key": row["idempotency_key"]}
        ):
            _fail("canonical_image_source_binding_invalid")
        expected_prior = (
            None
            if row["expected_head_state"] == "missing"
            else {
                "analysis_run_id": row["expected_head_run_id"],
                "revision": row["expected_head_revision"],
                "content_sha256": row["expected_head_content_sha256"],
            }
        )
        parent_binding = (
            None
            if row["parent_analysis_run_id"] is None
            and row["parent_revision"] is None
            and row["parent_content_sha256"] is None
            else {
                "analysis_run_id": row["parent_analysis_run_id"],
                "revision": row["parent_revision"],
                "content_sha256": row["parent_content_sha256"],
            }
        )
        if parent_binding is not None:
            _binding(parent_binding)
        expected_revision = 1 if expected_prior is None else int(expected_prior["revision"]) + 1
        if (
            request.get("expected_head") != expected_prior
            or parent_binding != expected_prior
            or row["parent_run_id"]
            != (
                None
                if expected_prior is None
                else expected_prior["analysis_run_id"]
            )
            or analysis_binding["revision"] != expected_revision
        ):
            _fail()
        publish = _publish_document(row["publish_receipt_json"])
        change = _sealed_document(
            row["change_json"],
            fields={
                "object",
                "schema_version",
                "projection_contract",
                "database_uuid",
                "change_position",
                "asset_id",
                "analysis_binding",
                "source_binding_sha256",
                "operation",
                "change_sha256",
            },
            digest_field="change_sha256",
            kind="change",
        )
        if (
            publish.get("object") != "memolens.image_publish_receipt"
            or publish.get("schema_version") != "1"
            or publish.get("database_uuid") != database_uuid
            or publish.get("job_id") != row["job_id"]
            or publish.get("request_sha256") != row["request_sha256"]
            or publish.get("asset_id") != asset_id
            or publish.get("source_binding_sha256") != source_digest
            or publish.get("expected_prior_head") != expected_prior
            or publish.get("result_head") != analysis_binding
            or publish.get("change_position") != row["change_position"]
            or publish.get("change_sha256") != row["change_sha256"]
            or publish.get("publish_receipt_sha256")
            != row["publish_receipt_sha256"]
            or row["publish_database_uuid"] != database_uuid
            or row["publish_asset_id"] != asset_id
            or row["publish_run_id"] != analysis_binding["analysis_run_id"]
            or row["publish_revision"] != analysis_binding["revision"]
            or row["publish_content_sha256"] != analysis_binding["content_sha256"]
            or row["publish_request_sha256"] != row["request_sha256"]
            or change.get("object") != "memolens.image_projection_change"
            or change.get("schema_version") != "1"
            or change.get("projection_contract") != _PROJECTION_CONTRACT
            or change.get("database_uuid") != database_uuid
            or change.get("change_position") != row["change_position"]
            or change.get("asset_id") != asset_id
            or change.get("analysis_binding") != analysis_binding
            or change.get("source_binding_sha256") != source_digest
            or change.get("operation") != "upsert"
            or change.get("change_sha256") != row["change_sha256"]
            or row["change_database_uuid"] != database_uuid
            or row["projection_contract"] != _PROJECTION_CONTRACT
            or row["change_asset_id"] != asset_id
            or row["change_run_id"] != analysis_binding["analysis_run_id"]
            or row["change_revision"] != analysis_binding["revision"]
            or row["change_content_sha256"] != analysis_binding["content_sha256"]
            or row["change_source_id"] != row["binding_source_id"]
            or row["change_source_sha256"] != source_digest
            or row["operation"] != "upsert"
        ):
            _fail()

        projection_rows = connection.execute(
            """SELECT receipt.generation_id,receipt.projection_contract,
                      receipt.projector_version,receipt.database_uuid,
                      receipt.asset_id,receipt.analysis_run_id,receipt.revision,
                      receipt.content_sha256,receipt.source_binding_sha256,
                      receipt.projected_row_sha256,receipt.alias_set_sha256,
                      receipt.outcome,receipt.reason_code,receipt.receipt_json,
                      receipt.receipt_sha256,
                      generation.database_uuid AS generation_database_uuid,
                      generation.projection_contract AS generation_projection_contract,
                      generation.canonical_high_water_position,generation.compiler_id,
                      generation.projector_version AS generation_projector_version,
                      generation.status AS generation_status,
                      generation.is_active,
                      projected.analysis_run_id AS projected_run_id,
                      projected.revision AS projected_revision,
                      projected.content_sha256 AS projected_content_sha256,
                      projected.source_id AS projected_source_id,
                      projected.source_binding_sha256 AS projected_source_sha256,
                      projected.row_json,projected.row_sha256,
                      projected.alias_set_sha256 AS projected_alias_set_sha256,
                      manifest.database_uuid AS manifest_database_uuid,
                      manifest.canonical_high_water_position AS manifest_high_water,
                      manifest.compiler_id AS manifest_compiler_id,
                      manifest.projector_version AS manifest_projector_version,
                      manifest.eligible_count,manifest.projected_count,
                      manifest.alias_count,manifest.missing_count,
                      manifest.unexpected_count,manifest.mismatched_count,
                      manifest.blocked_count,
                      manifest.alias_set_sha256 AS manifest_alias_set_sha256,
                      manifest.row_set_sha256 AS manifest_row_set_sha256,
                      manifest.manifest_json,manifest.manifest_sha256
                 FROM image_projection_receipts receipt
                 LEFT JOIN image_projection_generations generation
                   ON generation.id=receipt.generation_id
                 LEFT JOIN image_projection_rows projected
                   ON projected.generation_id=receipt.generation_id
                  AND projected.asset_id=receipt.asset_id
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=receipt.generation_id
                WHERE receipt.change_position=? LIMIT 2""",
            (row["change_position"],),
        ).fetchall()
        if not projection_rows:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
            )
        if len(projection_rows) != 1:
            _fail()
        projection = projection_rows[0]
        receipt = _projection_receipt_document(projection["receipt_json"])
        if (
            receipt.get("object") != "memolens.image_projection_receipt"
            or receipt.get("schema_version") != "1"
            or receipt.get("projection_contract") != _PROJECTION_CONTRACT
            or receipt.get("projector_version") != projection["projector_version"]
            or receipt.get("database_uuid") != database_uuid
            or receipt.get("change_position") != row["change_position"]
            or receipt.get("asset_id") != asset_id
            or receipt.get("analysis_binding") != analysis_binding
            or receipt.get("source_binding_sha256") != source_digest
            or receipt.get("projected_row_sha256")
            != projection["projected_row_sha256"]
            or receipt.get("alias_set_sha256") != projection["alias_set_sha256"]
            or receipt.get("outcome") != projection["outcome"]
            or receipt.get("reason_code") != projection["reason_code"]
            or receipt.get("receipt_sha256") != projection["receipt_sha256"]
            or projection["projection_contract"] != _PROJECTION_CONTRACT
            or projection["database_uuid"] != database_uuid
            or projection["asset_id"] != asset_id
            or projection["analysis_run_id"] != analysis_binding["analysis_run_id"]
            or projection["revision"] != analysis_binding["revision"]
            or projection["content_sha256"] != analysis_binding["content_sha256"]
            or projection["source_binding_sha256"] != source_digest
        ):
            _fail()
        if projection["outcome"] not in {"applied", "no_change"}:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
                processing_generation_id=projection["generation_id"],
                receipt_sha256=projection["receipt_sha256"],
                reason_code=str(projection["reason_code"] or "projection_not_current"),
            )
        processing_generation_id = projection["generation_id"]
        if type(processing_generation_id) is not str:
            _fail()
        processing = self._verified_generation(
            connection,
            generation_id=processing_generation_id,
            database_uuid=database_uuid,
            minimum_high_water_position=int(row["change_position"]),
            asset_id=asset_id,
            analysis_binding=analysis_binding,
            source_binding_sha256=source_digest,
            source_relative_path=str(row["source_relative_path"]),
            result=result,
            expected_projector_version=str(projection["projector_version"]),
            expected_row_sha256=str(projection["projected_row_sha256"]),
            expected_alias_set_sha256=str(projection["alias_set_sha256"]),
            require_active=False,
            require_physical=False,
        )
        if processing is None:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
                processing_generation_id=processing_generation_id,
                receipt_sha256=projection["receipt_sha256"],
                reason_code="projection_generation_not_active",
            )
        latest = connection.execute(
            """SELECT MAX(position) AS global_position,
                      MAX(CASE WHEN asset_id=? THEN position END) AS asset_position
                 FROM image_projection_changes""",
            (asset_id,),
        ).fetchone()
        if (
            latest is None
            or type(latest["global_position"]) is not int
            or type(latest["asset_position"]) is not int
            or latest["asset_position"] != row["change_position"]
        ):
            _fail()
        global_latest_position = int(latest["global_position"])
        asset_latest_position = int(latest["asset_position"])
        active_rows = connection.execute(
            """SELECT id,canonical_high_water_position
                 FROM image_projection_generations
                WHERE database_uuid=? AND projection_contract=?
                  AND status='complete' AND is_active=1 LIMIT 2""",
            (database_uuid, _PROJECTION_CONTRACT),
        ).fetchall()
        if not active_rows:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
                processing_generation_id=processing_generation_id,
                receipt_sha256=projection["receipt_sha256"],
                reason_code="projection_generation_not_active",
            )
        if len(active_rows) != 1:
            _fail()
        active_generation_id = str(active_rows[0]["id"])
        active_high_water = _integer(
            active_rows[0]["canonical_high_water_position"],
            minimum=0,
            maximum=9_223_372_036_854_775_807,
        )
        assert active_high_water is not None
        if active_high_water > global_latest_position:
            _fail()
        if active_high_water < asset_latest_position:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
                generation_id=active_generation_id,
                processing_generation_id=processing_generation_id,
                receipt_sha256=projection["receipt_sha256"],
                reason_code="projection_generation_not_current",
            )
        active = self._verified_generation(
            connection,
            generation_id=active_generation_id,
            database_uuid=database_uuid,
            minimum_high_water_position=asset_latest_position,
            asset_id=asset_id,
            analysis_binding=analysis_binding,
            source_binding_sha256=source_digest,
            source_relative_path=str(row["source_relative_path"]),
            result=result,
            expected_projector_version=None,
            expected_row_sha256=None,
            expected_alias_set_sha256=None,
            require_active=True,
            require_physical=True,
        )
        if active is None:
            return self._pending(
                asset_id,
                analysis_binding,
                source_digest,
                generation_id=active_generation_id,
                processing_generation_id=processing_generation_id,
                receipt_sha256=projection["receipt_sha256"],
                reason_code="projection_generation_not_active",
            )
        return {
            "object": "memolens.canonical_image_observation",
            "schema_version": "1",
            "status": "current",
            "authority": "canonical_image_analysis",
            "provenance_status": "verified_current",
            "asset_id": asset_id,
            "analysis_binding": analysis_binding,
            "source_binding_sha256": source_digest,
            "projection": {
                "status": "current",
                "generation_id": active_generation_id,
                "processing_generation_id": processing_generation_id,
                "receipt_sha256": str(projection["receipt_sha256"]),
                "row_sha256": str(active["row_sha256"]),
                "reason_code": None,
            },
            "stages": _typed_stages(result),
            "reason_code": None,
        }
__all__ = ["CanonicalImageObservationReader"]
