"""Closed, path-free contracts for canonical Timeline source preview.

Preview authority is deliberately separate from Timeline edit/restore authority.
This module owns only deterministic request, scope, and byte-range validation;
runtime capability proof, exact source opening, budgets, and streaming stay with
the broker/repository/API layers.

The derived scope is server-only.  It contains opaque Core source identities so
the data plane can reopen the exact source, but it never contains a filesystem
path, backend URL, credential, or lease secret and must never be returned to the
editor browser.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from .timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_sha256,
    canonical_timeline_content_sha256,
    require_valid_canonical_timeline,
    require_valid_timeline_source_bindings,
    timeline_source_bindings_sha256,
)


TIMELINE_PREVIEW_ACTION = "timeline.preview_media"
TIMELINE_PREVIEW_LEASE_TTL_SECONDS = 90
TIMELINE_PREVIEW_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES = 64 * 1024 * 1024
TIMELINE_PREVIEW_MAX_REQUESTS = 64
TIMELINE_PREVIEW_MAX_CONCURRENCY = 2

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_NONCE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SINGLE_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_MAX_RANGE_HEADER_LENGTH = 128
_MAX_RANGE_NUMBER_DIGITS = 20

_REQUEST_FIELDS = {
    "object",
    "schema_version",
    "project_id",
    "observed_head",
    "request_nonce",
}


@dataclass(frozen=True)
class TimelinePreviewContractError(ValueError):
    code: str
    path: str
    message: str

    def __init__(self, code: str, path: str, message: str):
        object.__setattr__(self, "code", code)
        object.__setattr__(self, "path", path)
        object.__setattr__(self, "message", message)
        ValueError.__init__(self, message)


@dataclass(frozen=True)
class TimelinePreviewByteRange:
    start: int
    end: int
    length: int
    status_code: int


def _raise(code: str, path: str, message: str) -> None:
    raise TimelinePreviewContractError(code, path, message)


def require_timeline_preview_lease_request(value: object) -> dict[str, Any]:
    """Validate the only caller-controlled fields accepted by lease minting."""

    if type(value) is not dict or set(value) != _REQUEST_FIELDS:
        _raise(
            "agent_preview_scope_denied",
            "",
            "Timeline preview lease request has an unsupported shape.",
        )
    if (
        value.get("object") != "memolens.timeline_preview_lease_request"
        or value.get("schema_version") != "1"
    ):
        _raise(
            "agent_preview_scope_denied",
            "/object",
            "Timeline preview lease request identity is invalid.",
        )
    project_id = value.get("project_id")
    if type(project_id) is not str or _IDENTIFIER.fullmatch(project_id) is None:
        _raise(
            "agent_preview_scope_denied",
            "/project_id",
            "Timeline preview project identity is invalid.",
        )
    observed_head = value.get("observed_head")
    if not (
        type(observed_head) is dict
        and set(observed_head) == {"revision", "content_sha256"}
        and type(observed_head.get("revision")) is int
        and 1 <= observed_head["revision"] <= 1_800_000
        and type(observed_head.get("content_sha256")) is str
        and _SHA256.fullmatch(observed_head["content_sha256"]) is not None
    ):
        _raise(
            "agent_preview_scope_denied",
            "/observed_head",
            "Timeline preview head binding is invalid.",
        )
    request_nonce = value.get("request_nonce")
    if type(request_nonce) is not str or _REQUEST_NONCE.fullmatch(request_nonce) is None:
        _raise(
            "agent_preview_scope_denied",
            "/request_nonce",
            "Timeline preview request nonce is invalid.",
        )
    return deepcopy(value)


def derive_timeline_preview_scope(
    *,
    timeline: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
    observed_head: Mapping[str, object],
) -> dict[str, Any]:
    """Derive the exact server-held video clip whitelist from canonical state."""

    try:
        canonical_timeline = require_valid_canonical_timeline(timeline)
        canonical_sources = require_valid_timeline_source_bindings(
            source_bindings,
            timeline=canonical_timeline,
        )
    except TimelineLoweringContractError as exc:
        message = (
            exc.errors[0]["message"]
            if exc.errors
            else "Canonical Timeline preview source binding is invalid."
        )
        _raise("agent_preview_source_changed", "/timeline", message)

    exact_head = {
        "revision": canonical_timeline["revision"],
        "content_sha256": canonical_timeline_content_sha256(canonical_timeline),
    }
    if dict(observed_head) != exact_head:
        _raise(
            "agent_preview_head_changed",
            "/observed_head",
            "Canonical Timeline head changed before preview scope derivation.",
        )

    clips = canonical_timeline["tracks"][0]["clips"]
    grants: list[dict[str, Any]] = []
    for clip, binding in zip(clips, canonical_sources, strict=True):
        if clip["media_kind"] != "video":
            continue
        grants.append(
            {
                "clip_id": clip["clip_id"],
                "ordinal": clip["ordinal"],
                "asset_id": clip["asset_id"],
                "asset_sha256": clip["asset_sha256"],
                "asset_source_id": binding["asset_source_id"],
                "timeline_start_ms": clip["start_ms"],
                "timeline_end_ms": clip["end_ms"],
                "source_in_ms": clip["source_in_ms"],
                "source_out_ms": clip["source_out_ms"],
                "source_binding_sha256": canonical_sha256(binding),
            }
        )
    return {
        "object": "memolens.timeline_preview_scope",
        "schema_version": "1",
        "project_id": canonical_timeline["project_id"],
        "timeline_id": canonical_timeline["timeline_id"],
        "observed_head": exact_head,
        "source_bindings_sha256": timeline_source_bindings_sha256(
            canonical_sources,
            timeline=canonical_timeline,
        ),
        "clips": grants,
    }


def require_timeline_preview_range(
    range_header: str | None,
    *,
    source_size: int,
    max_response_bytes: int = TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
) -> TimelinePreviewByteRange:
    """Resolve one bounded HTTP byte range without silently widening it."""

    if (
        type(source_size) is not int
        or source_size < 1
        or type(max_response_bytes) is not int
        or max_response_bytes < 1
    ):
        _raise(
            "agent_preview_source_changed",
            "/source_size",
            "Timeline preview source size is invalid.",
        )
    if range_header is None:
        if source_size > max_response_bytes:
            _raise(
                "agent_preview_range_invalid",
                "/range",
                "A bounded byte range is required for this source.",
            )
        return TimelinePreviewByteRange(0, source_size - 1, source_size, 200)
    if type(range_header) is not str:
        _raise(
            "agent_preview_range_invalid",
            "/range",
            "Only one valid byte range is supported.",
        )
    normalized_range = range_header.strip()
    match = (
        _SINGLE_RANGE.fullmatch(normalized_range)
        if len(normalized_range) <= _MAX_RANGE_HEADER_LENGTH
        else None
    )
    if (
        match is None
        or (not match.group(1) and not match.group(2))
        or len(match.group(1)) > _MAX_RANGE_NUMBER_DIGITS
        or len(match.group(2)) > _MAX_RANGE_NUMBER_DIGITS
    ):
        _raise(
            "agent_preview_range_invalid",
            "/range",
            "Only one valid byte range is supported.",
        )
    try:
        start_number = int(match.group(1)) if match.group(1) else None
        end_number = int(match.group(2)) if match.group(2) else None
    except (TypeError, ValueError, OverflowError):
        _raise(
            "agent_preview_range_invalid",
            "/range",
            "Only one valid byte range is supported.",
        )
    if start_number is not None:
        start = start_number
        # Browsers commonly request ``bytes=N-``.  A 206 response may satisfy
        # only a bounded sub-range, so cap this open-ended form to one response
        # budget and let the client request the next self-described window.
        end = (
            end_number
            if end_number is not None
            else min(source_size - 1, start + max_response_bytes - 1)
        )
    else:
        assert end_number is not None
        suffix = end_number
        if suffix < 1:
            _raise(
                "agent_preview_range_invalid",
                "/range",
                "Byte-range suffix must be positive.",
            )
        start = max(0, source_size - suffix)
        end = source_size - 1
    if start >= source_size or end < start:
        _raise(
            "agent_preview_range_invalid",
            "/range",
            "Requested byte range is unavailable.",
        )
    end = min(end, source_size - 1)
    length = end - start + 1
    if length > max_response_bytes:
        _raise(
            "agent_preview_range_invalid",
            "/range",
            "Requested byte range exceeds the preview response budget.",
        )
    return TimelinePreviewByteRange(start, end, length, 206)


__all__ = [
    "TIMELINE_PREVIEW_ACTION",
    "TIMELINE_PREVIEW_LEASE_TTL_SECONDS",
    "TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES",
    "TIMELINE_PREVIEW_MAX_CONCURRENCY",
    "TIMELINE_PREVIEW_MAX_REQUESTS",
    "TIMELINE_PREVIEW_MAX_RESPONSE_BYTES",
    "TimelinePreviewByteRange",
    "TimelinePreviewContractError",
    "derive_timeline_preview_scope",
    "require_timeline_preview_lease_request",
    "require_timeline_preview_range",
]
