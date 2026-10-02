"""Pure contract for immutable Creative Blueprint proposal revisions.

The A1 candidate is a negotiation envelope.  This module owns the distinct
persisted Blueprint/v1 document and deliberately has no database, network, or
filesystem side effects beyond reading its bundled schema at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from ipaddress import ip_address
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence
import unicodedata
from urllib.parse import urlsplit

from .residual_identity_contract import (
    ResidualIdentityContractError,
    require_valid_residual_binding,
)


BLUEPRINT_OBJECT = "memolens.creative_blueprint"
BLUEPRINT_SCHEMA_VERSION = "1"
BLUEPRINT_COMPILER = "memolens.blueprint-proposal-compiler/v1"
BLUEPRINT_STATUS = "proposal"
BLUEPRINT_AUTHORITY_STATE = "unverified"
BLUEPRINT_AUTHORITY_CLAIM = "agent_proposal"
BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256 = (
    "e239655e84014be04434eae19497706b28e62280b86b9db93bb1c80e32a56d52"
)
BLUEPRINT_RESIDUAL_SCHEMA_V1_EXPECTED_SHA256 = (
    "2a7be876aff82b642bee8dc09060e21c57352c9ef4826043daea1a3487689e64"
)
BLUEPRINT_MAX_ERRORS = 64
BLUEPRINT_MAX_CANONICAL_BYTES = 1_048_576
BLUEPRINT_MAX_SEMANTIC_BYTES = 524_288
BLUEPRINT_MAX_DEPTH = 16
BLUEPRINT_MAX_NODES = 20_000
BLUEPRINT_MAX_OBJECT_ITEMS = 64
BLUEPRINT_MAX_ARRAY_ITEMS = 512
BLUEPRINT_MAX_STRING_CHARS = 12_000
BLUEPRINT_MAX_INTEGER_BITS = 4_096

SEMANTIC_SECTIONS = (
    "intent",
    "script",
    "direction",
    "output",
    "constraints",
    "material_hints",
    "reference_refs",
    "technique_refs",
    "bindings",
    "assumptions",
    "missing_evidence",
    "open_decisions",
)
DECISION_UNITS = (
    "intent_goal",
    "intent_stance",
    "script",
    "creative_direction",
    "output",
    "material_constraints",
    "references",
    "techniques",
)

_SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "creative-blueprint-v1.schema.json"
_RESIDUAL_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "creative-blueprint-residual-v1.schema.json"
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/(?:asset|span)/[A-Za-z0-9][A-Za-z0-9._-]{0,199}$"
)
_RESIDUAL_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<residual_id>rseg_[0-9a-f]{64})$"
)
_PRIVATE_LOCATOR = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9+.-])(?:file|data):"
    r"|[A-Za-z]:[\\/]"
    r"|\\\\"
    r"|~[\\/]"
    r"|/(?:Users|home|var|tmp|Volumes|Library|etc|opt|srv|mnt)(?=/|$)"
    r"|(?<![^\W_])/(?![/\s])"
    r")",
    re.IGNORECASE,
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


def _load_schema(
    path: Path,
    expected_sha256: str,
) -> tuple[dict[str, Any] | None, bytes | None]:
    try:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_sha256:
            return None, None
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if type(value) is not dict:
        return None, None
    return value, raw


BLUEPRINT_SCHEMA, _BLUEPRINT_SCHEMA_BYTES = _load_schema(
    _SCHEMA_PATH,
    BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256,
)
BLUEPRINT_SCHEMA_AVAILABLE = BLUEPRINT_SCHEMA is not None
BLUEPRINT_SCHEMA_SHA256 = (
    hashlib.sha256(_BLUEPRINT_SCHEMA_BYTES).hexdigest()
    if _BLUEPRINT_SCHEMA_BYTES is not None
    else None
)
BLUEPRINT_RESIDUAL_SCHEMA, _BLUEPRINT_RESIDUAL_SCHEMA_BYTES = _load_schema(
    _RESIDUAL_SCHEMA_PATH,
    BLUEPRINT_RESIDUAL_SCHEMA_V1_EXPECTED_SHA256,
)
BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE = BLUEPRINT_RESIDUAL_SCHEMA is not None
BLUEPRINT_RESIDUAL_SCHEMA_SHA256 = (
    hashlib.sha256(_BLUEPRINT_RESIDUAL_SCHEMA_BYTES).hexdigest()
    if _BLUEPRINT_RESIDUAL_SCHEMA_BYTES is not None
    else None
)


@dataclass(frozen=True)
class BlueprintContractIssue:
    """One stable, non-reflective contract diagnostic."""

    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class BlueprintContractError(ValueError):
    """Raised when a value cannot be compiled as a persisted Blueprint."""

    def __init__(self, errors: Sequence[Mapping[str, str]]) -> None:
        self.errors = [dict(item) for item in errors]
        super().__init__("Creative Blueprint contract validation failed.")


class _Issues:
    def __init__(self) -> None:
        self.items: list[BlueprintContractIssue] = []
        self.truncated = False

    def add(self, code: str, path: str, message: str) -> None:
        if len(self.items) >= BLUEPRINT_MAX_ERRORS:
            self.truncated = True
            return
        self.items.append(BlueprintContractIssue(code, path, message))

    def result(self) -> list[dict[str, str]]:
        return [item.as_dict() for item in self.items]


def _pointer(path: str, token: str | int) -> str:
    if isinstance(token, int):
        return f"{path}/{token}"
    return f"{path}/{token.replace('~', '~0').replace('/', '~1')}"


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON representation used by all B0 digests."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


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


class _ResourceGate:
    def __init__(self, issues: _Issues, max_bytes: int) -> None:
        self.issues = issues
        self.max_bytes = max_bytes
        self.seen: set[int] = set()
        self.nodes = 0
        self.encoded_bytes = 0
        self.stopped = False

    def charge(self, size: int, path: str) -> None:
        self.encoded_bytes += size
        if self.encoded_bytes > self.max_bytes:
            self.issues.add("json_too_large", path, "Canonical JSON exceeds the supported size.")
            self.stopped = True

    def charge_string(self, item: str, path: str) -> None:
        try:
            encoded = json.dumps(item, ensure_ascii=False).encode("utf-8")
        except UnicodeEncodeError:
            self.issues.add("invalid_utf8", path, "String is not valid Unicode text.")
            self.stopped = True
            return
        self.charge(len(encoded), path)

    def walk(self, item: Any, path: str, depth: int) -> None:
        if self.stopped:
            return
        self.nodes += 1
        if self.nodes > BLUEPRINT_MAX_NODES:
            self.issues.add("json_too_many_nodes", path, "JSON contains too many values.")
            self.stopped = True
            return
        if depth > BLUEPRINT_MAX_DEPTH:
            self.issues.add("json_too_deep", path, "JSON nesting is deeper than allowed.")
            return
        item_type = type(item)
        if item is None:
            self.charge(4, path)
            return
        if item_type is bool:
            self.charge(4 if item else 5, path)
            return
        if item_type is int:
            if item.bit_length() > BLUEPRINT_MAX_INTEGER_BITS:
                self.issues.add("integer_too_large", path, "Integer exceeds the supported size.")
                self.stopped = True
                return
            self.charge(len(str(item).encode("ascii")), path)
            return
        if item_type is float:
            self.issues.add("unsupported_json_type", path, "Value uses an unsupported JSON type.")
            return
        if item_type is str:
            self._walk_string(item, path)
            return
        if item_type not in {dict, list}:
            self.issues.add("unsupported_json_type", path, "Value uses an unsupported JSON type.")
            self.stopped = True
            return
        identity = id(item)
        if identity in self.seen:
            self.issues.add("json_cycle", path, "JSON value contains a cycle.")
            return
        self.seen.add(identity)
        try:
            if item_type is dict:
                self._walk_object(item, path, depth)
            else:
                self._walk_array(item, path, depth)
        finally:
            self.seen.remove(identity)

    def _walk_string(self, item: str, path: str) -> None:
        if len(item) > BLUEPRINT_MAX_STRING_CHARS:
            self.issues.add("string_too_long", path, "String is longer than allowed.")
            self.stopped = True
            return
        self.charge_string(item, path)
        if not self.stopped and _contains_forbidden_control(item):
            self.issues.add(
                "forbidden_control_character",
                path,
                "String contains a forbidden control character.",
            )

    def _walk_object(self, item: dict[Any, Any], path: str, depth: int) -> None:
        if len(item) > BLUEPRINT_MAX_OBJECT_ITEMS:
            self.issues.add("object_too_large", path, "Object has too many members.")
            self.stopped = True
            return
        self.charge(2 + max(0, len(item) - 1) + len(item), path)
        child_path = _pointer(path, "*")
        keys = list(item)
        if any(type(key) is not str for key in keys):
            self.issues.add("non_string_key", path, "Object keys must be strings.")
            self.stopped = True
            return
        for key in keys:
            self._walk_string(key, child_path)
            if self.stopped or self.issues.items:
                return
        for key in sorted(keys):
            self.walk(item[key], child_path, depth + 1)
            if self.stopped:
                return

    def _walk_array(self, item: list[Any], path: str, depth: int) -> None:
        if len(item) > BLUEPRINT_MAX_ARRAY_ITEMS:
            self.issues.add("array_too_long", path, "Array has too many items.")
            self.stopped = True
            return
        self.charge(2 + max(0, len(item) - 1), path)
        for index, child in enumerate(item):
            self.walk(child, _pointer(path, index), depth + 1)
            if self.stopped:
                return


def _resource_gate(value: Any, issues: _Issues, *, max_bytes: int) -> bool:
    gate = _ResourceGate(issues, max_bytes)
    gate.walk(value, "", 1)

    if issues.items:
        return False
    try:
        size = len(canonical_json(value).encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError):
        issues.add("invalid_json", "", "Value cannot be represented as canonical JSON.")
        return False
    if size != gate.encoded_bytes or size > max_bytes:
        issues.add("json_too_large", "", "Canonical JSON exceeds the supported size.")
        return False
    return True


def _object(
    value: Any,
    *,
    path: str,
    required: Iterable[str],
    issues: _Issues,
) -> dict[str, Any] | None:
    if type(value) is not dict:
        issues.add("schema_type", path, "Value has the wrong JSON type.")
        return None
    required_set = set(required)
    for key in sorted(required_set - set(value)):
        issues.add("required_field_missing", _pointer(path, key), "A required field is missing.")
    if set(value) - required_set:
        issues.add("unknown_field", _pointer(path, "*"), "Object contains an unsupported field.")
    return value


def _array(value: Any, *, path: str, maximum: int, issues: _Issues) -> list[Any] | None:
    if type(value) is not list:
        issues.add("schema_type", path, "Value has the wrong JSON type.")
        return None
    if len(value) > maximum:
        issues.add("array_too_long", path, "Array has too many items.")
    return value


def _identifier(value: Any, *, path: str, issues: _Issues) -> bool:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        issues.add("invalid_identifier", path, "Value is not a supported opaque identifier.")
        return False
    return True


def _sha256(value: Any, *, path: str, issues: _Issues, nullable: bool = False) -> bool:
    if nullable and value is None:
        return True
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        issues.add("invalid_sha256", path, "Value is not a lowercase SHA-256 digest.")
        return False
    return True


def _revision(value: Any, *, path: str, issues: _Issues) -> bool:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        issues.add("invalid_revision", path, "Revision is outside the supported range.")
        return False
    return True


def _text(
    value: Any,
    *,
    path: str,
    maximum: int,
    issues: _Issues,
    nullable: bool = False,
) -> bool:
    if nullable and value is None:
        return True
    if type(value) is not str:
        issues.add("schema_type", path, "Value has the wrong JSON type.")
        return False
    valid = True
    if not value or not _has_visible_text(value):
        issues.add("blank_string", path, "String must contain visible text.")
        valid = False
    if len(value) > maximum:
        issues.add("string_too_long", path, "String is longer than allowed.")
        valid = False
    if _contains_forbidden_control(value):
        issues.add("forbidden_control_character", path, "String contains a forbidden control character.")
        valid = False
    return valid


def _enum(value: Any, *, path: str, allowed: set[Any], issues: _Issues) -> bool:
    if not any(type(value) is type(item) and value == item for item in allowed):
        issues.add("schema_enum", path, "Value is outside the supported enum.")
        return False
    return True


def _revision_ref(value: Any, *, path: str, issues: _Issues) -> bool:
    item = _object(
        value,
        path=path,
        required=("revision", "content_sha256"),
        issues=issues,
    )
    if item is None:
        return False
    return all(
        (
            _revision(item.get("revision"), path=f"{path}/revision", issues=issues),
            _sha256(item.get("content_sha256"), path=f"{path}/content_sha256", issues=issues),
        )
    )


def _validate_intent(value: Any, issues: _Issues) -> None:
    path = "/intent"
    item = _object(value, path=path, required=("goal", "stance", "audience", "platform"), issues=issues)
    if item is None:
        return
    _text(item.get("goal"), path=f"{path}/goal", maximum=4000, nullable=True, issues=issues)
    _text(item.get("stance"), path=f"{path}/stance", maximum=4000, nullable=True, issues=issues)
    _text(item.get("audience"), path=f"{path}/audience", maximum=1000, nullable=True, issues=issues)
    _text(item.get("platform"), path=f"{path}/platform", maximum=200, nullable=True, issues=issues)


def _validate_script(value: Any, issues: _Issues) -> set[str]:
    path = "/script"
    item = _object(value, path=path, required=("blocks",), issues=issues)
    if item is None:
        return set()
    blocks = _array(item.get("blocks"), path=f"{path}/blocks", maximum=256, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(blocks or []):
        item_path = f"{path}/blocks/{index}"
        block = _object(raw, path=item_path, required=("block_id", "text"), issues=issues)
        if block is None:
            continue
        block_id = block.get("block_id")
        if _identifier(block_id, path=f"{item_path}/block_id", issues=issues):
            if block_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/block_id", "Identifiers must be unique.")
            ids.add(block_id)
        _text(block.get("text"), path=f"{item_path}/text", maximum=12000, issues=issues)
    return ids


def _validate_direction(value: Any, issues: _Issues) -> None:
    path = "/direction"
    keys = ("theme", "narrative_arc", "emotion", "tone", "pace")
    item = _object(value, path=path, required=keys, issues=issues)
    if item is None:
        return
    for key, maximum in (("theme", 4000), ("narrative_arc", 4000), ("emotion", 1000), ("tone", 1000), ("pace", 1000)):
        _text(item.get(key), path=f"{path}/{key}", maximum=maximum, nullable=True, issues=issues)


def _validate_output(value: Any, issues: _Issues) -> None:
    path = "/output"
    item = _object(value, path=path, required=("duration_target_ms", "aspect_ratio"), issues=issues)
    if item is None:
        return
    duration = item.get("duration_target_ms")
    if duration is not None and (type(duration) is not int or not 1000 <= duration <= 1_800_000):
        issues.add("invalid_duration", f"{path}/duration_target_ms", "Duration is outside the supported range.")
    _enum(item.get("aspect_ratio"), path=f"{path}/aspect_ratio", allowed={None, "16:9", "9:16", "1:1", "4:5"}, issues=issues)


def _validate_script_links(value: Any, *, path: str, block_ids: set[str], issues: _Issues) -> None:
    links = _array(value, path=path, maximum=128, issues=issues)
    seen: set[str] = set()
    for index, block_id in enumerate(links or []):
        link_path = f"{path}/{index}"
        if not _identifier(block_id, path=link_path, issues=issues):
            continue
        if block_id in seen:
            issues.add("duplicate_array_item", link_path, "Array items must be unique.")
        seen.add(block_id)
        if block_id not in block_ids:
            issues.add("dangling_script_block", link_path, "Reference points to a missing script block.")


def _validate_constraint_group(
    value: Any,
    *,
    path: str,
    block_ids: set[str],
    issues: _Issues,
) -> tuple[set[str], set[str]]:
    rows = _array(value, path=path, maximum=64, issues=issues)
    ids: set[str] = set()
    evidence: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(
            raw,
            path=item_path,
            required=("constraint_id", "text", "evidence_ref", "script_block_ids"),
            issues=issues,
        )
        if item is None:
            continue
        constraint_id = item.get("constraint_id")
        if _identifier(constraint_id, path=f"{item_path}/constraint_id", issues=issues):
            if constraint_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/constraint_id", "Identifiers must be unique.")
            ids.add(constraint_id)
        text = item.get("text")
        evidence_ref = item.get("evidence_ref")
        _text(text, path=f"{item_path}/text", maximum=1000, nullable=True, issues=issues)
        if evidence_ref is not None:
            if (
                type(evidence_ref) is not str
                or _EVIDENCE_REF.fullmatch(evidence_ref) is None
                or _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref) is not None
            ):
                issues.add("invalid_evidence_reference", f"{item_path}/evidence_ref", "Evidence reference is invalid.")
            else:
                evidence.add(evidence_ref)
        if text is None and evidence_ref is None:
            issues.add("empty_constraint", item_path, "Constraint needs text, evidence, or both.")
        _validate_script_links(item.get("script_block_ids"), path=f"{item_path}/script_block_ids", block_ids=block_ids, issues=issues)
    return ids, evidence


def _validate_constraints(value: Any, block_ids: set[str], issues: _Issues) -> None:
    path = "/constraints"
    item = _object(value, path=path, required=("must_include", "must_exclude"), issues=issues)
    if item is None:
        return
    include_ids, include_evidence = _validate_constraint_group(
        item.get("must_include"), path=f"{path}/must_include", block_ids=block_ids, issues=issues
    )
    exclude_ids, exclude_evidence = _validate_constraint_group(
        item.get("must_exclude"), path=f"{path}/must_exclude", block_ids=block_ids, issues=issues
    )
    if include_ids & exclude_ids:
        issues.add("duplicate_identifier", path, "Constraint identifiers must be unique across both groups.")
    if include_evidence & exclude_evidence:
        issues.add("contradictory_evidence_constraint", path, "The same evidence cannot be required and excluded.")


def _validate_material_hints(value: Any, block_ids: set[str], issues: _Issues) -> None:
    path = "/material_hints"
    rows = _array(value, path=path, maximum=256, issues=issues)
    ids: set[str] = set()
    residual_proofs: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        residual_shape = type(raw) is dict and (
            "proof" in raw
            or (
                type(raw.get("evidence_ref")) is str
                and _RESIDUAL_EVIDENCE_REF.fullmatch(raw["evidence_ref"]) is not None
            )
        )
        required = (
            ("hint_id", "evidence_ref", "script_block_ids", "reason", "proof")
            if residual_shape
            else ("hint_id", "evidence_ref", "script_block_ids", "reason")
        )
        if (
            type(raw) is dict
            and "proof" not in raw
            and type(raw.get("evidence_ref")) is str
            and _RESIDUAL_EVIDENCE_REF.fullmatch(raw["evidence_ref"]) is not None
        ):
            issues.add(
                "residual_proof_missing",
                f"{item_path}/proof",
                "Residual material hints require a closed proof.",
            )
        item = _object(raw, path=item_path, required=required, issues=issues)
        if item is None:
            continue
        hint_id = item.get("hint_id")
        if _identifier(hint_id, path=f"{item_path}/hint_id", issues=issues):
            if hint_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/hint_id", "Identifiers must be unique.")
            ids.add(hint_id)
        evidence_ref = item.get("evidence_ref")
        if type(evidence_ref) is not str or _EVIDENCE_REF.fullmatch(evidence_ref) is None:
            issues.add("invalid_evidence_reference", f"{item_path}/evidence_ref", "Evidence reference is invalid.")
        residual_match = (
            _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
            if type(evidence_ref) is str
            else None
        )
        if residual_shape:
            proof = item.get("proof")
            proof_row = _object(
                proof,
                path=f"{item_path}/proof",
                required=("kind", "residual_binding"),
                issues=issues,
            )
            if residual_match is None:
                issues.add(
                    "residual_evidence_reference_mismatch",
                    f"{item_path}/evidence_ref",
                    "Residual proof requires its exact residual evidence identity.",
                )
            if proof_row is not None:
                if proof_row.get("kind") != "residual_span":
                    issues.add(
                        "invalid_residual_proof",
                        f"{item_path}/proof/kind",
                        "Residual material proof kind is invalid.",
                    )
                try:
                    binding = require_valid_residual_binding(
                        proof_row.get("residual_binding")  # type: ignore[arg-type]
                    )
                except (ResidualIdentityContractError, TypeError):
                    issues.add(
                        "invalid_residual_proof",
                        f"{item_path}/proof/residual_binding",
                        "Residual material proof is invalid.",
                    )
                else:
                    if (
                        residual_match is None
                        or binding["residual_id"] != residual_match.group("residual_id")
                    ):
                        issues.add(
                            "residual_evidence_reference_mismatch",
                            f"{item_path}/evidence_ref",
                            "Residual evidence identity does not match its binding.",
                        )
                    elif evidence_ref in residual_proofs and residual_proofs[evidence_ref] != binding:
                        issues.add(
                            "residual_identity_collision",
                            f"{item_path}/proof/residual_binding",
                            "One residual identity cannot carry multiple proofs.",
                        )
                    else:
                        residual_proofs[evidence_ref] = binding
        elif residual_match is not None:
            issues.add(
                "residual_proof_missing",
                f"{item_path}/proof",
                "Residual material hints require a closed proof.",
            )
        _validate_script_links(item.get("script_block_ids"), path=f"{item_path}/script_block_ids", block_ids=block_ids, issues=issues)
        _text(item.get("reason"), path=f"{item_path}/reason", maximum=1000, nullable=True, issues=issues)


def _looks_like_complete_relative_path(value: str) -> bool:
    normalized = value.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        return False
    if "\\" in normalized or normalized.startswith(("./", "../")):
        return True
    if "/" not in normalized:
        suffix = normalized.rpartition(".")[2].casefold()
        return bool(suffix in _KNOWN_FILE_SUFFIXES and _FILE_NAME_SHAPE.fullmatch(normalized))
    segments = normalized.split("/")
    return bool(
        not any(not segment or segment != segment.strip() for segment in segments)
        and _FILE_NAME_SHAPE.fullmatch(segments[-1])
    )


def _valid_external_https(value: Any) -> bool:
    if (
        type(value) is not str
        or len(value) > 4000
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x20 for character in value)
        or re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", value) is None
        or re.search(r"%(?![0-9A-Fa-f]{2})", value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
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
        return bool(
            domain
            and len(domain) <= 253
            and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in domain.split(".")
            )
        )


def _valid_user_text_reference(value: Any) -> bool:
    if type(value) is not str:
        return False
    normalized = value.strip()
    return bool(
        _has_visible_text(normalized)
        and not _contains_forbidden_control(normalized)
        and _PRIVATE_LOCATOR.search(normalized) is None
        and not _looks_like_complete_relative_path(normalized)
        and not normalized.startswith(("/", "~/"))
        and not re.match(r"^[A-Za-z]:[\\/]", normalized)
        and "://" not in normalized
        and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", normalized) is None
    )


def _validate_references(value: Any, issues: _Issues) -> None:
    path = "/reference_refs"
    rows = _array(value, path=path, maximum=64, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=("reference_id", "kind", "locator", "note"), issues=issues)
        if item is None:
            continue
        reference_id = item.get("reference_id")
        if _identifier(reference_id, path=f"{item_path}/reference_id", issues=issues):
            if reference_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/reference_id", "Identifiers must be unique.")
            ids.add(reference_id)
        kind = item.get("kind")
        _enum(kind, path=f"{item_path}/kind", allowed={"project", "research_snapshot", "evidence", "external_https", "user_text"}, issues=issues)
        locator = item.get("locator")
        _text(locator, path=f"{item_path}/locator", maximum=4000, issues=issues)
        if kind == "evidence" and (
            type(locator) is not str
            or _EVIDENCE_REF.fullmatch(locator) is None
            or _RESIDUAL_EVIDENCE_REF.fullmatch(locator) is not None
        ):
            issues.add("invalid_evidence_reference", f"{item_path}/locator", "Evidence reference is invalid.")
        elif kind == "external_https" and not _valid_external_https(locator):
            issues.add("invalid_https_reference", f"{item_path}/locator", "External reference must be a credential-free HTTPS URL.")
        elif kind in {"project", "research_snapshot"} and (type(locator) is not str or _IDENTIFIER.fullmatch(locator) is None):
            issues.add("invalid_opaque_reference", f"{item_path}/locator", "Reference must use an opaque identifier.")
        elif kind == "user_text" and not _valid_user_text_reference(locator):
            issues.add("invalid_user_text_reference", f"{item_path}/locator", "Inline reference text must not contain a path or URI.")
        _text(item.get("note"), path=f"{item_path}/note", maximum=1000, nullable=True, issues=issues)


def _validate_techniques(value: Any, issues: _Issues) -> None:
    path = "/technique_refs"
    rows = _array(value, path=path, maximum=64, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=("card_id", "revision", "declared_state"), issues=issues)
        if item is None:
            continue
        card_id = item.get("card_id")
        if _identifier(card_id, path=f"{item_path}/card_id", issues=issues):
            if card_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/card_id", "Identifiers must be unique.")
            ids.add(card_id)
        _revision(item.get("revision"), path=f"{item_path}/revision", issues=issues)
        _enum(item.get("declared_state"), path=f"{item_path}/declared_state", allowed={"proposed", "selected", "rejected"}, issues=issues)


def _validate_bindings(value: Any, issues: _Issues) -> None:
    path = "/bindings"
    item = _object(value, path=path, required=("creator_context", "wiki_generation"), issues=issues)
    if item is None:
        return
    creator = item.get("creator_context")
    if creator is not None:
        creator_item = _object(creator, path=f"{path}/creator_context", required=("profile_id", "revision", "content_sha256"), issues=issues)
        if creator_item is not None:
            _identifier(creator_item.get("profile_id"), path=f"{path}/creator_context/profile_id", issues=issues)
            _revision(creator_item.get("revision"), path=f"{path}/creator_context/revision", issues=issues)
            _sha256(creator_item.get("content_sha256"), path=f"{path}/creator_context/content_sha256", issues=issues)
    wiki = item.get("wiki_generation")
    if wiki is not None:
        wiki_item = _object(wiki, path=f"{path}/wiki_generation", required=("generation_id", "content_sha256"), issues=issues)
        if wiki_item is not None:
            _identifier(wiki_item.get("generation_id"), path=f"{path}/wiki_generation/generation_id", issues=issues)
            _sha256(wiki_item.get("content_sha256"), path=f"{path}/wiki_generation/content_sha256", issues=issues)


def _validate_named_text_collection(
    value: Any,
    *,
    path: str,
    id_key: str,
    text_key: str,
    maximum: int,
    issues: _Issues,
) -> None:
    rows = _array(value, path=path, maximum=maximum, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=(id_key, text_key), issues=issues)
        if item is None:
            continue
        identifier = item.get(id_key)
        if _identifier(identifier, path=f"{item_path}/{id_key}", issues=issues):
            if identifier in ids:
                issues.add("duplicate_identifier", f"{item_path}/{id_key}", "Identifiers must be unique.")
            ids.add(identifier)
        _text(item.get(text_key), path=f"{item_path}/{text_key}", maximum=1000, issues=issues)


def _validate_missing_evidence(value: Any, block_ids: set[str], issues: _Issues) -> None:
    path = "/missing_evidence"
    rows = _array(value, path=path, maximum=100, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=("gap_id", "description", "script_block_ids", "required_before"), issues=issues)
        if item is None:
            continue
        gap_id = item.get("gap_id")
        if _identifier(gap_id, path=f"{item_path}/gap_id", issues=issues):
            if gap_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/gap_id", "Identifiers must be unique.")
            ids.add(gap_id)
        _text(item.get("description"), path=f"{item_path}/description", maximum=1000, issues=issues)
        _validate_script_links(item.get("script_block_ids"), path=f"{item_path}/script_block_ids", block_ids=block_ids, issues=issues)
        _enum(item.get("required_before"), path=f"{item_path}/required_before", allowed={"coverage", "timeline", "render", "publish", "not_blocking"}, issues=issues)


def _validate_open_decisions(value: Any, issues: _Issues) -> None:
    path = "/open_decisions"
    rows = _array(value, path=path, maximum=100, issues=issues)
    ids: set[str] = set()
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=("decision_id", "question", "scope", "required_before"), issues=issues)
        if item is None:
            continue
        decision_id = item.get("decision_id")
        if _identifier(decision_id, path=f"{item_path}/decision_id", issues=issues):
            if decision_id in ids:
                issues.add("duplicate_identifier", f"{item_path}/decision_id", "Identifiers must be unique.")
            ids.add(decision_id)
        _text(item.get("question"), path=f"{item_path}/question", maximum=1000, issues=issues)
        _enum(item.get("scope"), path=f"{item_path}/scope", allowed={"intent", "stance", "script", "direction", "output", "material", "reference", "technique", "other"}, issues=issues)
        _enum(item.get("required_before"), path=f"{item_path}/required_before", allowed={"coverage", "timeline", "render", "publish", "not_blocking"}, issues=issues)


def validate_blueprint_semantic(semantic: Any) -> list[dict[str, str]]:
    """Validate the persisted semantic object and return bounded stable errors."""

    issues = _Issues()
    if not _resource_gate(semantic, issues, max_bytes=BLUEPRINT_MAX_SEMANTIC_BYTES):
        return issues.result()
    root = _object(semantic, path="", required=SEMANTIC_SECTIONS, issues=issues)
    if root is None:
        return issues.result()
    _validate_intent(root.get("intent"), issues)
    block_ids = _validate_script(root.get("script"), issues)
    _validate_direction(root.get("direction"), issues)
    _validate_output(root.get("output"), issues)
    _validate_constraints(root.get("constraints"), block_ids, issues)
    _validate_material_hints(root.get("material_hints"), block_ids, issues)
    _validate_references(root.get("reference_refs"), issues)
    _validate_techniques(root.get("technique_refs"), issues)
    _validate_bindings(root.get("bindings"), issues)
    _validate_named_text_collection(
        root.get("assumptions"),
        path="/assumptions",
        id_key="assumption_id",
        text_key="text",
        maximum=100,
        issues=issues,
    )
    _validate_missing_evidence(root.get("missing_evidence"), block_ids, issues)
    _validate_open_decisions(root.get("open_decisions"), issues)
    return issues.result()


def require_valid_blueprint_semantic(semantic: Any) -> dict[str, Any]:
    errors = validate_blueprint_semantic(semantic)
    if errors:
        raise BlueprintContractError(errors)
    assert isinstance(semantic, dict)
    return semantic


def blueprint_semantic_sha256(semantic: Mapping[str, Any]) -> str:
    require_valid_blueprint_semantic(semantic)
    return canonical_sha256(semantic)


def _decision_unit_payloads(semantic: Mapping[str, Any]) -> dict[str, Any]:
    intent = semantic["intent"]
    return {
        "intent_goal": {
            "goal": intent["goal"],
            "audience": intent["audience"],
            "platform": intent["platform"],
        },
        "intent_stance": {"stance": intent["stance"]},
        "script": semantic["script"],
        "creative_direction": semantic["direction"],
        "output": semantic["output"],
        "material_constraints": {
            "constraints": semantic["constraints"],
            "material_hints": semantic["material_hints"],
        },
        "references": {
            "reference_refs": semantic["reference_refs"],
            "bindings": semantic["bindings"],
        },
        "techniques": semantic["technique_refs"],
    }


def blueprint_decision_unit_payloads(semantic: Mapping[str, Any]) -> dict[str, Any]:
    """Return detached payloads for the eight frozen decision units."""

    require_valid_blueprint_semantic(semantic)
    # Canonical JSON round-tripping prevents callers from mutating the
    # persisted semantic object through a nested reference.
    value = json.loads(canonical_json(_decision_unit_payloads(semantic)))
    assert isinstance(value, dict)
    return value


def blueprint_decision_unit_digests(semantic: Mapping[str, Any]) -> dict[str, str]:
    """Return the eight frozen semantic decision-unit digests in contract order."""

    require_valid_blueprint_semantic(semantic)
    payloads = blueprint_decision_unit_payloads(semantic)
    return {name: canonical_sha256(payloads[name]) for name in DECISION_UNITS}


def _collect_evidence_refs(semantic: Mapping[str, Any]) -> tuple[str, ...]:
    refs: set[str] = set()
    constraints = semantic["constraints"]
    for group in ("must_include", "must_exclude"):
        for item in constraints[group]:
            evidence_ref = item["evidence_ref"]
            if evidence_ref is not None:
                refs.add(evidence_ref)
    for item in semantic["material_hints"]:
        refs.add(item["evidence_ref"])
    for item in semantic["reference_refs"]:
        if item["kind"] == "evidence":
            refs.add(item["locator"])
    return tuple(sorted(refs))


def blueprint_evidence_refs(semantic: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the exact sorted evidence set that a transaction must prove."""

    require_valid_blueprint_semantic(semantic)
    return _collect_evidence_refs(semantic)


