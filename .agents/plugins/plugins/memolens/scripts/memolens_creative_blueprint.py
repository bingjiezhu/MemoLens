"""Creative Blueprint candidate v1 schema, validation, and legacy projection.

The candidate is deliberately not a persisted Blueprint revision.  It is a
closed, Agent-neutral proposal that can be validated before a future Core
command is allowed to write project state.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from ipaddress import ip_address
import json
import re
from pathlib import Path
from typing import Any, Iterable
import unicodedata
from urllib.parse import urlsplit

from memolens_strict_json import (
    StrictJsonError,
    StrictJsonLimits,
    decode_strict_json,
    validate_json_value,
)
from memolens_text_safety import contains_private_locator
from memolens_wiki import asset_evidence_id, parse_evidence_id, span_evidence_id


BLUEPRINT_SCHEMA_VERSION = "1"
BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256 = (
    "b1c0a53b5d5e4313af11297ca61d16553597020d35febd091576a62c6dc719e9"
)
BLUEPRINT_CANDIDATE_OBJECT = "memolens.creative_blueprint_candidate"
BLUEPRINT_VALIDATION_OBJECT = "memolens.creative_blueprint_validation"
BLUEPRINT_SHADOW_OBJECT = "memolens.creative_blueprint_shadow"
BLUEPRINT_MAX_ERRORS = 64
BLUEPRINT_JSON_LIMITS = StrictJsonLimits(
    max_bytes=524_288,
    max_depth=16,
    max_nodes=20_000,
    max_object_items=64,
    max_array_items=512,
    max_string_chars=12_000,
)
BLUEPRINT_TRANSPORT_JSON_LIMITS = StrictJsonLimits(
    max_bytes=1_048_576,
    max_depth=32,
    max_nodes=50_000,
    max_object_items=2_000,
    max_array_items=10_000,
    max_string_chars=262_144,
)
PERSISTED_BLUEPRINT_SOURCE_LIMITS = StrictJsonLimits(
    max_bytes=1_048_576,
    max_depth=32,
    max_nodes=50_000,
    max_object_items=2_000,
    max_array_items=10_000,
    max_string_chars=262_144,
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "schemas"
    / "creative-blueprint-candidate-v1.schema.json"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RESIDUAL_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<residual_id>rseg_[0-9a-f]{64})$"
)
_FILE_NAME_SHAPE = re.compile(
    r"^(?:\.{1,2})?[A-Za-z0-9_-][A-Za-z0-9_. -]*\.[A-Za-z0-9]{1,16}$"
)
_KNOWN_FILE_SUFFIXES = {
    "aac",
    "ass",
    "avi",
    "bmp",
    "doc",
    "docx",
    "drp",
    "fcpxml",
    "flac",
    "gif",
    "heic",
    "heif",
    "jpeg",
    "jpg",
    "json",
    "m4a",
    "m4v",
    "md",
    "mkv",
    "mov",
    "mp3",
    "mp4",
    "pdf",
    "png",
    "prproj",
    "srt",
    "tif",
    "tiff",
    "txt",
    "vtt",
    "wav",
    "webm",
    "webp",
    "yaml",
    "yml",
}
_DECLARED_SOURCES = {
    "caller_declared_user_input",
    "agent_proposal",
    "legacy_unknown",
    "unknown",
}


def _load_schema() -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw = _SCHEMA_PATH.read_bytes()
        schema = decode_strict_json(
            raw,
            limits=StrictJsonLimits(
                max_bytes=262_144,
                max_depth=32,
                max_nodes=50_000,
                max_object_items=2_000,
                max_array_items=1_000,
                max_string_chars=20_000,
            ),
        )
    except (OSError, StrictJsonError):
        # Blueprint is an additive capability. A damaged optional artifact must
        # fail this capability closed without taking A0/Wiki/Timeline offline.
        return None, None
    if not isinstance(schema, dict):  # strict decoder already enforces this
        return None, None
    if hashlib.sha256(raw).hexdigest() != BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256:
        return None, None
    return schema, raw


BLUEPRINT_CANDIDATE_SCHEMA, _BLUEPRINT_SCHEMA_BYTES = _load_schema()
BLUEPRINT_SCHEMA_AVAILABLE = bool(
    BLUEPRINT_CANDIDATE_SCHEMA is not None and _BLUEPRINT_SCHEMA_BYTES is not None
)
BLUEPRINT_SCHEMA_SHA256 = (
    hashlib.sha256(_BLUEPRINT_SCHEMA_BYTES).hexdigest()
    if _BLUEPRINT_SCHEMA_BYTES is not None
    else None
)


def blueprint_contract_summary() -> dict[str, Any]:
    """Return the static discoverability contract advertised by status."""

    return {
        "object": BLUEPRINT_CANDIDATE_OBJECT,
        "schema_version": BLUEPRINT_SCHEMA_VERSION,
        "schema_available": BLUEPRINT_SCHEMA_AVAILABLE,
        "schema_sha256": BLUEPRINT_SCHEMA_SHA256,
        "schema_artifact": "creative-blueprint-candidate-v1.schema.json",
        "candidate_persisted": False,
        "authority_verified": False,
    }


def blueprint_safety_summary() -> dict[str, Any]:
    return {
        "read_only": True,
        "candidate_ephemeral": True,
        "private_ephemeral_snapshot_possible": True,
        "persistent_state_written": False,
        "blueprint_persisted": False,
        "project_modified": False,
        "photos_opened_by_plugin": False,
        "photos_modified": False,
        "media_modified": False,
        "timeline_persisted": False,
        "rendered": False,
        "exported": False,
        "remote_network_allowed": False,
    }


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BlueprintError:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class _Errors:
    def __init__(self) -> None:
        self.items: list[BlueprintError] = []
        self.truncated = False

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) >= BLUEPRINT_MAX_ERRORS:
            self.truncated = True
            return
        self.items.append(BlueprintError(code, path, message))

    def extend_strict(self, error: StrictJsonError) -> None:
        path = "" if error.path == "$" else error.path.removeprefix("$")
        self.add(error.code, path, error.message)

    def result(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in self.items]


def _pointer(path: str, token: str | int) -> str:
    if isinstance(token, int):
        return f"{path}/{token}"
    escaped = token.replace("~", "~0").replace("/", "~1")
    return f"{path}/{escaped}"


def _exact_equal(left: Any, right: Any) -> bool:
    return type(left) is type(right) and left == right


def _matches_type(value: Any, expected: str) -> bool:
    value_type = type(value)
    if expected == "null":
        return value is None
    if expected == "object":
        return value_type is dict
    if expected == "array":
        return value_type is list
    if expected == "string":
        return value_type is str
    if expected == "integer":
        return value_type is int
    if expected == "number":
        return value_type in {int, float}
    if expected == "boolean":
        return value_type is bool
    return False


def _schema_type_matches(value: Any, raw_type: Any) -> bool:
    if isinstance(raw_type, str):
        return _matches_type(value, raw_type)
    if isinstance(raw_type, list):
        return any(isinstance(item, str) and _matches_type(value, item) for item in raw_type)
    return True


def _contains_forbidden_control(value: str) -> bool:
    return any(
        (ord(character) < 0x20 and character not in {"\t", "\n", "\r"})
        or ord(character) == 0x7F
        for character in value
    )


def _has_visible_text(value: str) -> bool:
    return any(
        not character.isspace()
        and unicodedata.category(character)[0] not in {"C", "Z"}
        for character in value
    )


def _looks_like_complete_relative_path(value: str) -> bool:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        return False
    if "\\" in normalized or normalized.startswith(("./", "../")):
        return True
    if "/" not in normalized:
        suffix = normalized.rpartition(".")[2].casefold()
        return bool(
            suffix in _KNOWN_FILE_SUFFIXES
            and _FILE_NAME_SHAPE.fullmatch(normalized) is not None
        )
    segments = normalized.split("/")
    if any(not segment or segment != segment.strip() for segment in segments):
        return False
    return _FILE_NAME_SHAPE.fullmatch(segments[-1]) is not None


def _validate_schema_union(
    value: Any,
    schema: dict[str, Any],
    errors: _Errors,
    *,
    path: str,
) -> bool:
    for keyword, exact_one in (("anyOf", False), ("oneOf", True)):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            matches = 0
            for branch in branches:
                if not isinstance(branch, dict):
                    continue
                branch_errors = _Errors()
                _validate_schema(value, branch, branch_errors, path=path)
                if not branch_errors.items and not branch_errors.truncated:
                    matches += 1
            if matches == 0 or (exact_one and matches != 1):
                errors.add(
                    "schema_union",
                    path,
                    "Value does not match the required schema branch.",
                )
            return True
    return False


def _validate_schema(
    value: Any,
    schema: dict[str, Any],
    errors: _Errors,
    *,
    path: str = "",
) -> None:
    if _validate_schema_union(value, schema, errors, path=path):
        return
    raw_type = schema.get("type")
    if raw_type is not None and not _schema_type_matches(value, raw_type):
        errors.add("schema_type", path, "Value has the wrong JSON type.")
        return
    if "const" in schema and not _exact_equal(value, schema["const"]):
        errors.add("schema_const", path, "Value does not match the required constant.")
        return
    if "enum" in schema and not any(_exact_equal(value, item) for item in schema["enum"]):
        errors.add("schema_enum", path, "Value is outside the supported enum.")
        return

    if type(value) is str:
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.add("string_too_short", path, "String is shorter than allowed.")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.add("string_too_long", path, "String is longer than allowed.")
        if _contains_forbidden_control(value):
            errors.add(
                "forbidden_control_character",
                path,
                "String contains a forbidden control character.",
            )
        if isinstance(minimum, int) and minimum > 0 and not _has_visible_text(value):
            errors.add("blank_string", path, "String must contain visible text.")
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and re.fullmatch(pattern, value) is None:
            errors.add("string_pattern", path, "String does not match the required format.")
        return

    if type(value) in {int, float}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.add("number_too_small", path, "Number is below the allowed minimum.")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.add("number_too_large", path, "Number exceeds the allowed maximum.")
        return

    if type(value) is list:
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            errors.add("array_too_short", path, "Array has too few items.")
        if isinstance(maximum, int) and len(value) > maximum:
            errors.add("array_too_long", path, "Array has too many items.")
        if schema.get("uniqueItems") is True:
            seen: set[str] = set()
            for index, item in enumerate(value):
                try:
                    identity = canonical_json(item)
                except (TypeError, ValueError):
                    continue
                if identity in seen:
                    errors.add(
                        "duplicate_array_item",
                        _pointer(path, index),
                        "Array items must be unique.",
                    )
                seen.add(identity)
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema(item, item_schema, errors, path=_pointer(path, index))
        return

    if type(value) is dict:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if isinstance(key, str) and key not in value:
                    errors.add(
                        "required_field_missing",
                        _pointer(path, key),
                        "A required field is missing.",
                    )
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            errors.add(
                "unknown_field",
                _pointer(path, "*"),
                "Object contains an unsupported field.",
            )
        for key in sorted(set(value) & set(properties)):
            child_schema = properties[key]
            if isinstance(child_schema, dict):
                _validate_schema(
                    value[key], child_schema, errors, path=_pointer(path, key)
                )


def _collection_ids(
    items: Any,
    key: str,
    path: str,
    errors: _Errors,
) -> set[str]:
    result: set[str] = set()
    if not isinstance(items, list):
        return result
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        identifier = item.get(key)
        if not isinstance(identifier, str):
            continue
        if identifier in result:
            errors.add(
                "duplicate_identifier",
                _pointer(_pointer(path, index), key),
                "Identifiers must be unique within their collection.",
            )
        result.add(identifier)
    return result


def _validate_block_links(
    items: Any,
    path: str,
    block_ids: set[str],
    errors: _Errors,
) -> None:
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        links = item.get("script_block_ids")
        if not isinstance(links, list):
            continue
        for link_index, block_id in enumerate(links):
            if isinstance(block_id, str) and block_id not in block_ids:
                errors.add(
                    "dangling_script_block",
                    _pointer(
                        _pointer(_pointer(path, index), "script_block_ids"),
                        link_index,
                    ),
                    "Reference points to a script block that is not present.",
                )


def _valid_external_https(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or len(value) > 4000
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
        or re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", value) is None
        or re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port  # force urllib to validate syntax and the 0..65535 range
    except ValueError:
        return False
    if not (
        parsed.scheme == "https"
        and parsed.netloc
        and hostname
        and (port is None or 0 <= port <= 65_535)
        and parsed.username is None
        and parsed.password is None
    ):
        return False
    try:
        ip_address(hostname)
        return True
    except ValueError:
        domain = hostname[:-1] if hostname.endswith(".") else hostname
        if not domain or len(domain) > 253:
            return False
        return all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            is not None
            for label in domain.split(".")
        )


def valid_user_text_reference(value: Any) -> bool:
    """Accept inline reference text, never a path, URI, or hidden locator."""

    if not isinstance(value, str):
        return False
    normalized = value.strip()
    return bool(
        _has_visible_text(normalized)
        and not _contains_forbidden_control(normalized)
        and not contains_private_locator(normalized)
        and not _looks_like_complete_relative_path(normalized)
        and "://" not in normalized
        and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized) is None
    )


def _validate_reference_kinds(candidate: dict[str, Any], errors: _Errors) -> None:
    references = candidate.get("reference_refs")
    if not isinstance(references, list):
        return
    for index, reference in enumerate(references):
        if not isinstance(reference, dict):
            continue
        kind = reference.get("kind")
        locator = reference.get("locator")
        path = _pointer(_pointer("/reference_refs", index), "locator")
        if kind == "evidence":
            try:
                parse_evidence_id(locator)
            except Exception:  # only a stable validation error crosses the boundary
                errors.add(
                    "invalid_evidence_reference",
                    path,
                    "Evidence reference is not a supported MemoLens evidence URI.",
                )
            else:
                if (
                    isinstance(locator, str)
                    and _RESIDUAL_EVIDENCE_REF.fullmatch(locator) is not None
                ):
                    errors.add(
                        "invalid_evidence_reference",
                        path,
                        "Residual evidence is only valid in a proved material hint.",
                    )
        elif kind == "external_https":
            if not _valid_external_https(locator):
                errors.add(
                    "invalid_https_reference",
                    path,
                    "External reference must be a credential-free HTTPS URL.",
                )
        elif kind in {"project", "research_snapshot"}:
            if not isinstance(locator, str) or _IDENTIFIER.fullmatch(locator) is None:
                errors.add(
                    "invalid_opaque_reference",
                    path,
                    "Reference must use a supported opaque identifier.",
                )
        elif kind == "user_text" and not valid_user_text_reference(locator):
            errors.add(
                "invalid_user_text_reference",
                path,
                "Inline reference text must not contain a path or URI.",
            )


def _cross_validate(candidate: dict[str, Any], errors: _Errors) -> None:
    project_id = candidate.get("project_id")
    base = candidate.get("base")
    if project_id is None and base is not None:
        errors.add(
            "base_without_project",
            "/base",
            "A baseline cannot be supplied without a project identifier.",
        )
    if isinstance(base, dict):
        if base.get("legacy_brief") is None and base.get("observed_timeline") is not None:
            errors.add(
                "timeline_without_brief_base",
                "/base/observed_timeline",
                "A Timeline baseline requires a legacy brief baseline.",
            )

    script = candidate.get("script")
    blocks = script.get("blocks") if isinstance(script, dict) else []
    block_ids = _collection_ids(blocks, "block_id", "/script/blocks", errors)
    constraints = candidate.get("constraints")
    includes = constraints.get("must_include") if isinstance(constraints, dict) else []
    excludes = constraints.get("must_exclude") if isinstance(constraints, dict) else []
    include_ids = _collection_ids(
        includes, "constraint_id", "/constraints/must_include", errors
    )
    _collection_ids(
        excludes, "constraint_id", "/constraints/must_exclude", errors
    )
    if isinstance(excludes, list):
        for index, item in enumerate(excludes):
            if isinstance(item, dict) and item.get("constraint_id") in include_ids:
                errors.add(
                    "duplicate_identifier",
                    _pointer(
                        _pointer("/constraints/must_exclude", index),
                        "constraint_id",
                    ),
                    "Constraint identifiers must be unique across both groups.",
                )
    for path, items in (
        ("/constraints/must_include", includes),
        ("/constraints/must_exclude", excludes),
        ("/material_hints", candidate.get("material_hints")),
        ("/missing_evidence", candidate.get("missing_evidence")),
    ):
        _validate_block_links(items, path, block_ids, errors)

    for path, items in (
        ("/constraints/must_include", includes),
        ("/constraints/must_exclude", excludes),
    ):
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get("text") is None and item.get("evidence_ref") is None:
                errors.add(
                    "empty_constraint",
                    _pointer(path, index),
                    "Constraint needs text, evidence, or both.",
                )
            evidence = item.get("evidence_ref")
            if evidence is not None:
                try:
                    parse_evidence_id(evidence)
                except Exception:
                    errors.add(
                        "invalid_evidence_reference",
                        _pointer(_pointer(path, index), "evidence_ref"),
                        "Evidence reference is not a supported MemoLens evidence URI.",
                    )
                else:
                    if (
                        isinstance(evidence, str)
                        and _RESIDUAL_EVIDENCE_REF.fullmatch(evidence) is not None
                    ):
                        errors.add(
                            "invalid_evidence_reference",
                            _pointer(_pointer(path, index), "evidence_ref"),
                            "Residual evidence is only valid in a proved material hint.",
                        )

    include_evidence = {
        item.get("evidence_ref")
        for item in includes
        if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
    } if isinstance(includes, list) else set()
    exclude_evidence = {
        item.get("evidence_ref")
        for item in excludes
        if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str)
    } if isinstance(excludes, list) else set()
    if include_evidence & exclude_evidence:
        errors.add(
            "contradictory_evidence_constraint",
            "/constraints",
            "The same evidence cannot be both required and excluded.",
        )

    materials = candidate.get("material_hints")
    _collection_ids(materials, "hint_id", "/material_hints", errors)
    residual_proofs: dict[str, dict[str, object]] = {}
    if isinstance(materials, list):
        # Local import avoids a module-initialization cycle: residual identity
        # uses this module's canonical JSON digest implementation.
        from memolens_residual_authority import (
            ResidualAuthorityError,
            require_valid_residual_binding,
        )

        for index, item in enumerate(materials):
            if not isinstance(item, dict):
                continue
            item_path = _pointer("/material_hints", index)
            evidence_ref = item.get("evidence_ref")
            residual_match = (
                _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
                if isinstance(evidence_ref, str)
                else None
            )
            proof = item.get("proof")
            if residual_match is None:
                if proof is not None:
                    errors.add(
                        "residual_evidence_reference_mismatch",
                        _pointer(item_path, "evidence_ref"),
                        "Residual proof requires its exact residual evidence identity.",
                    )
                continue
            if proof is None:
                errors.add(
                    "residual_proof_missing",
                    _pointer(item_path, "proof"),
                    "Residual material hints require a closed proof.",
                )
                continue
            if (
                not isinstance(proof, dict)
                or set(proof) != {"kind", "residual_binding"}
                or proof.get("kind") != "residual_span"
            ):
                errors.add(
                    "invalid_residual_proof",
                    _pointer(item_path, "proof"),
                    "Residual material proof is invalid.",
                )
                continue
            try:
                binding = require_valid_residual_binding(
                    proof.get("residual_binding")  # type: ignore[arg-type]
                )
            except (ResidualAuthorityError, TypeError):
                errors.add(
                    "invalid_residual_proof",
                    _pointer(_pointer(item_path, "proof"), "residual_binding"),
                    "Residual material proof is invalid.",
                )
                continue
            if binding["residual_id"] != residual_match.group("residual_id"):
                errors.add(
                    "residual_evidence_reference_mismatch",
                    _pointer(item_path, "evidence_ref"),
                    "Residual evidence identity does not match its binding.",
                )
                continue
            prior = residual_proofs.get(evidence_ref)
            if prior is not None and prior != binding:
                errors.add(
                    "residual_identity_collision",
                    _pointer(_pointer(item_path, "proof"), "residual_binding"),
                    "One residual identity cannot carry multiple proofs.",
                )
                continue
            residual_proofs[evidence_ref] = binding
    _collection_ids(candidate.get("reference_refs"), "reference_id", "/reference_refs", errors)
    _collection_ids(candidate.get("technique_refs"), "card_id", "/technique_refs", errors)
    _collection_ids(candidate.get("assumptions"), "assumption_id", "/assumptions", errors)
    _collection_ids(candidate.get("missing_evidence"), "gap_id", "/missing_evidence", errors)
    _collection_ids(candidate.get("open_decisions"), "decision_id", "/open_decisions", errors)
    _validate_reference_kinds(candidate, errors)


def inspect_blueprint_candidate(candidate: Any) -> dict[str, Any]:
    """Validate the in-memory candidate without resolving persisted bindings."""

    errors = _Errors()
    try:
        validate_json_value(candidate, limits=BLUEPRINT_JSON_LIMITS)
    except StrictJsonError as exc:
        errors.extend_strict(exc)
    structurally_safe = not errors.items and isinstance(candidate, dict)
    if structurally_safe:
        if BLUEPRINT_CANDIDATE_SCHEMA is None:
            errors.add(
                "schema_unavailable",
                "",
                "The bundled Blueprint schema is unavailable.",
            )
        else:
            _validate_schema(candidate, BLUEPRINT_CANDIDATE_SCHEMA, errors)
            if not errors.items:
                _cross_validate(candidate, errors)
    schema_valid = not errors.items and not errors.truncated
    digest = canonical_sha256(candidate) if structurally_safe else None
    material_digest = (
        material_constraint_projection_digest_v1(candidate) if schema_valid else None
    )
    return {
        "_structurally_safe": structurally_safe,
        "schema_valid": schema_valid,
        "candidate_digest": digest,
        "material_constraint_projection_digest_v1": material_digest,
        "errors": errors.result(),
        "errors_truncated": errors.truncated,
    }


def decode_blueprint_candidate(payload: str | bytes | bytearray) -> dict[str, Any]:
    """Decode one CLI envelope; domain inspection applies the tighter candidate limits."""

    candidate = decode_strict_json(payload, limits=BLUEPRINT_TRANSPORT_JSON_LIMITS)
    assert isinstance(candidate, dict)
    return candidate


def material_constraint_projection_digest_v1(candidate: dict[str, Any]) -> str:
    constraints = candidate.get("constraints")
    constraints = constraints if isinstance(constraints, dict) else {}
    material = candidate.get("material_hints")
    material = material if isinstance(material, list) else []
    projection = {
        "must_include": constraints.get("must_include", []),
        "must_exclude": constraints.get("must_exclude", []),
        "material_hints": [
            (
                {
                    "evidence_ref": item.get("evidence_ref"),
                    "script_block_ids": item.get("script_block_ids"),
                    "proof": item.get("proof"),
                }
                if "proof" in item
                else {
                    "evidence_ref": item.get("evidence_ref"),
                    "script_block_ids": item.get("script_block_ids"),
                }
            )
            for item in material
            if isinstance(item, dict)
        ],
    }
    return canonical_sha256(projection)


def evidence_reference_paths(candidate: dict[str, Any]) -> list[tuple[str, str]]:
    """Collect exact evidence URIs without returning any surrounding text."""

    result: list[tuple[str, str]] = []
    constraints = candidate.get("constraints")
    if isinstance(constraints, dict):
        for group in ("must_include", "must_exclude"):
            items = constraints.get(group)
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str):
                    result.append(
                        (
                            f"/constraints/{group}/{index}/evidence_ref",
                            item["evidence_ref"],
                        )
                    )
    materials = candidate.get("material_hints")
    if isinstance(materials, list):
        for index, item in enumerate(materials):
            if isinstance(item, dict) and isinstance(item.get("evidence_ref"), str):
                result.append((f"/material_hints/{index}/evidence_ref", item["evidence_ref"]))
    references = candidate.get("reference_refs")
    if isinstance(references, list):
        for index, item in enumerate(references):
            if (
                isinstance(item, dict)
                and item.get("kind") == "evidence"
                and isinstance(item.get("locator"), str)
            ):
                result.append((f"/reference_refs/{index}/locator", item["locator"]))
    return result


def declared_sources(candidate: dict[str, Any]) -> list[str]:
    sources: set[str] = set()
    for section in ("intent", "script", "direction"):
        value = candidate.get(section)
        source = value.get("declared_source") if isinstance(value, dict) else None
        if isinstance(source, str) and source in _DECLARED_SOURCES:
            sources.add(source)
    return sorted(sources)


def has_starting_input(candidate: dict[str, Any]) -> bool:
    """Return whether any supported cold-start spine is present.

    This is planning state, not a JSON-schema invariant: an explicitly empty
    candidate is structurally valid but cannot enter coverage yet.
    """

    intent = candidate.get("intent")
    has_intent = isinstance(intent, dict) and any(
        isinstance(intent.get(key), str) and bool(intent[key].strip())
        for key in ("goal", "stance")
    )
    script = candidate.get("script")
    has_script = isinstance(script, dict) and bool(script.get("blocks"))
    return any(
        (
            has_intent,
            has_script,
            bool(candidate.get("material_hints")),
            bool(candidate.get("reference_refs")),
        )
    )


def candidate_outline(candidate: dict[str, Any]) -> dict[str, int]:
    script = candidate.get("script")
    constraints = candidate.get("constraints")
    return {
        "script_block_count": len(script.get("blocks", [])) if isinstance(script, dict) and isinstance(script.get("blocks"), list) else 0,
        "must_include_count": len(constraints.get("must_include", [])) if isinstance(constraints, dict) and isinstance(constraints.get("must_include"), list) else 0,
        "must_exclude_count": len(constraints.get("must_exclude", [])) if isinstance(constraints, dict) and isinstance(constraints.get("must_exclude"), list) else 0,
        "material_hint_count": len(candidate.get("material_hints", [])) if isinstance(candidate.get("material_hints"), list) else 0,
        "reference_count": len(candidate.get("reference_refs", [])) if isinstance(candidate.get("reference_refs"), list) else 0,
        "technique_count": len(candidate.get("technique_refs", [])) if isinstance(candidate.get("technique_refs"), list) else 0,
        "assumption_count": len(candidate.get("assumptions", [])) if isinstance(candidate.get("assumptions"), list) else 0,
        "missing_evidence_count": len(candidate.get("missing_evidence", [])) if isinstance(candidate.get("missing_evidence"), list) else 0,
        "open_decision_count": len(candidate.get("open_decisions", [])) if isinstance(candidate.get("open_decisions"), list) else 0,
    }


def _safe_legacy_text(value: Any, *, limit: int) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    normalized = value.strip()
    if contains_private_locator(normalized):
        return None, "legacy_private_locator_redacted"
    if len(normalized) > limit:
        return None, "legacy_text_over_limit"
    return normalized, None


def _legacy_text_list(
    value: Any,
    *,
    limit: int,
    item_limit: int,
) -> tuple[list[str], set[str]]:
    result: list[str] = []
    gaps: set[str] = set()
    if not isinstance(value, list):
        return result, gaps
    if len(value) > limit:
        gaps.add("legacy_list_truncated")
    for item in value[:limit]:
        text, gap = _safe_legacy_text(item, limit=item_limit)
        if gap:
            gaps.add(gap)
        if text is not None and text not in result:
            result.append(text)
    return result, gaps


def _legacy_candidate_evidence(brief: dict[str, Any]) -> tuple[list[str], set[str]]:
    candidates = brief.get("candidate_refs")
    result: list[str] = []
    gaps: set[str] = set()
    observed_ids: set[str] = set()
    if isinstance(candidates, list):
        if len(candidates) > 256:
            gaps.add("legacy_candidate_refs_truncated")
        for candidate in candidates[:256]:
            if not isinstance(candidate, dict):
                gaps.add("legacy_candidate_ref_omitted")
                continue
            raw_id = candidate.get("id")
            if isinstance(raw_id, str) and _IDENTIFIER.fullmatch(raw_id):
                observed_ids.add(raw_id)
            pairs: list[tuple[str, Any]] = [
                ("asset", candidate.get("asset_id")),
                ("span", candidate.get("segment_id")),
            ]
            if candidate.get("result_type") == "video_segment":
                pairs.append(("span", candidate.get("id")))
            elif candidate.get("result_type") in {
                "asset",
                "audio_asset",
                "image_asset",
                "video_asset",
            }:
                pairs.append(("asset", candidate.get("id")))
            for kind, identifier in pairs:
                if not isinstance(identifier, str) or _IDENTIFIER.fullmatch(identifier) is None:
                    continue
                evidence = (
                    asset_evidence_id(identifier)
                    if kind == "asset"
                    else span_evidence_id(identifier)
                )
                if evidence not in result:
                    if len(result) >= 256:
                        gaps.add("legacy_candidate_evidence_truncated")
                    else:
                        result.append(evidence)
    declared_ids = brief.get("candidate_ref_ids")
    if isinstance(declared_ids, list):
        valid_declared = {
            item
            for item in declared_ids
            if isinstance(item, str) and _IDENTIFIER.fullmatch(item)
        }
        if observed_ids and valid_declared != observed_ids:
            gaps.add("legacy_candidate_ref_conflict")
    return result, gaps


def _legacy_profile(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    profile_id = value.get("profile_id")
    revision = value.get("revision")
    digest = value.get("content_sha256")
    if (
        isinstance(profile_id, str)
        and _IDENTIFIER.fullmatch(profile_id)
        and type(revision) is int
        and 1 <= revision <= 1_000_000
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest.casefold())
    ):
        return {
            "profile_id": profile_id,
            "revision": revision,
            "content_sha256": digest.casefold(),
        }
    return None


def project_legacy_brief_to_candidate(
    *,
    project_id: str,
    brief: dict[str, Any],
    brief_revision: int,
    brief_content_sha256: str,
    observed_timeline: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Project a verified legacy object through a strict, deterministic allowlist."""

    gaps: set[str] = set()

    def text(key: str, limit: int) -> str | None:
        value, gap = _safe_legacy_text(brief.get(key), limit=limit)
        if gap:
            gaps.add(gap)
        return value

    include_text, include_gaps = _legacy_text_list(
        brief.get("must_include"), limit=64, item_limit=1000
    )
    exclude_text, exclude_gaps = _legacy_text_list(
        brief.get("must_exclude"), limit=64, item_limit=1000
    )
    gaps.update(include_gaps)
    gaps.update(exclude_gaps)
    evidence_refs, candidate_gaps = _legacy_candidate_evidence(brief)
    gaps.update(candidate_gaps)
    assumptions_text, assumption_gaps = _legacy_text_list(
        brief.get("assumptions"), limit=100, item_limit=1000
    )
    gaps.update(assumption_gaps)

    missing: list[dict[str, Any]] = []
    raw_missing = brief.get("missing_assets")
    if isinstance(raw_missing, list):
        if len(raw_missing) > 100:
            gaps.add("legacy_missing_evidence_truncated")
        for index, item in enumerate(raw_missing[:100]):
            raw_text = item
            if isinstance(item, dict):
                raw_text = item.get("description") or item.get("why_needed")
            description, gap = _safe_legacy_text(raw_text, limit=1000)
            if gap:
                gaps.add(gap)
            if description is not None:
                missing.append(
                    {
                        "gap_id": f"legacy_missing_{index + 1:03d}",
                        "description": description,
                        "script_block_ids": [],
                        "required_before": "not_blocking",
                    }
                )

    duration = brief.get("duration_ms")
    if type(duration) is not int or not 1_000 <= duration <= 1_800_000:
        duration = None
    aspect = brief.get("aspect_ratio")
    if aspect not in {"16:9", "9:16", "1:1", "4:5"}:
        aspect = None
    raw_creator_profile = brief.get("creator_profile_ref")
    creator_context = _legacy_profile(raw_creator_profile)
    if raw_creator_profile is not None and creator_context is None:
        gaps.add("legacy_creator_context_unverified")
    base_timeline = None
    if observed_timeline is not None:
        base_timeline = {
            "timeline_id": observed_timeline["timeline_id"],
            "revision": observed_timeline["revision"],
            "content_sha256": observed_timeline["content_sha256"],
        }
    candidate = {
        "object": BLUEPRINT_CANDIDATE_OBJECT,
        "schema_version": BLUEPRINT_SCHEMA_VERSION,
        "project_id": project_id,
        "base": {
            "legacy_brief": {
                "revision": brief_revision,
                "content_sha256": brief_content_sha256,
            },
            "observed_timeline": base_timeline,
        },
        "intent": {
            "goal": text("goal", 4000),
            "stance": None,
            "audience": text("audience", 1000),
            "platform": text("platform", 200),
            "declared_source": "legacy_unknown",
        },
        "script": {"declared_source": "legacy_unknown", "blocks": []},
        "direction": {
            "theme": None,
            "narrative_arc": text("narrative_arc", 4000),
            "emotion": None,
            "tone": text("tone", 1000),
            "pace": text("pace", 1000),
            "declared_source": "legacy_unknown",
        },
        "output": {"duration_target_ms": duration, "aspect_ratio": aspect},
        "constraints": {
            "must_include": [
                {
                    "constraint_id": f"legacy_include_{index + 1:03d}",
                    "text": value,
                    "evidence_ref": None,
                    "script_block_ids": [],
                }
                for index, value in enumerate(include_text)
            ],
            "must_exclude": [
                {
                    "constraint_id": f"legacy_exclude_{index + 1:03d}",
                    "text": value,
                    "evidence_ref": None,
                    "script_block_ids": [],
                }
                for index, value in enumerate(exclude_text)
            ],
        },
        "material_hints": [
            {
                "hint_id": f"legacy_hint_{index + 1:03d}",
                "evidence_ref": evidence,
                "script_block_ids": [],
                "reason": None,
            }
            for index, evidence in enumerate(evidence_refs)
        ],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {
            "creator_context": creator_context,
            "wiki_generation": None,
        },
        "assumptions": [
            {"assumption_id": f"legacy_assumption_{index + 1:03d}", "text": value}
            for index, value in enumerate(assumptions_text)
        ],
        "missing_evidence": missing,
        "open_decisions": [],
    }
    return candidate, sorted(gaps)


