"""Closed, path-safe contracts for durable canonical image-analysis jobs.

This module defines the execution envelope for ``image_analysis``.  It does
not grant filesystem or provider authority: a request binds an already
approved database, root, source, asset and analysis profile.  Checkpoints can
only carry completed-stage outcomes, content digests and the exact canonical
publication binding needed for recovery.

All documents are deterministic domain values.  Persistence may store clocks
and leases beside them, but those mutable envelope values are intentionally
absent here.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import PurePosixPath
import re
from typing import Any
import unicodedata


IMAGE_ANALYSIS_WORKER_KIND = "image_analysis"
IMAGE_ANALYSIS_REQUEST_OBJECT = "memolens.image_analysis_request"
IMAGE_ANALYSIS_JOB_OBJECT = "memolens.image_analysis_job"
IMAGE_ANALYSIS_CHECKPOINT_OBJECT = "memolens.image_analysis_checkpoint"
IMAGE_ANALYSIS_ERROR_OBJECT = "memolens.image_analysis_error"
IMAGE_ANALYSIS_IDEMPOTENCY_OBJECT = "memolens.image_analysis_idempotency_binding"
IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_OBJECT = "memolens.image_analysis_attempt_authority"
IMAGE_ANALYSIS_JOB_SCHEMA_VERSION = "1"

IMAGE_ANALYSIS_STAGES = (
    "queued",
    "source_admission",
    "metadata",
    "geocode",
    "vision",
    "embedding",
    "quality",
    "publish",
    "shadow_projection",
    "completed",
)
IMAGE_ANALYSIS_WORK_STAGES = IMAGE_ANALYSIS_STAGES[1:-1]
IMAGE_ANALYSIS_STAGE_OUTCOMES = frozenset({"succeeded", "partial", "unsupported", "disabled"})
IMAGE_ANALYSIS_JOB_STATUSES = frozenset(
    {
        "queued",
        "running",
        "cancelling",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    }
)
IMAGE_ANALYSIS_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
IMAGE_ANALYSIS_ERROR_CODES = frozenset(
    {
        "cancelled",
        "checkpoint_corrupt",
        "database_scope_changed",
        "head_conflict",
        "idempotency_conflict",
        "internal_error",
        "invalid_request",
        "profile_changed",
        "projection_failed",
        "provider_denied",
        "provider_unavailable",
        "publication_conflict",
        "root_scope_changed",
        "source_changed",
        "source_permission_denied",
        "source_unavailable",
        "stage_failed",
        "worker_interrupted",
    }
)
IMAGE_ANALYSIS_ATTEMPT_REASONS = frozenset(
    {
        "initial",
        "explicit_resume",
        "process_recovery",
        "runtime_handoff",
    }
)

IMAGE_ANALYSIS_REQUEST_MAX_CANONICAL_BYTES = 65_536
IMAGE_ANALYSIS_CHECKPOINT_MAX_CANONICAL_BYTES = 65_536
IMAGE_ANALYSIS_ERROR_MAX_CANONICAL_BYTES = 8_192
IMAGE_ANALYSIS_IDEMPOTENCY_MAX_CANONICAL_BYTES = 16_384
IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_MAX_CANONICAL_BYTES = 32_768
IMAGE_ANALYSIS_JOB_MAX_CANONICAL_BYTES = 196_608
IMAGE_ANALYSIS_MAX_ATTEMPTS = 100
IMAGE_ANALYSIS_MAX_ERRORS = 64
IMAGE_ANALYSIS_MAX_DEPTH = 16
IMAGE_ANALYSIS_MAX_NODES = 4_096
IMAGE_ANALYSIS_MAX_ARTIFACTS_PER_STAGE = 16
_SQLITE_INT64_MAX = 9_223_372_036_854_775_807
_MAX_IMAGE_SOURCE_BYTES = 8_589_934_592

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^asset_[0-9a-f]{24}$")
_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_ROOT_ID = re.compile(r"^root_[0-9a-f]{24}$")
_JOB_ID = re.compile(r"^job_[0-9a-f]{32}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_DATABASE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_RUNTIME_GENERATION = re.compile(r"^runtime_generation_[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+~-]{0,255}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,119}$")

_DATABASE_BINDING_FIELDS = {
    "database_uuid",
    "database_device",
    "database_inode",
}
_ASSET_BINDING_FIELDS = {"asset_id", "input_asset_sha256"}
_SOURCE_BINDING_FIELDS = {
    "source_id",
    "library_root_id",
    "relative_path",
    "observed_size",
    "observed_mtime_ns",
    "observed_ctime_ns",
    "source_device",
    "source_inode",
    "file_identity_sha256",
}
_PROFILE_FIELDS = {"profile_id", "profile_version", "profile_sha256"}
_ANALYSIS_BINDING_FIELDS = {"analysis_run_id", "revision", "content_sha256"}
_INTENDED_PUBLICATION_FIELDS = {"analysis_run_id", "revision"}
_IDEMPOTENCY_REQUEST_FIELDS = {"scope", "key"}
_STAGE_OUTCOME_FIELDS = {"stage", "outcome", "outcome_sha256", "reason_code"}
_ARTIFACT_DIGEST_FIELDS = {"stage", "name", "sha256"}
_PUBLISH_BINDING_FIELDS = {
    "analysis_run_id",
    "revision",
    "content_sha256",
    "change_position",
    "publish_receipt_sha256",
    "projection_receipt_sha256",
}
_ATTEMPT_AUTHORITY_FIELDS = {
    "object",
    "schema_version",
    "worker_kind",
    "job_id",
    "attempt",
    "runtime_generation",
    "database_binding",
    "request_sha256",
    "reason",
    "predecessor",
    "start_checkpoint_sha256",
}
_ATTEMPT_PREDECESSOR_FIELDS = {
    "attempt",
    "status",
    "stage",
    "checkpoint_sha256",
    "authority_sha256",
}


@dataclass
class ImageAnalysisJobContractError(ValueError):
    """Raised when an image-analysis job document is not closed and valid."""

    errors: tuple[dict[str, str], ...]

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        normalized = tuple(
            {
                "code": str(item.get("code") or "invalid_image_analysis_job_contract"),
                "path": str(item.get("path") or ""),
                "message": str(item.get("message") or "Image-analysis job contract is invalid."),
            }
            for item in errors
        )
        object.__setattr__(self, "errors", normalized)
        ValueError.__init__(
            self,
            normalized[0]["message"] if normalized else "Image-analysis job contract is invalid.",
        )


class _Issues:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) < IMAGE_ANALYSIS_MAX_ERRORS:
            self.items.append({"code": code, "path": path, "message": message})


def canonical_json(value: object) -> str:
    """Return deterministic UTF-8 JSON and reject non-finite numbers."""

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
    for field in sorted(fields - set(row)):
        issues.add("required_field_missing", f"{path}/{field}", "A required field is missing.")
    if set(row) - fields:
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
    if not unicodedata.is_normalized("NFC", value):
        issues.add(code, path, "String must use Unicode NFC normalization.")
        return False
    if any((ord(char) < 0x20) or ord(char) == 0x7F for char in value):
        issues.add(code, path, "String contains a forbidden control character.")
        return False
    if pattern is not None and pattern.fullmatch(value) is None:
        issues.add(code, path, "String does not satisfy its closed format.")
        return False
    return True


def _integer(
    value: object,
    path: str,
    issues: _Issues,
    *,
    minimum: int,
    maximum: int,
) -> bool:
    if type(value) is not int or not minimum <= value <= maximum:
        issues.add("invalid_integer", path, "Value must be a bounded integer.")
        return False
    return True


def _boolean(value: object, path: str, issues: _Issues) -> bool:
    if type(value) is not bool:
        issues.add("invalid_boolean", path, "Value must be a boolean.")
        return False
    return True


def _sha256(value: object, path: str, issues: _Issues) -> bool:
    return _text(
        value,
        path,
        issues,
        minimum=64,
        maximum=64,
        pattern=_SHA256,
        code="invalid_sha256",
    )


def _resource_id(
    value: object,
    path: str,
    pattern: re.Pattern[str],
    issues: _Issues,
) -> bool:
    return _text(value, path, issues, pattern=pattern, code="invalid_identifier")


def _token(value: object, path: str, issues: _Issues) -> bool:
    return _text(value, path, issues, pattern=_TOKEN, code="invalid_token")


def _validate_resource_bounds(
    value: object,
    *,
    maximum_bytes: int,
    issues: _Issues,
) -> None:
    nodes = 0
    active: set[int] = set()
    exhausted = False

    def visit(current: object, path: str, depth: int) -> None:
        nonlocal exhausted, nodes
        if exhausted:
            return
        nodes += 1
        if nodes > IMAGE_ANALYSIS_MAX_NODES:
            issues.add("resource_too_large", path, "Document exceeds the node limit.")
            exhausted = True
            return
        if depth > IMAGE_ANALYSIS_MAX_DEPTH:
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


def _relative_path(value: object, path: str, issues: _Issues) -> bool:
    if not _text(value, path, issues, maximum=4_096, code="invalid_relative_path"):
        return False
    assert isinstance(value, str)
    if value.startswith(("/", "~")) or "\\" in value or re.match(r"^[A-Za-z]:", value) is not None or "://" in value:
        issues.add("absolute_path_forbidden", path, "Only a normalized root-relative path is allowed.")
        return False
    relative = PurePosixPath(value)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        issues.add("invalid_relative_path", path, "Path must be normalized and root-relative.")
        return False
    if any(len(part.encode("utf-8")) > 255 for part in relative.parts):
        issues.add("invalid_relative_path", path, "A path component exceeds its byte limit.")
        return False
    if relative.as_posix() != value:
        issues.add("invalid_relative_path", path, "Path must use canonical POSIX separators.")
        return False
    return True


def _validate_database_binding(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_DATABASE_BINDING_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _text(
        row.get("database_uuid"),
        f"{path}/database_uuid",
        issues,
        minimum=36,
        maximum=36,
        pattern=_DATABASE_UUID,
        code="invalid_database_uuid",
    )
    valid = (
        _integer(
            row.get("database_device"),
            f"{path}/database_device",
            issues,
            minimum=0,
            maximum=_SQLITE_INT64_MAX,
        )
        and valid
    )
    valid = (
        _integer(
            row.get("database_inode"),
            f"{path}/database_inode",
            issues,
            minimum=1,
            maximum=_SQLITE_INT64_MAX,
        )
        and valid
    )
    return valid


def _validate_asset_binding(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_ASSET_BINDING_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _resource_id(row.get("asset_id"), f"{path}/asset_id", _ASSET_ID, issues)
    valid = _sha256(row.get("input_asset_sha256"), f"{path}/input_asset_sha256", issues) and valid
    if valid and row["asset_id"] != f"asset_{str(row['input_asset_sha256'])[:24]}":
        issues.add(
            "asset_digest_mismatch",
            f"{path}/asset_id",
            "Asset identity must be content-addressed from the input digest.",
        )
        valid = False
    return valid


def _validate_source_binding(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_SOURCE_BINDING_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _resource_id(row.get("source_id"), f"{path}/source_id", _SOURCE_ID, issues)
    valid = (
        _resource_id(
            row.get("library_root_id"),
            f"{path}/library_root_id",
            _ROOT_ID,
            issues,
        )
        and valid
    )
    valid = _relative_path(row.get("relative_path"), f"{path}/relative_path", issues) and valid
    for field, minimum, maximum in (
        ("observed_size", 1, _MAX_IMAGE_SOURCE_BYTES),
        ("observed_mtime_ns", 0, _SQLITE_INT64_MAX),
        ("observed_ctime_ns", 0, _SQLITE_INT64_MAX),
        ("source_device", 0, _SQLITE_INT64_MAX),
        ("source_inode", 1, _SQLITE_INT64_MAX),
    ):
        valid = (
            _integer(
                row.get(field),
                f"{path}/{field}",
                issues,
                minimum=minimum,
                maximum=maximum,
            )
            and valid
        )
    valid = _sha256(row.get("file_identity_sha256"), f"{path}/file_identity_sha256", issues) and valid
    return valid


def _validate_profile(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_PROFILE_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _token(row.get("profile_id"), f"{path}/profile_id", issues)
    valid = _token(row.get("profile_version"), f"{path}/profile_version", issues) and valid
    valid = _sha256(row.get("profile_sha256"), f"{path}/profile_sha256", issues) and valid
    return valid


def _validate_analysis_binding(
    value: object,
    path: str,
    issues: _Issues,
    *,
    nullable: bool = False,
) -> bool:
    if nullable and value is None:
        return True
    row = _closed(value, path=path, fields=_ANALYSIS_BINDING_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _resource_id(
        row.get("analysis_run_id"),
        f"{path}/analysis_run_id",
        _ANALYSIS_RUN_ID,
        issues,
    )
    valid = (
        _integer(
            row.get("revision"),
            f"{path}/revision",
            issues,
            minimum=1,
            maximum=1_800_000,
        )
        and valid
    )
    valid = _sha256(row.get("content_sha256"), f"{path}/content_sha256", issues) and valid
    return valid


def _validate_intended_publication(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_INTENDED_PUBLICATION_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _resource_id(
        row.get("analysis_run_id"),
        f"{path}/analysis_run_id",
        _ANALYSIS_RUN_ID,
        issues,
    )
    valid = (
        _integer(
            row.get("revision"),
            f"{path}/revision",
            issues,
            minimum=1,
            maximum=1_800_000,
        )
        and valid
    )
    return valid


def _validate_request_idempotency(value: object, path: str, issues: _Issues) -> bool:
    row = _closed(value, path=path, fields=_IDEMPOTENCY_REQUEST_FIELDS, issues=issues)
    if row is None:
        return False
    valid = _text(
        row.get("scope"),
        f"{path}/scope",
        issues,
        maximum=256,
        pattern=_IDEMPOTENCY_KEY,
        code="invalid_idempotency_scope",
    )
    valid = (
        _text(
            row.get("key"),
            f"{path}/key",
            issues,
            maximum=256,
            pattern=_IDEMPOTENCY_KEY,
            code="invalid_idempotency_key",
        )
        and valid
    )
    return valid


def require_valid_image_analysis_worker_kind(value: object) -> str:
    """Require the single closed worker kind; image-to-video substitution fails."""

    if value != IMAGE_ANALYSIS_WORKER_KIND or type(value) is not str:
        raise ImageAnalysisJobContractError(
            [
                {
                    "code": "unsupported_worker_kind",
                    "path": "/worker_kind",
                    "message": "Worker kind must be image_analysis.",
                }
            ]
        )
    return IMAGE_ANALYSIS_WORKER_KIND


def _validate_request(value: object, issues: _Issues) -> dict[str, Any] | None:
    fields = {
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
    }
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is None:
        return None
    if row.get("object") != IMAGE_ANALYSIS_REQUEST_OBJECT:
        issues.add("invalid_object", "/object", "Request object discriminator is invalid.")
    if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Request schema version is unsupported.")
    if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
        issues.add("unsupported_worker_kind", "/worker_kind", "Worker kind must be image_analysis.")
    _validate_database_binding(row.get("database_binding"), "/database_binding", issues)
    _validate_asset_binding(row.get("asset_binding"), "/asset_binding", issues)
    _validate_source_binding(row.get("source_binding"), "/source_binding", issues)
    _validate_profile(row.get("analysis_profile"), "/analysis_profile", issues)
    _validate_analysis_binding(row.get("expected_head"), "/expected_head", issues, nullable=True)
    _validate_intended_publication(row.get("intended_publication"), "/intended_publication", issues)
    _validate_request_idempotency(row.get("idempotency"), "/idempotency", issues)
    expected_head = row.get("expected_head")
    intended = row.get("intended_publication")
    if type(intended) is dict and type(intended.get("revision")) is int:
        expected_revision = 1
        if type(expected_head) is dict and type(expected_head.get("revision")) is int:
            expected_revision = int(expected_head["revision"]) + 1
        if intended["revision"] != expected_revision:
            issues.add(
                "nonlinear_intended_revision",
                "/intended_publication/revision",
                "Intended publication must advance the exact expected head by one.",
            )
    return row


def require_valid_image_analysis_request(value: object) -> dict[str, Any]:
    """Validate and copy one path-safe, authority-bound enqueue request."""

    issues = _Issues()
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_REQUEST_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    row = _validate_request(value, issues)
    if issues.items or row is None:
        raise ImageAnalysisJobContractError(issues.items)
    return deepcopy(row)


def image_analysis_request_sha256(value: object) -> str:
    """Return the digest used to bind an idempotency key to one exact request."""

    return canonical_sha256(require_valid_image_analysis_request(value))


def _validate_attempt_predecessor(
    value: object,
    *,
    attempt: object,
    issues: _Issues,
) -> None:
    if value is None:
        if type(attempt) is int and attempt != 1:
            issues.add(
                "attempt_predecessor_required",
                "/predecessor",
                "Every attempt after the first must bind its exact predecessor.",
            )
        return
    row = _closed(
        value,
        path="/predecessor",
        fields=_ATTEMPT_PREDECESSOR_FIELDS,
        issues=issues,
    )
    if row is None:
        return
    predecessor_attempt = row.get("attempt")
    _integer(
        predecessor_attempt,
        "/predecessor/attempt",
        issues,
        minimum=1,
        maximum=99,
    )
    if type(attempt) is int:
        if attempt == 1:
            issues.add(
                "attempt_predecessor_forbidden",
                "/predecessor",
                "The first attempt cannot have a predecessor.",
            )
        elif predecessor_attempt != attempt - 1:
            issues.add(
                "attempt_predecessor_mismatch",
                "/predecessor/attempt",
                "The predecessor must be the immediately prior attempt.",
            )
    status = row.get("status")
    if type(status) is not str or status not in IMAGE_ANALYSIS_JOB_STATUSES:
        issues.add(
            "invalid_predecessor_status",
            "/predecessor/status",
            "Predecessor status is outside the closed job registry.",
        )
    stage = row.get("stage")
    if type(stage) is not str or stage not in IMAGE_ANALYSIS_STAGES:
        issues.add(
            "invalid_stage",
            "/predecessor/stage",
            "Predecessor stage is outside the closed stage registry.",
        )
    _sha256(
        row.get("checkpoint_sha256"),
        "/predecessor/checkpoint_sha256",
        issues,
    )
    _sha256(
        row.get("authority_sha256"),
        "/predecessor/authority_sha256",
        issues,
    )


def _validate_attempt_authority(
    value: object,
    *,
    sealed: bool,
) -> list[dict[str, str]]:
    issues = _Issues()
    fields = set(_ATTEMPT_AUTHORITY_FIELDS)
    if sealed:
        fields.add("authority_sha256")
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is not None:
        if row.get("object") != IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_OBJECT:
            issues.add(
                "invalid_object",
                "/object",
                "Attempt authority object discriminator is invalid.",
            )
        if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
            issues.add(
                "unsupported_schema_version",
                "/schema_version",
                "Attempt authority schema version is unsupported.",
            )
        if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
            issues.add(
                "unsupported_worker_kind",
                "/worker_kind",
                "Worker kind must be image_analysis.",
            )
        _resource_id(row.get("job_id"), "/job_id", _JOB_ID, issues)
        attempt = row.get("attempt")
        _integer(
            attempt,
            "/attempt",
            issues,
            minimum=1,
            maximum=IMAGE_ANALYSIS_MAX_ATTEMPTS,
        )
        _text(
            row.get("runtime_generation"),
            "/runtime_generation",
            issues,
            minimum=83,
            maximum=83,
            pattern=_RUNTIME_GENERATION,
            code="invalid_runtime_generation",
        )
        _validate_database_binding(
            row.get("database_binding"),
            "/database_binding",
            issues,
        )
        _sha256(row.get("request_sha256"), "/request_sha256", issues)
        reason = row.get("reason")
        if type(reason) is not str or reason not in IMAGE_ANALYSIS_ATTEMPT_REASONS:
            issues.add(
                "invalid_attempt_reason",
                "/reason",
                "Attempt reason is outside the closed registry.",
            )
        elif type(attempt) is int and ((attempt == 1 and reason != "initial") or (attempt > 1 and reason == "initial")):
            issues.add(
                "attempt_reason_mismatch",
                "/reason",
                "Only attempt one may use the initial reason.",
            )
        _validate_attempt_predecessor(
            row.get("predecessor"),
            attempt=attempt,
            issues=issues,
        )
        _sha256(
            row.get("start_checkpoint_sha256"),
            "/start_checkpoint_sha256",
            issues,
        )
        if sealed and _sha256(
            row.get("authority_sha256"),
            "/authority_sha256",
            issues,
        ):
            try:
                normalized = deepcopy(row)
                normalized.pop("authority_sha256", None)
                expected_digest = canonical_sha256(normalized)
            except (TypeError, ValueError, UnicodeError, RecursionError):
                issues.add(
                    "invalid_json",
                    "",
                    "Attempt authority cannot be represented as canonical JSON.",
                )
            else:
                if row.get("authority_sha256") != expected_digest:
                    issues.add(
                        "digest_mismatch",
                        "/authority_sha256",
                        "Stored authority digest does not match canonical content.",
                    )
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    return issues.items


def validate_image_analysis_attempt_authority(
    value: object,
) -> list[dict[str, str]]:
    """Return every closed-contract error for one sealed attempt authority."""

    return _validate_attempt_authority(value, sealed=True)


def require_valid_image_analysis_attempt_authority(
    value: object,
) -> dict[str, Any]:
    """Validate and detach one self-authenticating attempt authority."""

    errors = validate_image_analysis_attempt_authority(value)
    if errors or type(value) is not dict:
        raise ImageAnalysisJobContractError(errors)
    return deepcopy(value)


def image_analysis_attempt_authority_sha256(
    value: Mapping[str, object],
) -> str:
    """Digest an authority with only its recursive self-digest removed."""

    normalized = deepcopy(dict(value))
    normalized.pop("authority_sha256", None)
    errors = _validate_attempt_authority(normalized, sealed=False)
    if errors:
        raise ImageAnalysisJobContractError(errors)
    return canonical_sha256(normalized)


def seal_image_analysis_attempt_authority(
    value: Mapping[str, object],
) -> dict[str, object]:
    """Validate, detach and seal one append-only attempt authority."""

    normalized = deepcopy(dict(value))
    normalized.pop("authority_sha256", None)
    errors = _validate_attempt_authority(normalized, sealed=False)
    if errors:
        raise ImageAnalysisJobContractError(errors)
    normalized["authority_sha256"] = canonical_sha256(normalized)
    sealed_errors = validate_image_analysis_attempt_authority(normalized)
    if sealed_errors:
        raise ImageAnalysisJobContractError(sealed_errors)
    return normalized


def canonical_image_analysis_attempt_authority_json(value: object) -> str:
    """Return canonical JSON for one verified sealed attempt authority."""

    return canonical_json(require_valid_image_analysis_attempt_authority(value))


def _validate_stage_outcomes(
    value: object,
    *,
    completed_stages: list[str],
    issues: _Issues,
) -> None:
    if type(value) is not list:
        issues.add("schema_type", "/stage_outcomes", "Stage outcomes must be an array.")
        return
    if len(value) != len(completed_stages):
        issues.add(
            "checkpoint_stage_mismatch",
            "/stage_outcomes",
            "There must be exactly one outcome for every completed stage.",
        )
    observed: list[str] = []
    for index, raw in enumerate(value[: len(IMAGE_ANALYSIS_WORK_STAGES)]):
        path = f"/stage_outcomes/{index}"
        row = _closed(raw, path=path, fields=_STAGE_OUTCOME_FIELDS, issues=issues)
        if row is None:
            continue
        stage = row.get("stage")
        if type(stage) is not str or stage not in IMAGE_ANALYSIS_WORK_STAGES:
            issues.add("invalid_stage", f"{path}/stage", "Stage is outside the closed registry.")
        else:
            observed.append(stage)
        if type(row.get("outcome")) is not str or row.get("outcome") not in IMAGE_ANALYSIS_STAGE_OUTCOMES:
            issues.add("invalid_stage_outcome", f"{path}/outcome", "Stage outcome is outside the closed registry.")
        _sha256(row.get("outcome_sha256"), f"{path}/outcome_sha256", issues)
        reason = row.get("reason_code")
        if reason is not None:
            _text(
                reason,
                f"{path}/reason_code",
                issues,
                maximum=120,
                pattern=_REASON_CODE,
                code="invalid_reason_code",
            )
        if row.get("outcome") in {"unsupported", "disabled", "partial"} and reason is None:
            issues.add(
                "reason_code_required",
                f"{path}/reason_code",
                "Non-complete stage outcomes require a stable reason code.",
            )
    if observed != completed_stages:
        issues.add(
            "checkpoint_stage_mismatch",
            "/stage_outcomes",
            "Outcome stages must exactly match the ordered completed-stage prefix.",
        )


def _validate_artifact_digests(
    value: object,
    *,
    completed_stages: list[str],
    issues: _Issues,
) -> None:
    if type(value) is not list:
        issues.add("schema_type", "/artifact_digests", "Artifact digests must be an array.")
        return
    maximum = len(IMAGE_ANALYSIS_WORK_STAGES) * IMAGE_ANALYSIS_MAX_ARTIFACTS_PER_STAGE
    if len(value) > maximum:
        issues.add("artifact_limit_exceeded", "/artifact_digests", "Checkpoint has too many artifact digests.")
    identities: list[tuple[str, str]] = []
    for index, raw in enumerate(value[:maximum]):
        path = f"/artifact_digests/{index}"
        row = _closed(raw, path=path, fields=_ARTIFACT_DIGEST_FIELDS, issues=issues)
        if row is None:
            continue
        stage = row.get("stage")
        name = row.get("name")
        if type(stage) is not str or stage not in completed_stages:
            issues.add(
                "artifact_stage_not_completed",
                f"{path}/stage",
                "Artifact digest may only reference a completed stage.",
            )
        _token(name, f"{path}/name", issues)
        _sha256(row.get("sha256"), f"{path}/sha256", issues)
        if type(stage) is str and type(name) is str:
            identities.append((stage, name))
    if identities != sorted(identities) or len(identities) != len(set(identities)):
        issues.add(
            "artifact_order_invalid",
            "/artifact_digests",
            "Artifact digests must be unique and sorted by stage and name.",
        )


def _validate_publish_binding(
    value: object,
    *,
    completed_stages: list[str],
    intended_publication: Mapping[str, object] | None,
    issues: _Issues,
) -> None:
    if value is None:
        if "publish" in completed_stages:
            issues.add(
                "publish_binding_required",
                "/publish_binding",
                "A completed publish stage requires its exact publication binding.",
            )
        return
    row = _closed(value, path="/publish_binding", fields=_PUBLISH_BINDING_FIELDS, issues=issues)
    if row is None:
        return
    valid = _resource_id(
        row.get("analysis_run_id"),
        "/publish_binding/analysis_run_id",
        _ANALYSIS_RUN_ID,
        issues,
    )
    valid = (
        _integer(
            row.get("revision"),
            "/publish_binding/revision",
            issues,
            minimum=1,
            maximum=1_800_000,
        )
        and valid
    )
    valid = _sha256(row.get("content_sha256"), "/publish_binding/content_sha256", issues) and valid
    valid = (
        _integer(
            row.get("change_position"),
            "/publish_binding/change_position",
            issues,
            minimum=1,
            maximum=9_007_199_254_740_991,
        )
        and valid
    )
    valid = (
        _sha256(
            row.get("publish_receipt_sha256"),
            "/publish_binding/publish_receipt_sha256",
            issues,
        )
        and valid
    )
    projection_receipt = row.get("projection_receipt_sha256")
    if projection_receipt is not None:
        valid = (
            _sha256(
                projection_receipt,
                "/publish_binding/projection_receipt_sha256",
                issues,
            )
            and valid
        )
    if "publish" not in completed_stages:
        issues.add(
            "premature_publish_binding",
            "/publish_binding",
            "Publication binding is forbidden before publish completes.",
        )
    if "shadow_projection" in completed_stages and projection_receipt is None:
        issues.add(
            "projection_binding_required",
            "/publish_binding/projection_receipt_sha256",
            "A completed shadow projection requires its receipt digest.",
        )
    if "shadow_projection" not in completed_stages and projection_receipt is not None:
        issues.add(
            "premature_projection_binding",
            "/publish_binding/projection_receipt_sha256",
            "Projection receipt is forbidden before shadow projection completes.",
        )
    if (
        valid
        and intended_publication is not None
        and (
            row["analysis_run_id"] != intended_publication.get("analysis_run_id")
            or row["revision"] != intended_publication.get("revision")
        )
    ):
        issues.add(
            "publication_binding_mismatch",
            "/publish_binding",
            "Published analysis identity must equal the request's intended publication.",
        )


def _validate_checkpoint(
    value: object,
    issues: _Issues,
    *,
    intended_publication: Mapping[str, object] | None = None,
) -> dict[str, Any] | None:
    fields = {
        "object",
        "schema_version",
        "worker_kind",
        "job_id",
        "attempt",
        "current_stage",
        "completed_stages",
        "stage_outcomes",
        "artifact_digests",
        "publish_binding",
    }
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is None:
        return None
    if row.get("object") != IMAGE_ANALYSIS_CHECKPOINT_OBJECT:
        issues.add("invalid_object", "/object", "Checkpoint object discriminator is invalid.")
    if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Checkpoint schema version is unsupported.")
    if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
        issues.add("unsupported_worker_kind", "/worker_kind", "Worker kind must be image_analysis.")
    _resource_id(row.get("job_id"), "/job_id", _JOB_ID, issues)
    _integer(
        row.get("attempt"),
        "/attempt",
        issues,
        minimum=1,
        maximum=IMAGE_ANALYSIS_MAX_ATTEMPTS,
    )
    current_stage = row.get("current_stage")
    if type(current_stage) is not str or current_stage not in IMAGE_ANALYSIS_STAGES:
        issues.add("invalid_stage", "/current_stage", "Stage is outside the closed registry.")
    completed = row.get("completed_stages")
    completed_stages: list[str] = []
    if type(completed) is not list or any(type(item) is not str for item in completed):
        issues.add("schema_type", "/completed_stages", "Completed stages must be a string array.")
    else:
        completed_stages = list(completed)
        expected = list(IMAGE_ANALYSIS_WORK_STAGES[: len(completed_stages)])
        if completed_stages != expected:
            issues.add(
                "checkpoint_stage_order_invalid",
                "/completed_stages",
                "Completed stages must be one exact prefix of the closed registry.",
            )
        if len(completed_stages) >= len(IMAGE_ANALYSIS_WORK_STAGES):
            expected_current = "completed"
        else:
            expected_current = IMAGE_ANALYSIS_WORK_STAGES[len(completed_stages)]
        if not completed_stages and current_stage == "queued":
            expected_current = "queued"
        if current_stage != expected_current:
            issues.add(
                "checkpoint_current_stage_mismatch",
                "/current_stage",
                "Current stage must be the next stage after the completed prefix.",
            )
    _validate_stage_outcomes(
        row.get("stage_outcomes"),
        completed_stages=completed_stages,
        issues=issues,
    )
    _validate_artifact_digests(
        row.get("artifact_digests"),
        completed_stages=completed_stages,
        issues=issues,
    )
    _validate_publish_binding(
        row.get("publish_binding"),
        completed_stages=completed_stages,
        intended_publication=intended_publication,
        issues=issues,
    )
    return row


def require_valid_image_analysis_checkpoint(value: object) -> dict[str, Any]:
    """Validate a bounded, monotonic recovery checkpoint."""

    issues = _Issues()
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_CHECKPOINT_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    row = _validate_checkpoint(value, issues)
    if issues.items or row is None:
        raise ImageAnalysisJobContractError(issues.items)
    return deepcopy(row)


def _validate_error(value: object, issues: _Issues) -> dict[str, Any] | None:
    fields = {
        "object",
        "schema_version",
        "worker_kind",
        "code",
        "stage",
        "retryable",
        "detail_sha256",
    }
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is None:
        return None
    if row.get("object") != IMAGE_ANALYSIS_ERROR_OBJECT:
        issues.add("invalid_object", "/object", "Error object discriminator is invalid.")
    if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Error schema version is unsupported.")
    if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
        issues.add("unsupported_worker_kind", "/worker_kind", "Worker kind must be image_analysis.")
    if type(row.get("code")) is not str or row.get("code") not in IMAGE_ANALYSIS_ERROR_CODES:
        issues.add("invalid_error_code", "/code", "Error code is outside the stable registry.")
    if type(row.get("stage")) is not str or row.get("stage") not in IMAGE_ANALYSIS_STAGES:
        issues.add("invalid_stage", "/stage", "Error stage is outside the closed registry.")
    _boolean(row.get("retryable"), "/retryable", issues)
    detail_sha256 = row.get("detail_sha256")
    if detail_sha256 is not None:
        _sha256(detail_sha256, "/detail_sha256", issues)
    nonretryable = {
        "cancelled",
        "database_scope_changed",
        "head_conflict",
        "idempotency_conflict",
        "invalid_request",
        "profile_changed",
        "publication_conflict",
        "source_changed",
    }
    if row.get("code") in nonretryable and row.get("retryable") is not False:
        issues.add("invalid_retry_policy", "/retryable", "This stable error is never retryable.")
    return row


def require_valid_image_analysis_error(value: object) -> dict[str, Any]:
    """Validate a stable, payload-free worker error outcome."""

    issues = _Issues()
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_ERROR_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    row = _validate_error(value, issues)
    if issues.items or row is None:
        raise ImageAnalysisJobContractError(issues.items)
    return deepcopy(row)


def _validate_idempotency_binding(
    value: object,
    issues: _Issues,
) -> dict[str, Any] | None:
    fields = {
        "object",
        "schema_version",
        "worker_kind",
        "scope",
        "key",
        "request_sha256",
        "job_id",
    }
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is None:
        return None
    if row.get("object") != IMAGE_ANALYSIS_IDEMPOTENCY_OBJECT:
        issues.add("invalid_object", "/object", "Idempotency object discriminator is invalid.")
    if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Idempotency schema version is unsupported.")
    if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
        issues.add("unsupported_worker_kind", "/worker_kind", "Worker kind must be image_analysis.")
    _text(
        row.get("scope"),
        "/scope",
        issues,
        maximum=256,
        pattern=_IDEMPOTENCY_KEY,
        code="invalid_idempotency_scope",
    )
    _text(
        row.get("key"),
        "/key",
        issues,
        maximum=256,
        pattern=_IDEMPOTENCY_KEY,
        code="invalid_idempotency_key",
    )
    _sha256(row.get("request_sha256"), "/request_sha256", issues)
    _resource_id(row.get("job_id"), "/job_id", _JOB_ID, issues)
    return row


def require_valid_image_analysis_idempotency_binding(value: object) -> dict[str, Any]:
    """Validate the immutable key -> request digest -> job binding."""

    issues = _Issues()
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_IDEMPOTENCY_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    row = _validate_idempotency_binding(value, issues)
    if issues.items or row is None:
        raise ImageAnalysisJobContractError(issues.items)
    return deepcopy(row)


def require_valid_image_analysis_job(value: object) -> dict[str, Any]:
    """Validate the complete durable job snapshot and all cross-bindings."""

    issues = _Issues()
    _validate_resource_bounds(
        value,
        maximum_bytes=IMAGE_ANALYSIS_JOB_MAX_CANONICAL_BYTES,
        issues=issues,
    )
    fields = {
        "object",
        "schema_version",
        "worker_kind",
        "job_id",
        "request",
        "request_sha256",
        "idempotency_binding",
        "status",
        "attempt",
        "current_stage",
        "checkpoint",
        "heartbeat_sequence",
        "cancel_requested",
        "error",
    }
    row = _closed(value, path="", fields=fields, issues=issues)
    if row is None:
        raise ImageAnalysisJobContractError(issues.items)
    if row.get("object") != IMAGE_ANALYSIS_JOB_OBJECT:
        issues.add("invalid_object", "/object", "Job object discriminator is invalid.")
    if row.get("schema_version") != IMAGE_ANALYSIS_JOB_SCHEMA_VERSION:
        issues.add("unsupported_schema_version", "/schema_version", "Job schema version is unsupported.")
    if row.get("worker_kind") != IMAGE_ANALYSIS_WORKER_KIND or type(row.get("worker_kind")) is not str:
        issues.add("unsupported_worker_kind", "/worker_kind", "Worker kind must be image_analysis.")
    _resource_id(row.get("job_id"), "/job_id", _JOB_ID, issues)

    request = _validate_request(row.get("request"), issues)
    request_sha = row.get("request_sha256")
    _sha256(request_sha, "/request_sha256", issues)
    if request is not None:
        try:
            expected_request_sha = canonical_sha256(request)
        except (TypeError, ValueError, UnicodeError, RecursionError):
            expected_request_sha = None
        if expected_request_sha is not None and request_sha != expected_request_sha:
            issues.add(
                "request_digest_mismatch",
                "/request_sha256",
                "Job request digest does not bind the embedded request.",
            )

    idempotency = _validate_idempotency_binding(row.get("idempotency_binding"), issues)
    if idempotency is not None and request is not None:
        request_idempotency = request.get("idempotency")
        if type(request_idempotency) is dict and (
            idempotency.get("scope") != request_idempotency.get("scope")
            or idempotency.get("key") != request_idempotency.get("key")
            or idempotency.get("request_sha256") != request_sha
            or idempotency.get("job_id") != row.get("job_id")
        ):
            issues.add(
                "idempotency_binding_mismatch",
                "/idempotency_binding",
                "Idempotency binding must match the exact request digest and job.",
            )

    status = row.get("status")
    if type(status) is not str or status not in IMAGE_ANALYSIS_JOB_STATUSES:
        issues.add("invalid_job_status", "/status", "Job status is outside the closed registry.")
    _integer(
        row.get("attempt"),
        "/attempt",
        issues,
        minimum=1,
        maximum=IMAGE_ANALYSIS_MAX_ATTEMPTS,
    )
    _integer(
        row.get("heartbeat_sequence"),
        "/heartbeat_sequence",
        issues,
        minimum=0,
        maximum=9_007_199_254_740_991,
    )
    _boolean(row.get("cancel_requested"), "/cancel_requested", issues)
    current_stage = row.get("current_stage")
    if type(current_stage) is not str or current_stage not in IMAGE_ANALYSIS_STAGES:
        issues.add("invalid_stage", "/current_stage", "Job stage is outside the closed registry.")

    intended = request.get("intended_publication") if request is not None else None
    checkpoint = _validate_checkpoint(
        row.get("checkpoint"),
        issues,
        intended_publication=intended if isinstance(intended, Mapping) else None,
    )
    if checkpoint is not None and (
        checkpoint.get("job_id") != row.get("job_id")
        or checkpoint.get("attempt") != row.get("attempt")
        or checkpoint.get("current_stage") != current_stage
    ):
        issues.add(
            "job_checkpoint_mismatch",
            "/checkpoint",
            "Checkpoint identity, attempt and current stage must match the job.",
        )

    error = row.get("error")
    if error is None:
        if status in {"failed", "cancelled", "interrupted"}:
            issues.add("job_error_required", "/error", "This job status requires a stable error outcome.")
    else:
        validated_error = _validate_error(error, issues)
        if status not in {"failed", "cancelled", "interrupted"}:
            issues.add("unexpected_job_error", "/error", "This job status cannot carry an error outcome.")
        if validated_error is not None and validated_error.get("stage") != current_stage:
            issues.add("job_error_stage_mismatch", "/error/stage", "Error stage must match the job stage.")
        if status == "cancelled" and validated_error is not None and validated_error.get("code") != "cancelled":
            issues.add("cancel_error_mismatch", "/error/code", "Cancelled jobs require the cancelled error code.")
        if status == "interrupted" and validated_error is not None and validated_error.get("retryable") is not True:
            issues.add("interrupted_retry_mismatch", "/error/retryable", "Interrupted jobs must be retryable.")

    if status == "queued" and current_stage != "queued":
        issues.add("queued_state_mismatch", "/status", "Queued jobs must be at queued stage.")
    if status == "running" and current_stage in {"queued", "completed"}:
        issues.add("running_state_mismatch", "/status", "Running jobs need an active stage.")
    if status == "cancelling" and row.get("cancel_requested") is not True:
        issues.add("cancel_state_mismatch", "/cancel_requested", "Cancelling jobs require a cancel request.")
    if status == "cancelled" and row.get("cancel_requested") is not True:
        issues.add("cancel_state_mismatch", "/cancel_requested", "Cancelled jobs require a cancel request.")
    if status == "succeeded" and current_stage != "completed":
        issues.add("success_state_mismatch", "/current_stage", "Succeeded jobs must complete every stage.")
    if current_stage == "completed" and status != "succeeded":
        issues.add("completed_state_mismatch", "/status", "Only a succeeded job may use completed stage.")

    if issues.items:
        raise ImageAnalysisJobContractError(issues.items)
    return deepcopy(row)


def next_image_analysis_stage(current_stage: object) -> str:
    """Return the next closed stage; terminal ``completed`` has no successor."""

    if type(current_stage) is not str or current_stage not in IMAGE_ANALYSIS_STAGES:
        raise ImageAnalysisJobContractError(
            [{"code": "invalid_stage", "path": "/current_stage", "message": "Stage is outside the closed registry."}]
        )
    index = IMAGE_ANALYSIS_STAGES.index(current_stage)
    if index == len(IMAGE_ANALYSIS_STAGES) - 1:
        raise ImageAnalysisJobContractError(
            [
                {
                    "code": "terminal_stage",
                    "path": "/current_stage",
                    "message": "Completed is terminal and has no successor.",
                }
            ]
        )
    return IMAGE_ANALYSIS_STAGES[index + 1]