def _residual_proofs(semantic: Mapping[str, Any]) -> dict[str, dict[str, object]]:
    proofs: dict[str, dict[str, object]] = {}
    for hint in semantic["material_hints"]:
        if not isinstance(hint, Mapping) or hint.get("proof") is None:
            continue
        proof = hint["proof"]
        assert isinstance(proof, Mapping)
        binding = require_valid_residual_binding(
            proof["residual_binding"]  # type: ignore[arg-type]
        )
        evidence_ref = str(hint["evidence_ref"])
        normalized = {
            "kind": "residual_span",
            "residual_binding": binding,
        }
        prior = proofs.get(evidence_ref)
        if prior is not None and prior != normalized:
            raise BlueprintContractError(
                [
                    {
                        "code": "residual_identity_collision",
                        "path": "/material_hints",
                        "message": "One residual identity cannot carry multiple proofs.",
                    }
                ]
            )
        proofs[evidence_ref] = normalized
    return proofs


def blueprint_residual_proofs(
    semantic: Mapping[str, Any],
) -> dict[str, dict[str, object]]:
    """Return detached exact residual proofs keyed by their ``rseg`` URI."""

    require_valid_blueprint_semantic(semantic)
    return json.loads(canonical_json(_residual_proofs(semantic)))


def blueprint_schema_sha256_for_semantic(
    semantic: Mapping[str, Any],
) -> str | None:
    """Select the one immutable schema artifact authorized by the semantic."""

    require_valid_blueprint_semantic(semantic)
    return (
        BLUEPRINT_RESIDUAL_SCHEMA_SHA256
        if _residual_proofs(semantic)
        else BLUEPRINT_SCHEMA_SHA256
    )


