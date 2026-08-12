"""Loopback-only HTTP transport and service identity verification."""

from __future__ import annotations

import ipaddress
import json
import socket
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from memolens_contracts import PLUGIN_VERSION, MemoLensError


DEFAULT_BASE_URL = "http://127.0.0.1:5519"
DEFAULT_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

AddressResolver = Callable[..., list[tuple[Any, ...]]]


class RejectRedirects(HTTPRedirectHandler):
    """Fail closed instead of following even loopback HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise MemoLensError(
            "The local MemoLens service attempted an HTTP redirect; refusing it.",
            code="redirect_refused",
        )


def clamp_timeout(value: float) -> float:
    return max(0.1, min(float(value), 30.0))


def validate_base_url(
    raw_url: str, *, resolver: AddressResolver = socket.getaddrinfo
) -> str:
    """Validate a URL and prove every resolved address is loopback."""

    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise MemoLensError(
            "MemoLens base URL must use http or https.", code="unsafe_base_url"
        )
    if parsed.hostname is None or parsed.hostname.casefold() not in LOOPBACK_HOSTS:
        raise MemoLensError(
            "MemoLens base URL must target 127.0.0.1, ::1, or localhost.",
            code="unsafe_base_url",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MemoLensError(
            "MemoLens base URL cannot contain credentials, query text, or a fragment.",
            code="unsafe_base_url",
        )
    if parsed.path not in {"", "/"}:
        raise MemoLensError(
            "MemoLens base URL cannot contain a path.", code="unsafe_base_url"
        )
    try:
        resolved = {
            ipaddress.ip_address(item[4][0])
            for item in resolver(parsed.hostname, parsed.port or 5519)
        }
    except (OSError, ValueError) as exc:
        raise MemoLensError(
            "MemoLens loopback host could not be resolved safely.",
            code="unsafe_base_url",
        ) from exc
    if not resolved or not all(address.is_loopback for address in resolved):
        raise MemoLensError(
            "MemoLens host resolved outside the loopback interface.",
            code="unsafe_base_url",
        )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def service_identity(payload: Any) -> bool:
    """Match fields unique to the supported MemoLens health contract."""

    return bool(
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("object") == "health.check"
        and payload.get("service") == "memolens-backend"
        and payload.get("api_version") == "1"
    )


def validated_settings(payload: Any) -> tuple[dict[str, Any], Path, Path]:
    """Validate the settings contract and return authoritative local paths."""

    if not isinstance(payload, dict) or payload.get("object") != "memolens.settings":
        raise MemoLensError(
            "MemoLens settings returned an unexpected object type.",
            code="invalid_response",
        )
    effective = payload.get("effective")
    if not isinstance(effective, dict):
        raise MemoLensError(
            "MemoLens settings did not include effective settings.",
            code="invalid_response",
        )

    resolved: dict[str, Path] = {}
    for field in ("image_library_dir", "db_path"):
        raw_value = effective.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise MemoLensError(
                f"MemoLens settings field `{field}` must be a non-empty path.",
                code="invalid_response",
            )
        raw_path = Path(raw_value).expanduser()
        if not raw_path.is_absolute():
            raise MemoLensError(
                f"MemoLens settings field `{field}` must be an absolute path.",
                code="invalid_response",
            )
        try:
            resolved[field] = raw_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemoLensError(
                f"MemoLens settings field `{field}` could not be resolved safely.",
                code="invalid_response",
            ) from exc

    if not isinstance(effective.get("embedding_backend"), str):
        raise MemoLensError(
            "MemoLens settings did not identify the embedding backend.",
            code="invalid_response",
        )
    if not isinstance(payload.get("index_stats"), dict):
        raise MemoLensError(
            "MemoLens settings did not include index statistics.",
            code="invalid_response",
        )
    return effective, resolved["db_path"], resolved["image_library_dir"]


class LocalApiClient:
    """Bounded loopback transport with an identity-before-settings handshake."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        resolver: AddressResolver = socket.getaddrinfo,
        opener: OpenerDirector | None = None,
    ) -> None:
        self.base_url = validate_base_url(base_url, resolver=resolver)
        self.timeout = clamp_timeout(timeout)
        # An explicit empty proxy map prevents environment-derived proxy use.
        self.opener = opener or build_opener(ProxyHandler({}), RejectRedirects())
        self.health_cache: dict[str, Any] | None = None
        self.settings_cache: dict[str, Any] | None = None
        self.db_path: Path | None = None
        self.library_dir: Path | None = None

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        verify_identity: bool = True,
    ) -> dict[str, Any]:
        if verify_identity:
            self.health()
        return self._read_json(path, method=method, body=body)

    def _read_json(
        self,
        path: str,
        *,
        method: str,
        body: dict[str, Any] | None,
    ) -> dict[str, Any]:
        encoded_body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": f"MemoLens-Codex-Plugin/{PLUGIN_VERSION}",
        }
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}",
            data=encoded_body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except MemoLensError:
            raise
        except HTTPError as exc:
            raise MemoLensError(
                f"MemoLens returned HTTP {exc.code} for {path}.", code="http_error"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MemoLensError(
                "The local MemoLens service is unavailable.",
                code="service_unavailable",
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoLensError(
                "MemoLens response exceeded the local safety limit.",
                code="response_too_large",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MemoLensError(
                "MemoLens returned a non-JSON response.", code="invalid_response"
            ) from exc
        if not isinstance(payload, dict):
            raise MemoLensError(
                "MemoLens returned an unexpected JSON shape.",
                code="invalid_response",
            )
        return payload

    def health(self) -> dict[str, Any]:
        if self.health_cache is not None:
            return self.health_cache
        health = self._read_json("/healthz", method="GET", body=None)
        if not service_identity(health):
            raise MemoLensError(
                "The loopback service does not match the MemoLens health contract.",
                code="service_identity_mismatch",
            )
        settings = self._read_json("/v1/settings", method="GET", body=None)
        _, db_path, library_dir = validated_settings(settings)
        # Commit the pair atomically. Invalid settings never cache a valid health.
        self.settings_cache = settings
        self.db_path = db_path
        self.library_dir = library_dir
        self.health_cache = health
        return health
