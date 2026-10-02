"""Fail-closed JSON transport parsing for state-changing HTTP commands."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Iterable

from flask import Request

from core.media_db import canonical_json


MAX_RAW_BYTES = 1_048_576
# Domain contracts apply their own semantic/document limits. The transport
# envelope must still admit a 512 KiB Blueprint semantic plus command metadata.
MAX_CANONICAL_BYTES = MAX_RAW_BYTES
MAX_DEPTH = 32
MAX_NODES = 50_000
MAX_OBJECT_MEMBERS = 64
MAX_ARRAY_ITEMS = 512
MAX_STRING_CHARACTERS = 12_000
MAX_INTEGER_BITS = 4_096
MAX_NUMBER_CHARACTERS = 4_096


@dataclass(frozen=True)
class StrictJsonRequestError(ValueError):
    code: str
    message: str

    def __str__(self) -> str:
        return self.message


def _error(code: str, message: str) -> StrictJsonRequestError:
    return StrictJsonRequestError(code=code, message=message)


def _pairs_object(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _error("duplicate_json_key", "Request JSON contains a duplicate object key.")
        value[key] = item
    return value


def _parse_constant(_value: str) -> None:
    raise _error("non_finite_json_number", "Request JSON contains a non-finite number.")


def _parse_integer(value: str) -> int:
    if len(value) > MAX_NUMBER_CHARACTERS:
        raise _error("json_number_too_large", "Request JSON contains an oversized number.")
    parsed = int(value)
    if parsed.bit_length() > MAX_INTEGER_BITS:
        raise _error("json_integer_too_large", "Request JSON contains an oversized integer.")
    return parsed


def _parse_float(value: str) -> float:
    if len(value) > MAX_NUMBER_CHARACTERS:
        raise _error("json_number_too_large", "Request JSON contains an oversized number.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise _error("non_finite_json_number", "Request JSON contains a non-finite number.")
    return parsed


def _precheck_container_depth(text: str) -> None:
    """Bound decoder recursion with the same depth model as the value walk."""

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
            if depth > MAX_DEPTH:
                raise _error("json_too_deep", "Request JSON exceeds the nesting limit.")
        elif character in "]}" and depth > 0:
            depth -= 1


def _walk_resource_limits(value: Any) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            raise _error("json_too_many_nodes", "Request JSON exceeds the node limit.")
        if depth > MAX_DEPTH:
            raise _error("json_too_deep", "Request JSON exceeds the nesting limit.")
        value_type = type(current)
        if value_type is dict:
            if len(current) > MAX_OBJECT_MEMBERS:
                raise _error("json_object_too_large", "Request JSON contains an oversized object.")
            for key, item in current.items():
                if type(key) is not str:
                    raise _error("invalid_json_type", "Request JSON contains an unsupported value type.")
                if len(key) > MAX_STRING_CHARACTERS:
                    raise _error("json_string_too_large", "Request JSON contains an oversized string.")
                if any(0xD800 <= ord(character) <= 0xDFFF for character in key):
                    raise _error("invalid_json_unicode", "Request JSON contains invalid Unicode.")
                stack.append((item, depth + 1))
        elif value_type is list:
            if len(current) > MAX_ARRAY_ITEMS:
                raise _error("json_array_too_large", "Request JSON contains an oversized array.")
            stack.extend((item, depth + 1) for item in reversed(current))
        elif value_type is str:
            if len(current) > MAX_STRING_CHARACTERS:
                raise _error("json_string_too_large", "Request JSON contains an oversized string.")
            if any(0xD800 <= ord(character) <= 0xDFFF for character in current):
                raise _error("invalid_json_unicode", "Request JSON contains invalid Unicode.")
        elif value_type is int:
            if current.bit_length() > MAX_INTEGER_BITS:
                raise _error("json_integer_too_large", "Request JSON contains an oversized integer.")
        elif value_type is float:
            if not math.isfinite(current):
                raise _error("non_finite_json_number", "Request JSON contains a non-finite number.")
        elif current is not None and value_type is not bool:
            raise _error("invalid_json_type", "Request JSON contains an unsupported value type.")


def strict_json_object(request: Request) -> dict[str, Any]:
    """Decode one bounded RFC JSON object without normalizing hostile syntax."""

    content_length = request.content_length
    if content_length is not None and content_length > MAX_RAW_BYTES:
        raise _error("json_transport_too_large", "Request JSON exceeds the transport size limit.")
    if not request.is_json:
        raise _error("json_content_type_required", "Request Content-Type must be application/json.")
    # The Content-Length header is optional (for example with chunked HTTP), so
    # bound the actual stream read instead of materializing an unbounded body.
    payload = request.stream.read(MAX_RAW_BYTES + 1)
    if len(payload) > MAX_RAW_BYTES:
        raise _error("json_transport_too_large", "Request JSON exceeds the transport size limit.")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _error("invalid_json_utf8", "Request JSON must be valid UTF-8.") from exc
    _precheck_container_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_object,
            parse_constant=_parse_constant,
            parse_int=_parse_integer,
            parse_float=_parse_float,
        )
    except StrictJsonRequestError:
        raise
    except (json.JSONDecodeError, ValueError, OverflowError, RecursionError) as exc:
        raise _error("invalid_json", "Request body must be one valid JSON object.") from exc
    if type(value) is not dict:
        raise _error("json_object_required", "Request body must be a JSON object.")
    _walk_resource_limits(value)
    try:
        canonical_bytes = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error("invalid_json", "Request body must be one valid JSON object.") from exc
    if len(canonical_bytes) > MAX_CANONICAL_BYTES:
        raise _error("json_semantic_too_large", "Request JSON exceeds the canonical size limit.")
    return value


__all__ = [
    "MAX_CANONICAL_BYTES",
    "MAX_RAW_BYTES",
    "StrictJsonRequestError",
    "strict_json_object",
]