def _authority_for(semantic: Mapping[str, Any]) -> dict[str, Any]:
    digests = blueprint_decision_unit_digests(semantic)
    return {
        "state": BLUEPRINT_AUTHORITY_STATE,
        "decision_units": {
            name: {
                "semantic_sha256": digests[name],
                "claim": BLUEPRINT_AUTHORITY_CLAIM,
                "verified": False,
            }
            for name in DECISION_UNITS
        },
    }


def _validate_evidence_manifest(value: Any, semantic: Mapping[str, Any], issues: _Issues) -> None:
    path = "/evidence_manifest"
    rows = _array(value, path=path, maximum=512, issues=issues)
    observed: list[str] = []
    residual_proofs = _residual_proofs(semantic)
    for index, raw in enumerate(rows or []):
        item_path = f"{path}/{index}"
        item = _object(raw, path=item_path, required=("evidence_ref", "status", "proof_sha256"), issues=issues)
        if item is None:
            continue
        evidence_ref = item.get("evidence_ref")
        if type(evidence_ref) is not str or _EVIDENCE_REF.fullmatch(evidence_ref) is None:
            issues.add("invalid_evidence_reference", f"{item_path}/evidence_ref", "Evidence reference is invalid.")
        else:
            observed.append(evidence_ref)
        status = item.get("status")
        _enum(status, path=f"{item_path}/status", allowed={"verified", "unresolved"}, issues=issues)
        proof = item.get("proof_sha256")
        _sha256(proof, path=f"{item_path}/proof_sha256", nullable=True, issues=issues)
        if status == "verified" and proof is None:
            issues.add("evidence_proof_missing", f"{item_path}/proof_sha256", "Verified evidence requires a stable proof digest.")
        if status == "unresolved" and proof is not None:
            issues.add("unresolved_evidence_has_proof", f"{item_path}/proof_sha256", "Unresolved evidence cannot carry a proof digest.")
        residual_proof = residual_proofs.get(str(evidence_ref))
        if residual_proof is not None and (
            status != "verified" or proof != canonical_sha256(residual_proof)
        ):
            issues.add(
                "residual_proof_digest_mismatch",
                item_path,
                "Residual evidence must freeze the exact verified proof digest.",
            )
    if observed != sorted(set(observed)):
        issues.add("evidence_manifest_not_canonical", path, "Evidence manifest must be sorted and unique.")
    if tuple(observed) != _collect_evidence_refs(semantic):
        issues.add("evidence_manifest_mismatch", path, "Evidence manifest must exactly cover semantic evidence references.")


