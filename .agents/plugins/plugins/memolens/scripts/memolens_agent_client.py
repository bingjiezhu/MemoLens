"""Authenticated loopback transport for short-lived paired Agent authority.

Canonical media preview deliberately has its own proof domains and streaming
transport.  Lease secrets stay in this Python process and media responses are
never converted into a whole-buffer JSON value or written to disk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import socket
from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import OpenerDirector, ProxyHandler, Request, build_opener

from memolens_api_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MAX_RESPONSE_BYTES,
    RejectRedirects,
    clamp_timeout,
    service_identity,
    validate_base_url,
)
from memolens_contracts import PLUGIN_VERSION, MemoLensError


PAIRING_ACTIONS = (
    "blueprint.commit_proposal",
    "blueprint.restore_revision",
    "timeline.apply_edit",
    "timeline.apply_structural_edit",
    "timeline.restore_revision",
    "timeline.preview_media",
)
DEFAULT_PAIRING_ACTIONS = PAIRING_ACTIONS[:2]
COMMAND_NONCE_ACTIONS = PAIRING_ACTIONS[:5]

TIMELINE_PREVIEW_ACTION = "timeline.preview_media"
TIMELINE_PREVIEW_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES = 64 * 1024 * 1024
TIMELINE_PREVIEW_MAX_REQUESTS = 64
TIMELINE_PREVIEW_MAX_CONCURRENCY = 2
TIMELINE_PREVIEW_MAX_SOURCE_BYTES = 9_223_372_036_854_775_807

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LEASE_ID = re.compile(r"^preview_[0-9a-f]{32}$")
_LEASE_SECRET = re.compile(r"^[0-9a-f]{64}$")
_CONTENT_LENGTH = re.compile(r"^(?:0|[1-9][0-9]{0,18})$")
_CONTENT_RANGE = re.compile(r"^bytes ([0-9]+)-([0-9]+)/([1-9][0-9]*)$")
_UNSATISFIED_CONTENT_RANGE = re.compile(r"^bytes \*/([1-9][0-9]*)$")
_REQUEST_RANGE = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")
_RANGE_NUMBER = re.compile(r"[0-9]+")
_ETAG = re.compile(r'^"[A-Za-z0-9._:-]{1,180}"$')


def _preview_utc(value: object) -> datetime | None:
    if type(value) is not str or not value.endswith("Z") or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    return parsed if parsed.tzinfo == timezone.utc else None


class AgentPreviewMediaResponse:
    """Context-managed, bounded upstream response used by the editor proxy."""

    def __init__(
        self,
        response: BinaryIO,
        *,
        status: int,
        headers: Mapping[str, str],
        method: str,
    ) -> None:
        self._response = response
        self.status = status
        self.headers = dict(headers)
        self.method = method
        self.content_length = int(self.headers["Content-Length"])
        self._remaining = 0 if method == "HEAD" else self.content_length
        self._closed = False

    def read(self, size: int = 64 * 1024) -> bytes:
        """Read at most one small chunk and never cross Content-Length."""

        if self._remaining <= 0:
            return b""
        if self._closed:
            raise MemoLensError(
                "MemoLens closed the Timeline preview stream before completion.",
                code="invalid_response",
            )
        if type(size) is not int or size < 1:
            raise ValueError("invalid_preview_stream_chunk_size")
        requested = min(size, self._remaining)
        raw = self._response.read(requested)
        if self._closed:
            raise MemoLensError(
                "MemoLens closed the Timeline preview stream during a read.",
                code="invalid_response",
            )
        if not isinstance(raw, bytes):
            self.close()
            raise MemoLensError(
                "MemoLens returned an invalid Timeline preview stream.",
                code="invalid_response",
            )
        if not raw:
            self.close()
            raise MemoLensError(
                "MemoLens truncated the Timeline preview stream.",
                code="invalid_response",
            )
        if len(raw) > requested:
            self.close()
            raise MemoLensError(
                "MemoLens exceeded the bounded Timeline preview stream chunk.",
                code="invalid_response",
            )
        self._remaining -= len(raw)
        return raw

    @property
    def complete(self) -> bool:
        return self._remaining == 0

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._response.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def __enter__(self) -> "AgentPreviewMediaResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class AgentApiError(MemoLensError):
    """Structured rejection from the authenticated Agent API.

    The status and bounded JSON payload are retained for the loopback editor
    process, but are never copied into browser state without a closed
    projection. Existing CLI callers can continue treating this as a normal
    :class:`MemoLensError`.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int,
        payload: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code)
        self.status = status
        self.payload = dict(payload or {})


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MemoLensError(
            "Agent request must be canonical JSON.", code="invalid_argument"
        ) from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _proof_secret_bytes(proof_secret: str) -> bytes:
    try:
        secret = bytes.fromhex(proof_secret)
    except (TypeError, ValueError) as exc:
        raise MemoLensError(
            "Paired Agent proof material is invalid.", code="agent_credential_invalid"
        ) from exc
    if len(secret) != 32:
        raise MemoLensError(
            "Paired Agent proof material is invalid.", code="agent_credential_invalid"
        )
    return secret


