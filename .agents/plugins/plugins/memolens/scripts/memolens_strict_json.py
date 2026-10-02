"""Fail-closed JSON decoding and in-memory value validation.

This module is deliberately independent from MemoLens transport and domain
contracts.  It gives every boundary the same JSON grammar, resource limits,
and non-reflective error shape without performing file, database, or network
I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class StrictJsonLimits:
    """Resource ceilings applied to decoded and direct Python JSON values."""

    max_bytes: int = 1_048_576
    max_depth: int = 32
    max_nodes: int = 50_000
    max_object_items: int = 2_000
    max_array_items: int = 10_000
    max_string_chars: int = 262_144
    max_integer_bits: int = 4_096

    def __post_init__(self) -> None:
        values = (
            self.max_bytes,
            self.max_depth,
            self.max_nodes,
            self.max_object_items,
            self.max_array_items,
            self.max_string_chars,
            self.max_integer_bits,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("Strict JSON limits must be positive integers.")


class StrictJsonError(ValueError):
    """A stable, non-reflective strict JSON failure."""

    __slots__ = ("code", "path", "message")

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.message = message

    def as_dict(self) -> dict[str, str]:
        """Return the complete public error contract."""

        return {"code": self.code, "path": self.path, "message": self.message}


_MESSAGES = {
    "cyclic_reference": "JSON data must not contain cyclic references.",
    "duplicate_key": "JSON objects must not contain duplicate keys.",
    "input_too_large": "JSON input exceeds the byte limit.",
    "integer_too_large": "A JSON integer exceeds the numeric size limit.",
    "invalid_input_type": "JSON input must be UTF-8 text or bytes.",
    "invalid_json": "Input must be valid JSON.",
    "invalid_utf8": "JSON input must be valid UTF-8.",
    "max_array_items_exceeded": "A JSON array exceeds the item limit.",
    "max_depth_exceeded": "JSON data exceeds the nesting depth limit.",
    "max_nodes_exceeded": "JSON data exceeds the node limit.",
    "max_object_items_exceeded": "A JSON object exceeds the member limit.",
    "max_string_length_exceeded": "A JSON string exceeds the character limit.",
    "non_finite_number": "JSON numbers must be finite.",
    "non_string_key": "JSON object keys must be strings.",
    "root_not_object": "JSON root must be an object.",
    "unsupported_type": "Data contains a value that JSON does not support.",
}


def _error(code: str, path: str = "$") -> StrictJsonError:
    return StrictJsonError(code, path, _MESSAGES[code])


def _container_depth_precheck(text: str, limit: int) -> None:
    """Bound parser recursion without trying to replace JSON grammar parsing."""

    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > limit:
                raise _error("max_depth_exceeded")
        elif character in "]}" and depth:
            depth -= 1


def _decode_text(payload: Any, limits: StrictJsonLimits) -> str:
    if type(payload) is str:
        if len(payload) > limits.max_bytes:
            raise _error("input_too_large")
        try:
            encoded_size = len(payload.encode("utf-8"))
        except UnicodeEncodeError:
            raise _error("invalid_utf8") from None
        if encoded_size > limits.max_bytes:
            raise _error("input_too_large")
        return payload

    if type(payload) in {bytes, bytearray}:
        if len(payload) > limits.max_bytes:
            raise _error("input_too_large")
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            raise _error("invalid_utf8") from None

    raise _error("invalid_input_type")


def _json_loader(text: str, limits: StrictJsonLimits) -> Any:
    def reject_constant(_token: str) -> Any:
        raise _error("non_finite_number")

    def parse_float(token: str) -> float:
        value = float(token)
        if not math.isfinite(value):
            raise _error("non_finite_number")
        return value

    def parse_int(token: str) -> int:
        # Avoid interpreter-dependent decimal conversion limits and prevent a
        # direct or textual integer from becoming an unbounded canonicalization
        # workload later in the domain validator.
        digits = token.removeprefix("-")
        if len(digits) > 1_234:
            raise _error("integer_too_large")
        value = int(token)
        if value.bit_length() > limits.max_integer_bits:
            raise _error("integer_too_large")
        return value

    def object_from_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        if len(pairs) > limits.max_object_items:
            raise _error("max_object_items_exceeded")
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _error("duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=object_from_pairs,
            parse_constant=reject_constant,
            parse_float=parse_float,
            parse_int=parse_int,
        )
    except StrictJsonError:
        raise
    except RecursionError:
        raise _error("max_depth_exceeded") from None
    except (json.JSONDecodeError, ValueError, OverflowError):
        raise _error("invalid_json") from None


def _child_object_path(path: str) -> str:
    # Field names may contain prompts, locators, or secrets.  A structural
    # marker keeps diagnostics useful without reflecting an untrusted key.
    return f"{path}.*"


def _validate_string(value: str, *, path: str, limits: StrictJsonLimits) -> None:
    if len(value) > limits.max_string_chars:
        raise _error("max_string_length_exceeded", path)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        # JSON escape sequences can decode into isolated UTF-16 surrogates even
        # when the source bytes themselves were valid UTF-8.
        raise _error("invalid_utf8", path) from None


def _validate_value(value: Any, limits: StrictJsonLimits) -> None:
    active_containers: set[int] = set()
    node_count = 0
    encoded_count = 0

    def charge(size: int, *, path: str) -> None:
        nonlocal encoded_count
        encoded_count += size
        if encoded_count > limits.max_bytes:
            raise _error("input_too_large", path)

    def charge_string(value: str, *, path: str) -> None:
        # The value is already bounded and UTF-8-valid before this call.
        charge(
            len(json.dumps(value, ensure_ascii=False).encode("utf-8")),
            path=path,
        )

    def walk(candidate: Any, *, depth: int, path: str) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > limits.max_nodes:
            raise _error("max_nodes_exceeded", path)

        candidate_type = type(candidate)
        if candidate_type is str:
            _validate_string(candidate, path=path, limits=limits)
            charge_string(candidate, path=path)
            return
        if candidate is None:
            charge(4, path=path)
            return
        if candidate_type is bool:
            charge(4 if candidate else 5, path=path)
            return
        if candidate_type is int:
            if candidate.bit_length() > limits.max_integer_bits:
                raise _error("integer_too_large", path)
            charge(len(str(candidate).encode("ascii")), path=path)
            return
        if candidate_type is float:
            if not math.isfinite(candidate):
                raise _error("non_finite_number", path)
            charge(len(json.dumps(candidate).encode("ascii")), path=path)
            return
        if candidate_type not in {dict, list}:
            raise _error("unsupported_type", path)
        if depth > limits.max_depth:
            raise _error("max_depth_exceeded", path)

        identity = id(candidate)
        if identity in active_containers:
            raise _error("cyclic_reference", path)
        active_containers.add(identity)
        try:
            if candidate_type is dict:
                if len(candidate) > limits.max_object_items:
                    raise _error("max_object_items_exceeded", path)
                charge(2 + max(0, len(candidate) - 1) + len(candidate), path=path)
                keys = list(candidate)
                if any(type(key) is not str for key in keys):
                    raise _error("non_string_key", path)
                child_path = _child_object_path(path)
                for key in keys:
                    _validate_string(key, path=child_path, limits=limits)
                    charge_string(key, path=child_path)
                for key in sorted(keys):
                    walk(candidate[key], depth=depth + 1, path=child_path)
                return

            if len(candidate) > limits.max_array_items:
                raise _error("max_array_items_exceeded", path)
            charge(2 + max(0, len(candidate) - 1), path=path)
            for index, item in enumerate(candidate):
                walk(item, depth=depth + 1, path=f"{path}[{index}]")
        finally:
            active_containers.remove(identity)

    walk(value, depth=1, path="$")


def validate_json_value(
    value: Any,
    *,
    require_object: bool = True,
    limits: StrictJsonLimits | None = None,
) -> Any:
    """Validate an already-decoded Python value and return it unchanged.

    Only exact built-in JSON types are accepted.  This avoids invoking custom
    mapping, sequence, number, or string subclass behavior at a trust boundary.
    Shared subtrees are allowed; only active recursion cycles are rejected.
    """

    selected_limits = limits or StrictJsonLimits()
    if require_object and type(value) is not dict:
        raise _error("root_not_object")
    _validate_value(value, selected_limits)
    return value


def decode_strict_json(
    payload: str | bytes | bytearray,
    *,
    require_object: bool = True,
    limits: StrictJsonLimits | None = None,
) -> Any:
    """Decode strict JSON, apply resource limits, and return the decoded value."""

    selected_limits = limits or StrictJsonLimits()
    text = _decode_text(payload, selected_limits)
    _container_depth_precheck(text, selected_limits.max_depth)
    decoded = _json_loader(text, selected_limits)
    return validate_json_value(
        decoded,
        require_object=require_object,
        limits=selected_limits,
    )


__all__ = [
    "StrictJsonError",
    "StrictJsonLimits",
    "decode_strict_json",
    "validate_json_value",
]