_UNSET = object()


def validate_blueprint_document(
    document: Any,
    *,
    expected_project_id: Any = _UNSET,
    expected_revision: Any = _UNSET,
    expected_content_sha256: Any = _UNSET,
    expected_operation_id: Any = _UNSET,
) -> list[dict[str, str]]:
    """Validate one full persisted document, including derived digest fields.

    Optional expected values bind the JSON to its database row.  The function
    never repairs or normalizes stored content.
    """

    issues = _Issues()
    if not _resource_gate(document, issues, max_bytes=BLUEPRINT_MAX_CANONICAL_BYTES):
        return issues.result()
    fields = (
        "object",
        "schema_version",
        "schema_sha256",
        "project_id",
        "revision",
        "parent",
        "created_by_operation_id",
        "status",
        "semantic_sha256",
        "semantic",
        "lineage",
        "evidence_manifest",
        "authority",
    )
    root = _object(document, path="", required=fields, issues=issues)
    if root is None:
        return issues.result()
    if not BLUEPRINT_SCHEMA_AVAILABLE:
        issues.add("schema_unavailable", "/schema_sha256", "The bundled Blueprint schema is unavailable.")
    if root.get("object") != BLUEPRINT_OBJECT:
        issues.add("schema_const", "/object", "Value does not match the required constant.")
    if root.get("schema_version") != BLUEPRINT_SCHEMA_VERSION:
        issues.add("schema_const", "/schema_version", "Value does not match the required constant.")
    if root.get("schema_sha256") not in {
        BLUEPRINT_SCHEMA_SHA256,
        BLUEPRINT_RESIDUAL_SCHEMA_SHA256,
    }:
        issues.add(
            "schema_digest_mismatch",
            "/schema_sha256",
            "Blueprint schema digest is not a supported immutable artifact.",
        )
    _identifier(root.get("project_id"), path="/project_id", issues=issues)
    revision = root.get("revision")
    revision_valid = _revision(revision, path="/revision", issues=issues)
    parent = root.get("parent")
    if parent is not None:
        _revision_ref(parent, path="/parent", issues=issues)
    if revision_valid:
        if revision == 1 and parent is not None:
            issues.add("invalid_parent", "/parent", "The first Blueprint revision cannot have a parent.")
        elif revision > 1:
            if type(parent) is not dict or parent.get("revision") != revision - 1:
                issues.add("invalid_parent", "/parent", "Blueprint parent must be the immediately preceding revision.")
    _identifier(root.get("created_by_operation_id"), path="/created_by_operation_id", issues=issues)
    if root.get("status") != BLUEPRINT_STATUS:
        issues.add("authority_escalation", "/status", "B0 only permits unverified proposal revisions.")

    semantic = root.get("semantic")
    semantic_errors = validate_blueprint_semantic(semantic)
    for error in semantic_errors:
        issues.add(error["code"], f"/semantic{error['path']}", error["message"])
    semantic_valid = not semantic_errors and isinstance(semantic, dict)
    if semantic_valid:
        expected_schema_sha256 = blueprint_schema_sha256_for_semantic(semantic)
        if expected_schema_sha256 == BLUEPRINT_RESIDUAL_SCHEMA_SHA256:
            if not BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE:
                issues.add(
                    "schema_unavailable",
                    "/schema_sha256",
                    "The bundled residual Blueprint schema is unavailable.",
                )
        if root.get("schema_sha256") != expected_schema_sha256:
            issues.add(
                "schema_digest_mismatch",
                "/schema_sha256",
                "Blueprint schema digest does not match its semantic profile.",
            )
        semantic_digest = canonical_sha256(semantic)
        if root.get("semantic_sha256") != semantic_digest:
            issues.add("semantic_digest_mismatch", "/semantic_sha256", "Semantic digest does not match the document content.")

    lineage = _object(
        root.get("lineage"),
        path="/lineage",
        required=("compiler", "source_candidate_sha256", "initial_legacy_brief"),
        issues=issues,
    )
    if lineage is not None:
        if lineage.get("compiler") != BLUEPRINT_COMPILER:
            issues.add("compiler_mismatch", "/lineage/compiler", "Compiler identity does not match the frozen contract.")
        _sha256(lineage.get("source_candidate_sha256"), path="/lineage/source_candidate_sha256", nullable=True, issues=issues)
        _revision_ref(lineage.get("initial_legacy_brief"), path="/lineage/initial_legacy_brief", issues=issues)

    if semantic_valid:
        _validate_evidence_manifest(root.get("evidence_manifest"), semantic, issues)
        expected_authority = _authority_for(semantic)
        if root.get("authority") != expected_authority:
            issues.add("authority_mismatch", "/authority", "Authority must be the derived unverified proposal projection.")

    if expected_project_id is not _UNSET and root.get("project_id") != expected_project_id:
        issues.add("row_identity_mismatch", "/project_id", "Document project identity does not match its row.")
    if expected_revision is not _UNSET and root.get("revision") != expected_revision:
        issues.add("row_identity_mismatch", "/revision", "Document revision does not match its row.")
    if expected_operation_id is not _UNSET and root.get("created_by_operation_id") != expected_operation_id:
        issues.add("row_identity_mismatch", "/created_by_operation_id", "Document operation identity does not match its row.")
    if expected_content_sha256 is not _UNSET:
        if type(expected_content_sha256) is not str or _SHA256.fullmatch(expected_content_sha256) is None:
            issues.add("invalid_stored_digest", "", "Stored content digest is invalid.")
        elif canonical_sha256(root) != expected_content_sha256:
            issues.add("content_digest_mismatch", "", "Document content digest does not match its row.")
    return issues.result()