def strict_persisted_object(
    raw_json: Any, stored_sha256: Any
) -> tuple[dict[str, Any] | None, str | None, str]:
    """Verify stored bytes before parsing; never fall back to an older revision."""

    if not isinstance(raw_json, str):
        return None, None, "invalid_json"
    try:
        computed = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None, None, "invalid_json"
    stored = (
        stored_sha256.casefold()
        if isinstance(stored_sha256, str) and _SHA256.fullmatch(stored_sha256.casefold())
        else None
    )
    if stored is None:
        return None, computed, "invalid_stored_digest"
    if computed != stored:
        return None, computed, "digest_mismatch"
    try:
        parsed = decode_strict_json(raw_json, limits=PERSISTED_BLUEPRINT_SOURCE_LIMITS)
    except StrictJsonError:
        return None, computed, "invalid_json"
    if not isinstance(parsed, dict):
        return None, computed, "invalid_json"
    return parsed, computed, "verified"


def stable_codes(values: Iterable[str]) -> list[str]:
    return sorted(set(values))


__all__ = [
    "BLUEPRINT_CANDIDATE_OBJECT",
    "BLUEPRINT_CANDIDATE_SCHEMA",
    "BLUEPRINT_JSON_LIMITS",
    "BLUEPRINT_MAX_ERRORS",
    "BLUEPRINT_SCHEMA_AVAILABLE",
    "BLUEPRINT_SCHEMA_SHA256",
    "BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256",
    "BLUEPRINT_SCHEMA_VERSION",
    "BLUEPRINT_SHADOW_OBJECT",
    "BLUEPRINT_TRANSPORT_JSON_LIMITS",
    "BLUEPRINT_VALIDATION_OBJECT",
    "blueprint_contract_summary",
    "blueprint_safety_summary",
    "candidate_outline",
    "canonical_sha256",
    "decode_blueprint_candidate",
    "declared_sources",
    "evidence_reference_paths",
    "has_starting_input",
    "inspect_blueprint_candidate",
    "project_legacy_brief_to_candidate",
    "material_constraint_projection_digest_v1",
    "stable_codes",
    "strict_persisted_object",
    "valid_user_text_reference",
]