def status_proof(*, proof_secret: str, kind: str, identity: str) -> str:
    """Bind status and nonce discovery to possession of the pairing secret."""

    if kind not in {"pairing", "nonce"} or not identity:
        raise MemoLensError(
            "Agent status proof scope is invalid.", code="invalid_argument"
        )
    message = f"MLSTATUS1\n{kind}\n{identity}"
    return hmac.new(
        _proof_secret_bytes(proof_secret),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def operation_proof(
    *,
    proof_secret: str,
    capability_id: str,
    nonce: str,
    method: str,
    canonical_path: str,
    database_uuid: str,
    project_id: str,
    body: Mapping[str, Any],
    idempotency_key: str,
) -> str:
    """Create the frozen MLCAP1 request-bound proof without exposing the key."""

    secret = _proof_secret_bytes(proof_secret)
    message = "\n".join(
        (
            "MLCAP1",
            capability_id,
            nonce,
            method.upper(),
            canonical_path,
            database_uuid,
            project_id,
            canonical_sha256(dict(body)),
            idempotency_key,
        )
    )
    return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def timeline_preview_mint_proof(
    *,
    proof_secret: str,
    capability_id: str,
    method: str,
    canonical_path: str,
    database_uuid: str,
    body: Mapping[str, Any],
) -> str:
    """Create the frozen MLPREVIEWMINT1 proof, independent from MLCAP1."""

    message = (
        "MLPREVIEWMINT1\n"
        f"{capability_id}\n{method.upper()}\n{canonical_path}\n"
        f"{database_uuid}\n{canonical_sha256(dict(body))}"
    )
    return hmac.new(
        _proof_secret_bytes(proof_secret),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def timeline_preview_read_proof(
    *,
    lease_secret: str,
    lease_id: str,
    request_nonce: str,
    method: str,
    canonical_path: str,
    range_header: str | None,
) -> str:
    """Create the frozen MLPREVIEWREAD1 proof for one server-to-server read."""

    normalized_range = "" if range_header is None else range_header.strip()
    message = (
        "MLPREVIEWREAD1\n"
        f"{lease_id}\n{request_nonce}\n{method.upper()}\n{canonical_path}\n"
        f"{normalized_range}"
    )
    return hmac.new(
        _proof_secret_bytes(lease_secret),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _closed_preview_lease(
    value: object,
    *,
    project_id: str,
    observed_head: Mapping[str, Any],
) -> dict[str, Any]:
    fields = {
        "object",
        "schema_version",
        "lease_id",
        "lease_secret",
        "project_id",
        "observed_head",
        "issued_at",
        "expires_at",
        "budgets",
        "clips",
    }
    if type(value) is not dict or set(value) != fields:
        raise MemoLensError(
            "MemoLens returned an invalid Timeline preview lease.",
            code="invalid_response",
        )
    lease_id = value.get("lease_id")
    lease_secret = value.get("lease_secret")
    head = value.get("observed_head")
    budgets = value.get("budgets")
    clips = value.get("clips")
    issued_at = _preview_utc(value.get("issued_at"))
    expires_at = _preview_utc(value.get("expires_at"))
    now = datetime.now(timezone.utc)
    valid_head = (
        type(head) is dict
        and set(head) == {"revision", "content_sha256"}
        and type(head.get("revision")) is int
        and 1 <= head["revision"] <= 1_800_000
        and type(head.get("content_sha256")) is str
        and _SHA256.fullmatch(head["content_sha256"]) is not None
        and head == dict(observed_head)
    )
    valid_budget = budgets == {
        "max_requests": TIMELINE_PREVIEW_MAX_REQUESTS,
        "max_response_bytes": TIMELINE_PREVIEW_MAX_RESPONSE_BYTES,
        "max_aggregate_bytes": TIMELINE_PREVIEW_MAX_AGGREGATE_BYTES,
        "max_concurrency": TIMELINE_PREVIEW_MAX_CONCURRENCY,
    }
    if (
        value.get("object") != "memolens.timeline_preview_lease"
        or value.get("schema_version") != "1"
        or type(lease_id) is not str
        or _LEASE_ID.fullmatch(lease_id) is None
        or type(lease_secret) is not str
        or _LEASE_SECRET.fullmatch(lease_secret) is None
        or value.get("project_id") != project_id
        or not valid_head
        or issued_at is None
        or expires_at is None
        or not issued_at < expires_at <= issued_at + timedelta(seconds=90)
        or issued_at < now - timedelta(minutes=2)
        or issued_at > now + timedelta(seconds=30)
        or expires_at <= now - timedelta(seconds=1)
        or not valid_budget
        or type(clips) is not list
        or len(clips) > 256
    ):
        raise MemoLensError(
            "MemoLens returned an invalid Timeline preview lease.",
            code="invalid_response",
        )
    clip_fields = {
        "clip_id",
        "timeline_start_ms",
        "timeline_end_ms",
        "source_in_ms",
        "source_out_ms",
        "status",
        "reason_code",
    }
    seen: set[str] = set()
    for clip in clips:
        if type(clip) is not dict or set(clip) != clip_fields:
            raise MemoLensError(
                "MemoLens returned an invalid Timeline preview lease.",
                code="invalid_response",
            )
        clip_id = clip.get("clip_id")
        times = (
            clip.get("timeline_start_ms"),
            clip.get("timeline_end_ms"),
            clip.get("source_in_ms"),
            clip.get("source_out_ms"),
        )
        status = clip.get("status")
        reason = clip.get("reason_code")
        if (
            type(clip_id) is not str
            or _IDENTIFIER.fullmatch(clip_id) is None
            or clip_id in seen
            or any(type(item) is not int or item < 0 for item in times)
            or clip["timeline_end_ms"] <= clip["timeline_start_ms"]
            or clip["source_out_ms"] <= clip["source_in_ms"]
            or clip["timeline_end_ms"] - clip["timeline_start_ms"]
            != clip["source_out_ms"] - clip["source_in_ms"]
            or status not in {"playable", "unsupported"}
            or (
                (status == "playable" and reason is not None)
                or (
                    status == "unsupported"
                    and reason != "agent_preview_media_unsupported"
                )
            )
        ):
            raise MemoLensError(
                "MemoLens returned an invalid Timeline preview lease.",
                code="invalid_response",
            )
        seen.add(clip_id)
    return dict(value)


def _single_response_header(response: object, name: str) -> str | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    values = headers.get_all(name)
    if values is None:
        return None
    if len(values) != 1 or type(values[0]) is not str:
        raise MemoLensError(
            "MemoLens returned ambiguous Timeline preview headers.",
            code="invalid_response",
        )
    return values[0].strip()


def _closed_preview_media_headers(
    response: object,
    *,
    status: int,
    method: str,
    range_header: str | None,
) -> dict[str, str]:
    content_type = _single_response_header(response, "Content-Type")
    content_length = _single_response_header(response, "Content-Length")
    accept_ranges = _single_response_header(response, "Accept-Ranges")
    content_range = _single_response_header(response, "Content-Range")
    etag = _single_response_header(response, "ETag")
    cache_control = _single_response_header(response, "Cache-Control")
    pragma = _single_response_header(response, "Pragma")
    content_options = _single_response_header(
        response,
        "X-Content-Type-Options",
    )
    location = _single_response_header(response, "Location")
    disposition = _single_response_header(response, "Content-Disposition")
    if location is not None or disposition is not None:
        raise MemoLensError(
            "MemoLens returned an unsafe Timeline preview response.",
            code="invalid_response",
        )
    if (
        content_type is None
        or content_type.split(";", 1)[0].strip().casefold() != "video/mp4"
        or content_length is None
        or _CONTENT_LENGTH.fullmatch(content_length) is None
        or accept_ranges != "bytes"
        or etag is None
        or _ETAG.fullmatch(etag) is None
        or cache_control is None
        or {token.strip().casefold() for token in cache_control.split(",")}
        != {"private", "no-store"}
        or pragma is None
        or pragma.casefold() != "no-cache"
        or content_options is None
        or content_options.casefold() != "nosniff"
    ):
        raise MemoLensError(
            "MemoLens returned invalid Timeline preview headers.",
            code="invalid_response",
        )
    length = int(content_length)
    if length > TIMELINE_PREVIEW_MAX_SOURCE_BYTES:
        raise MemoLensError(
            "MemoLens returned an invalid Timeline preview length.",
            code="invalid_response",
        )
    if status == 416:
        unsatisfied = (
            None
            if content_range is None
            else _UNSATISFIED_CONTENT_RANGE.fullmatch(content_range)
        )
        if (
            length != 0
            or unsatisfied is None
            or len(unsatisfied.group(1)) > 19
            or int(unsatisfied.group(1)) > TIMELINE_PREVIEW_MAX_SOURCE_BYTES
        ):
            raise MemoLensError(
                "MemoLens returned an invalid Timeline preview range rejection.",
                code="invalid_response",
            )
    elif status == 200:
        if (
            range_header is not None
            or content_range is not None
            or length < 1
            or (
                method != "HEAD"
                and length > TIMELINE_PREVIEW_MAX_RESPONSE_BYTES
            )
        ):
            raise MemoLensError(
                "MemoLens returned an invalid full Timeline preview response.",
                code="invalid_response",
            )
    elif status == 206:
        match = (
            None
            if content_range is None
            else _CONTENT_RANGE.fullmatch(content_range)
        )
        requested = (
            None
            if range_header is None
            else _REQUEST_RANGE.fullmatch(range_header.strip())
        )
        if (
            match is None
            or requested is None
            or not any(requested.groups())
            or any(len(group) > 19 for group in match.groups())
            or any(len(group) > 19 for group in requested.groups())
        ):
            raise MemoLensError(
                "MemoLens returned an invalid partial Timeline preview response.",
                code="invalid_response",
            )
        start, end, source_size = (int(item) for item in match.groups())
        if (
            start > end
            or end >= source_size
            or source_size > TIMELINE_PREVIEW_MAX_SOURCE_BYTES
            or length != end - start + 1
            or not 1 <= length <= TIMELINE_PREVIEW_MAX_RESPONSE_BYTES
        ):
            raise MemoLensError(
                "MemoLens returned an invalid partial Timeline preview response.",
                code="invalid_response",
            )
        requested_start, requested_end = requested.groups()
        if requested_start:
            if start != int(requested_start) or (
                requested_end and end > int(requested_end)
            ):
                raise MemoLensError(
                    "MemoLens widened the requested Timeline preview range.",
                    code="invalid_response",
                )
        else:
            suffix = int(requested_end)
            if end != source_size - 1 or length > suffix:
                raise MemoLensError(
                    "MemoLens widened the requested Timeline preview suffix range.",
                    code="invalid_response",
                )
    else:
        raise MemoLensError(
            "MemoLens returned an unsupported Timeline preview status.",
            code="invalid_response",
        )
    return {
        "Content-Type": content_type,
        "Content-Length": content_length,
        "Accept-Ranges": accept_ranges,
        "ETag": etag,
        "Cache-Control": cache_control,
        "Pragma": pragma,
        **({"Content-Range": content_range} if content_range is not None else {}),
    }


def _raise_preview_http_error(exc: HTTPError) -> None:
    try:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
    finally:
        exc.close()
    if len(raw) <= MAX_RESPONSE_BYTES:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            code = payload.get("code")
            message = payload.get("message")
            if isinstance(error, dict):
                code = error.get("code", code)
                message = error.get("message", message)
            if isinstance(code, str) and isinstance(message, str):
                raise AgentApiError(
                    message,
                    code=code,
                    status=exc.code,
                    payload=payload,
                ) from exc
    raise AgentApiError(
        f"MemoLens rejected the Timeline preview request with HTTP {exc.code}.",
        code="http_error",
        status=exc.code,
    ) from exc


class AgentApiClient:
    """No-proxy, no-redirect loopback client for the explicit pairing protocol."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.base_url = validate_base_url(
            base_url or os.getenv("MEMOLENS_BASE_URL", DEFAULT_BASE_URL),
            resolver=socket.getaddrinfo,
        )
        self.timeout = clamp_timeout(timeout)
        self.opener = opener or build_opener(ProxyHandler({}), RejectRedirects())
        self._identity_checked = False

    def health(self) -> dict[str, Any]:
        payload = self._request_json(
            "/healthz",
            method="GET",
            body=None,
            headers=None,
            verify_identity=False,
        )
        if not service_identity(payload):
            raise MemoLensError(
                "The loopback service does not match the MemoLens health contract.",
                code="service_identity_mismatch",
            )
        self._identity_checked = True
        return payload

    def _request_json(
        self,
        path: str,
        *,
        method: str,
        body: Mapping[str, Any] | None,
        headers: Mapping[str, str] | None = None,
        verify_identity: bool = True,
    ) -> dict[str, Any]:
        if verify_identity and not self._identity_checked:
            self.health()
        encoded = canonical_json(dict(body)).encode("utf-8") if body is not None else None
        request_headers = {
            "Accept": "application/json",
            "User-Agent": f"MemoLens-Agent-CLI/{PLUGIN_VERSION}",
        }
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
        if headers is not None:
            request_headers.update(headers)
        request = Request(
            f"{self.base_url}{path}",
            data=encoded,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except MemoLensError:
            raise
        except HTTPError as exc:
            raw = exc.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) <= MAX_RESPONSE_BYTES:
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    error = payload.get("error")
                    code = payload.get("code")
                    message = payload.get("message")
                    if isinstance(error, dict):
                        code = error.get("code", code)
                        message = error.get("message", message)
                    if isinstance(code, str) and isinstance(message, str):
                        raise AgentApiError(
                            message,
                            code=code,
                            status=exc.code,
                            payload=payload,
                        ) from exc
            raise AgentApiError(
                f"MemoLens rejected the paired Agent request with HTTP {exc.code}.",
                code="http_error",
                status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MemoLensError(
                "The local MemoLens pairing service is unavailable.",
                code="service_unavailable",
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoLensError(
                "MemoLens pairing response exceeded the local safety limit.",
                code="response_too_large",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MemoLensError(
                "MemoLens returned a non-JSON pairing response.",
                code="invalid_response",
            ) from exc
        if not isinstance(payload, dict):
            raise MemoLensError(
                "MemoLens returned an unexpected pairing response.",
                code="invalid_response",
            )
        return payload

    def media_thumbnail(
        self,
        *,
        media_kind: str,
        asset_id: str,
        span_id: str | None = None,
    ) -> bytes:
        """Read one bounded, verified loopback thumbnail without a file locator.

        The canonical editor supplies only identities that it already admitted
        from the current Timeline snapshot.  The backend remains responsible
        for reopening and verifying the corresponding source or keyframe; this
        client never accepts a URL or filesystem path from the browser.
        """

        identifier = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
        if identifier.fullmatch(asset_id) is None:
            raise MemoLensError(
                "Media thumbnail identity is invalid.", code="invalid_argument"
            )
        if media_kind == "image":
            path = f"/v1/assets/{quote(asset_id, safe='')}/thumbnail"
        elif (
            media_kind == "video"
            and isinstance(span_id, str)
            and identifier.fullmatch(span_id) is not None
        ):
            path = f"/v1/video-segments/{quote(span_id, safe='')}/thumbnail"
        else:
            raise MemoLensError(
                "Media thumbnail identity is invalid.", code="invalid_argument"
            )
        if not self._identity_checked:
            self.health()
        request = Request(
            f"{self.base_url}{path}",
            headers={
                "Accept": "image/jpeg",
                "User-Agent": f"MemoLens-Agent-CLI/{PLUGIN_VERSION}",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise AgentApiError(
                "The verified media thumbnail is unavailable.",
                code="media_thumbnail_unavailable",
                status=status,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MemoLensError(
                "The local MemoLens thumbnail service is unavailable.",
                code="service_unavailable",
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoLensError(
                "The verified media thumbnail exceeded the local safety limit.",
                code="response_too_large",
            )
        if not raw or content_type.split(";", 1)[0].strip().casefold() != "image/jpeg":
            raise MemoLensError(
                "MemoLens returned an invalid media thumbnail.",
                code="invalid_response",
            )
        return raw

    def create_pairing(
        self,
        *,
        project_id: str,
        observed_head: Mapping[str, Any],
        subject_id: str,
        claimed_client_label: str,
        proof_secret: str,
        actions: list[str],
        ttl_seconds: int,
        max_operations: int,
    ) -> dict[str, Any]:
        return self._request_json(
            "/v1/agent/pairings",
            method="POST",
            body={
                "object": "memolens.agent_pairing_request",
                "schema_version": "1",
                "project_id": project_id,
                "observed_head": dict(observed_head),
                "subject_id": subject_id,
                "claimed_client_label": claimed_client_label,
                "proof_secret": proof_secret,
                "actions": actions,
                "ttl_seconds": ttl_seconds,
                "max_operations": max_operations,
            },
        )

    def pairing_status(
        self, pairing_id: str, *, proof_secret: str
    ) -> dict[str, Any]:
        return self._request_json(
            f"/v1/agent/pairings/{pairing_id}",
            method="GET",
            body=None,
            headers={
                "X-MemoLens-Agent-Status-Proof": status_proof(
                    proof_secret=proof_secret,
                    kind="pairing",
                    identity=pairing_id,
                )
            },
        )

    def request_nonce(
        self, capability_id: str, *, action: str, proof_secret: str
    ) -> dict[str, Any]:
        if action not in COMMAND_NONCE_ACTIONS:
            raise MemoLensError(
                "Agent nonce action is invalid.", code="invalid_argument"
            )
        return self._request_json(
            f"/v1/agent/capabilities/{capability_id}/nonce",
            method="POST",
            body={
                "object": "memolens.agent_operation_nonce_request",
                "schema_version": "1",
                "action": action,
            },
            headers={
                "X-MemoLens-Agent-Status-Proof": status_proof(
                    proof_secret=proof_secret,
                    kind="nonce",
                    identity=f"{capability_id}:{action}",
                )
            },
        )

    def project_workspace(self, project_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/v1/creative/projects/{project_id}",
            method="GET",
            body=None,
        )

    def coverage_workspace(self, project_id: str) -> dict[str, Any]:
        return self._request_json(
            f"/v1/creative/projects/{project_id}/coverage",
            method="GET",
            body=None,
        )

    def timeline_workspace(
        self, project_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        if revision is not None and (
            type(revision) is not int or not 1 <= revision <= 1_000_000
        ):
            raise MemoLensError(
                "Canonical Timeline revision is invalid.", code="invalid_argument"
            )
        suffix = "" if revision is None else f"?revision={revision}"
        return self._request_json(
            f"/v1/creative/projects/{project_id}/timeline{suffix}",
            method="GET",
            body=None,
        )

    def mint_timeline_preview_lease(
        self,
        *,
        project_id: str,
        observed_head: Mapping[str, Any],
        credential: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Mint one exact-head preview lease without using a write nonce."""

        capability_id = credential.get("capability_id")
        database_uuid = credential.get("database_uuid")
        proof_secret = credential.get("proof_secret")
        actions = credential.get("actions")
        head = {
            "revision": observed_head.get("revision"),
            "content_sha256": observed_head.get("content_sha256"),
        }
        if (
            not all(
                isinstance(value, str) and value
                for value in (capability_id, database_uuid, proof_secret)
            )
            or not isinstance(actions, list)
            or TIMELINE_PREVIEW_ACTION not in actions
            or type(head["revision"]) is not int
            or not 1 <= head["revision"] <= 1_800_000
            or type(head["content_sha256"]) is not str
            or _SHA256.fullmatch(head["content_sha256"]) is None
            or type(project_id) is not str
            or _IDENTIFIER.fullmatch(project_id) is None
        ):
            raise MemoLensError(
                "The Agent pairing does not grant exact canonical media preview.",
                code="agent_pairing_scope_denied",
            )
        body = {
            "object": "memolens.timeline_preview_lease_request",
            "schema_version": "1",
            "project_id": project_id,
            "observed_head": head,
            "request_nonce": secrets.token_urlsafe(32),
        }
        canonical_path = (
            f"/v1/agent/creative/projects/{project_id}/timeline/"
            "preview-leases"
        )
        url_path = (
            f"/v1/agent/creative/projects/{quote(project_id, safe='')}/timeline/"
            "preview-leases"
        )
        proof = timeline_preview_mint_proof(
            proof_secret=proof_secret,
            capability_id=capability_id,
            method="POST",
            canonical_path=canonical_path,
            database_uuid=database_uuid,
            body=body,
        )
        payload = self._request_json(
            url_path,
            method="POST",
            body=body,
            headers={
                "X-MemoLens-Agent-Capability": capability_id,
                "X-MemoLens-Preview-Proof": proof,
            },
        )
        return _closed_preview_lease(
            payload,
            project_id=project_id,
            observed_head=head,
        )

    def timeline_preview_media(
        self,
        *,
        lease: Mapping[str, Any],
        clip_id: str,
        method: str,
        range_header: str | None,
    ) -> AgentPreviewMediaResponse:
        """Open one no-redirect response; callers must stream and close it."""

        normalized_method = method.upper()
        lease_id = lease.get("lease_id")
        lease_secret = lease.get("lease_secret")
        if (
            normalized_method not in {"GET", "HEAD"}
            or type(lease_id) is not str
            or _LEASE_ID.fullmatch(lease_id) is None
            or type(lease_secret) is not str
            or _LEASE_SECRET.fullmatch(lease_secret) is None
            or type(clip_id) is not str
            or _IDENTIFIER.fullmatch(clip_id) is None
            or (
                range_header is not None
                and (
                    type(range_header) is not str
                    or not range_header.strip()
                    or len(range_header.strip()) > 64
                    or any(
                        ord(character) < 0x20 or ord(character) > 0x7E
                        for character in range_header.strip()
                    )
                    or any(
                        len(group) > 19
                        for group in _RANGE_NUMBER.findall(range_header)
                    )
                )
            )
        ):
            raise MemoLensError(
                "Timeline preview media request is invalid.",
                code="invalid_argument",
            )
        if not self._identity_checked:
            self.health()
        canonical_path = (
            f"/v1/agent/preview-leases/{lease_id}/clips/{clip_id}/media"
        )
        url_path = (
            f"/v1/agent/preview-leases/{quote(lease_id, safe='')}/clips/"
            f"{quote(clip_id, safe='')}/media"
        )
        request_nonce = secrets.token_urlsafe(32)
        normalized_range = None if range_header is None else range_header.strip()
        proof = timeline_preview_read_proof(
            lease_secret=lease_secret,
            lease_id=lease_id,
            request_nonce=request_nonce,
            method=normalized_method,
            canonical_path=canonical_path,
            range_header=normalized_range,
        )
        request_headers = {
            "Accept": "video/mp4",
            "User-Agent": f"MemoLens-Agent-CLI/{PLUGIN_VERSION}",
            "X-MemoLens-Preview-Lease": lease_id,
            "X-MemoLens-Preview-Nonce": request_nonce,
            "X-MemoLens-Preview-Proof": proof,
        }
        if normalized_range is not None:
            request_headers["Range"] = normalized_range
        request = Request(
            f"{self.base_url}{url_path}",
            headers=request_headers,
            method=normalized_method,
        )
        response: BinaryIO
        status: int
        try:
            opened = self.opener.open(request, timeout=self.timeout)
            response = opened
            status = int(opened.status)
        except HTTPError as exc:
            if exc.code != 416:
                _raise_preview_http_error(exc)
            response = exc
            status = 416
        except (URLError, TimeoutError, OSError) as exc:
            raise MemoLensError(
                "The local MemoLens Timeline preview service is unavailable.",
                code="service_unavailable",
            ) from exc
        try:
            if (
                getattr(response, "geturl", lambda: "")()
                != f"{self.base_url}{url_path}"
            ):
                raise MemoLensError(
                    "MemoLens redirected the Timeline preview request.",
                    code="invalid_response",
                )
            headers = _closed_preview_media_headers(
                response,
                status=status,
                method=normalized_method,
                range_header=normalized_range,
            )
            return AgentPreviewMediaResponse(
                response,
                status=status,
                headers=headers,
                method=normalized_method,
            )
        except Exception:
            response.close()
            raise

    def blueprint_write(
        self,
        *,
        command: str,
        project_id: str,
        body: Mapping[str, Any],
        idempotency_key: str,
        credential: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability_id = credential.get("capability_id")
        database_uuid = credential.get("database_uuid")
        proof_secret = credential.get("proof_secret")
        if not all(isinstance(value, str) and value for value in (
            capability_id,
            database_uuid,
            proof_secret,
        )):
            raise MemoLensError(
                "The Agent pairing has not been approved or is incomplete.",
                code="agent_pairing_required",
            )
        if command not in {"commit", "restore"}:
            raise MemoLensError("Unsupported Agent write command.", code="invalid_argument")
        action = (
            "blueprint.commit_proposal"
            if command == "commit"
            else "blueprint.restore_revision"
        )
        path = f"/v1/agent/creative/projects/{project_id}/blueprint/{command}"
        nonce_response = self.request_nonce(
            capability_id,
            action=action,
            proof_secret=proof_secret,
        )
        nonce = nonce_response.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise MemoLensError(
                "MemoLens did not return a valid one-time operation nonce.",
                code="invalid_response",
            )
        proof = operation_proof(
            proof_secret=proof_secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=database_uuid,
            project_id=project_id,
            body=body,
            idempotency_key=idempotency_key,
        )
        return self._request_json(
            path,
            method="POST",
            body=body,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-MemoLens-Agent-Capability": capability_id,
                "X-MemoLens-Agent-Nonce": nonce,
                "X-MemoLens-Agent-Proof": proof,
            },
        )

    def timeline_write(
        self,
        *,
        command: str = "edit",
        project_id: str,
        body: Mapping[str, Any],
        idempotency_key: str,
        credential: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability_id = credential.get("capability_id")
        database_uuid = credential.get("database_uuid")
        proof_secret = credential.get("proof_secret")
        if not all(
            isinstance(value, str) and value
            for value in (capability_id, database_uuid, proof_secret)
        ):
            raise MemoLensError(
                "The Agent pairing has not been approved or is incomplete.",
                code="agent_pairing_required",
            )
        command_contract = {
            "edit": (
                "timeline.apply_edit",
                f"/v1/agent/creative/projects/{project_id}/timeline/edit",
            ),
            "structural_edit": (
                "timeline.apply_structural_edit",
                f"/v1/agent/creative/projects/{project_id}/timeline/structural-edit",
            ),
            "restore": (
                "timeline.restore_revision",
                f"/v1/agent/creative/projects/{project_id}/timeline/restore",
            ),
        }
        if command not in command_contract:
            raise MemoLensError(
                "Unsupported Agent Timeline write command.",
                code="invalid_argument",
            )
        action, path = command_contract[command]
        nonce_response = self.request_nonce(
            capability_id,
            action=action,
            proof_secret=proof_secret,
        )
        nonce = nonce_response.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise MemoLensError(
                "MemoLens did not return a valid one-time operation nonce.",
                code="invalid_response",
            )
        proof = operation_proof(
            proof_secret=proof_secret,
            capability_id=capability_id,
            nonce=nonce,
            method="POST",
            canonical_path=path,
            database_uuid=database_uuid,
            project_id=project_id,
            body=body,
            idempotency_key=idempotency_key,
        )
        return self._request_json(
            path,
            method="POST",
            body=body,
            headers={
                "Idempotency-Key": idempotency_key,
                "X-MemoLens-Agent-Capability": capability_id,
                "X-MemoLens-Agent-Nonce": nonce,
                "X-MemoLens-Agent-Proof": proof,
            },
        )


__all__ = [
    "AgentApiClient",
    "AgentApiError",
    "AgentPreviewMediaResponse",
    "COMMAND_NONCE_ACTIONS",
    "DEFAULT_PAIRING_ACTIONS",
    "PAIRING_ACTIONS",
    "TIMELINE_PREVIEW_ACTION",
    "canonical_json",
    "canonical_sha256",
    "operation_proof",
    "status_proof",
    "timeline_preview_mint_proof",
    "timeline_preview_read_proof",
]