def require_valid_blueprint_document(document: Any, **expected: Any) -> dict[str, Any]:
    errors = validate_blueprint_document(document, **expected)
    if errors:
        raise BlueprintContractError(errors)
    assert isinstance(document, dict)
    return document


def _detached_json(value: Any, *, path: str) -> Any:
    issues = _Issues()
    if not _resource_gate(value, issues, max_bytes=BLUEPRINT_MAX_CANONICAL_BYTES):
        raise BlueprintContractError(issues.result())
    try:
        return json.loads(canonical_json(value))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BlueprintContractError(
            [
                {
                    "code": "invalid_json",
                    "path": path,
                    "message": "Value cannot be represented as canonical JSON.",
                }
            ]
        ) from exc


def compile_blueprint_document(
    *,
    project_id: str,
    revision: int,
    parent: dict[str, Any] | None,
    created_by_operation_id: str,
    semantic: dict[str, Any],
    source_candidate_sha256: str | None,
    initial_legacy_brief: dict[str, Any],
    evidence_manifest: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compile server-owned identity and authority into one persisted document."""

    require_valid_blueprint_semantic(semantic)
    # Canonical round trips create detached built-in JSON values and prevent a
    # caller from mutating the revision after validation.
    semantic_copy = _detached_json(semantic, path="/semantic")
    manifest_copy = _detached_json(evidence_manifest, path="/evidence_manifest")
    if isinstance(manifest_copy, list) and all(
        isinstance(item, dict) and type(item.get("evidence_ref")) is str
        for item in manifest_copy
    ):
        manifest_copy.sort(key=lambda item: item["evidence_ref"])
    document = {
        "object": BLUEPRINT_OBJECT,
        "schema_version": BLUEPRINT_SCHEMA_VERSION,
        "schema_sha256": blueprint_schema_sha256_for_semantic(semantic_copy),
        "project_id": project_id,
        "revision": revision,
        "parent": _detached_json(parent, path="/parent") if parent is not None else None,
        "created_by_operation_id": created_by_operation_id,
        "status": BLUEPRINT_STATUS,
        "semantic_sha256": canonical_sha256(semantic_copy),
        "semantic": semantic_copy,
        "lineage": {
            "compiler": BLUEPRINT_COMPILER,
            "source_candidate_sha256": source_candidate_sha256,
            "initial_legacy_brief": _detached_json(
                initial_legacy_brief,
                path="/lineage/initial_legacy_brief",
            ),
        },
        "evidence_manifest": manifest_copy,
        "authority": _authority_for(semantic_copy),
    }
    require_valid_blueprint_document(document)
    return document


def blueprint_content_sha256(document: Mapping[str, Any]) -> str:
    require_valid_blueprint_document(document)
    return canonical_sha256(document)


def diff_blueprint_sections(
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
) -> list[dict[str, str | None]]:
    """Return a stable typed diff over the twelve semantic top-level sections."""

    if before is not None:
        require_valid_blueprint_semantic(before)
    require_valid_blueprint_semantic(after)
    changes: list[dict[str, str | None]] = []
    for section in SEMANTIC_SECTIONS:
        before_digest = canonical_sha256(before[section]) if before is not None else None
        after_digest = canonical_sha256(after[section])
        if before_digest == after_digest:
            continue
        changes.append(
            {
                "section": section,
                "change": "added" if before is None else "modified",
                "before_sha256": before_digest,
                "after_sha256": after_digest,
            }
        )
    return changes


def blueprint_contract_summary() -> dict[str, Any]:
    return {
        "object": BLUEPRINT_OBJECT,
        "schema_version": BLUEPRINT_SCHEMA_VERSION,
        "schema_available": BLUEPRINT_SCHEMA_AVAILABLE,
        "schema_sha256": BLUEPRINT_SCHEMA_SHA256,
        "schema_artifact": "creative-blueprint-v1.schema.json",
        "residual_schema_available": BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE,
        "residual_schema_sha256": BLUEPRINT_RESIDUAL_SCHEMA_SHA256,
        "residual_schema_artifact": "creative-blueprint-residual-v1.schema.json",
        "status": BLUEPRINT_STATUS,
        "authority_state": BLUEPRINT_AUTHORITY_STATE,
        "decision_units": list(DECISION_UNITS),
    }


__all__ = [
    "BLUEPRINT_AUTHORITY_CLAIM",
    "BLUEPRINT_AUTHORITY_STATE",
    "BLUEPRINT_COMPILER",
    "BLUEPRINT_MAX_ERRORS",
    "BLUEPRINT_OBJECT",
    "BLUEPRINT_SCHEMA",
    "BLUEPRINT_SCHEMA_AVAILABLE",
    "BLUEPRINT_SCHEMA_SHA256",
    "BLUEPRINT_SCHEMA_V1_EXPECTED_SHA256",
    "BLUEPRINT_RESIDUAL_SCHEMA",
    "BLUEPRINT_RESIDUAL_SCHEMA_AVAILABLE",
    "BLUEPRINT_RESIDUAL_SCHEMA_SHA256",
    "BLUEPRINT_RESIDUAL_SCHEMA_V1_EXPECTED_SHA256",
    "BLUEPRINT_SCHEMA_VERSION",
    "BLUEPRINT_STATUS",
    "DECISION_UNITS",
    "SEMANTIC_SECTIONS",
    "BlueprintContractError",
    "BlueprintContractIssue",
    "blueprint_content_sha256",
    "blueprint_contract_summary",
    "blueprint_decision_unit_digests",
    "blueprint_decision_unit_payloads",
    "blueprint_evidence_refs",
    "blueprint_residual_proofs",
    "blueprint_schema_sha256_for_semantic",
    "blueprint_semantic_sha256",
    "canonical_json",
    "canonical_sha256",
    "compile_blueprint_document",
    "diff_blueprint_sections",
    "require_valid_blueprint_document",
    "require_valid_blueprint_semantic",
    "validate_blueprint_document",
    "validate_blueprint_semantic",
]
