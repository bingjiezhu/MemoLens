from __future__ import annotations

import hmac
import os
from collections.abc import Collection
from ipaddress import ip_address

from flask import Flask, jsonify, request


DESKTOP_TOKEN_HEADER = "X-MemoLens-Desktop-Token"


def _configured_frontend_port() -> str:
    value = os.environ.get("MEMOLENS_FRONTEND_PORT", "5173").strip()
    if value.isdigit() and 1 <= int(value) <= 65535:
        return value
    return "5173"


def _trusted_cors_origins(frontend_port: str) -> frozenset[str]:
    return frozenset(
        {
            "null",
            "file://",
            f"http://127.0.0.1:{frontend_port}",
            f"http://localhost:{frontend_port}",
            f"http://[::1]:{frontend_port}",
        }
    )


TRUSTED_CORS_ORIGINS = _trusted_cors_origins(_configured_frontend_port())


def is_local_remote_addr(remote_addr: str | None) -> bool:
    """Return whether an address belongs to the loopback interface.

    IPv4-mapped IPv6 addresses are accepted because Flask may expose local
    connections in either representation, depending on the host platform.
    """

    normalized = str(remote_addr or "").strip()
    if not normalized:
        return False
    try:
        address = ip_address(normalized)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    ipv4_mapped = getattr(address, "ipv4_mapped", None)
    return bool(ipv4_mapped and ipv4_mapped.is_loopback)


def resolve_allowed_origin(
    origin: str | None,
    trusted_origins: Collection[str] = TRUSTED_CORS_ORIGINS,
) -> str | None:
    if not isinstance(origin, str):
        return None
    normalized = origin.strip()
    return normalized if normalized and normalized in trusted_origins else None


def _append_vary_header(response, value: str) -> None:
    existing = {
        item.strip().lower()
        for item in response.headers.get("Vary", "").split(",")
        if item.strip()
    }
    if value.lower() not in existing:
        response.headers.add("Vary", value)


def _desktop_token_authenticated(app: Flask) -> bool:
    expected = str(app.config.get("DESKTOP_SESSION_TOKEN") or "")
    supplied = request.headers.get(DESKTOP_TOKEN_HEADER, "")
    return bool(expected and supplied and hmac.compare_digest(supplied, expected))


def _permission_error(message: str):
    return jsonify({"object": "error", "type": "permission_error", "message": message}), 403


def install_local_api_security(
    app: Flask,
    *,
    trusted_origins: Collection[str] = TRUSTED_CORS_ORIGINS,
) -> None:
    """Install MemoLens' loopback, origin, CORS, and response-header policy."""

    @app.before_request
    def enforce_local_api_boundary():
        if not request.path.startswith("/v1/"):
            return None
        if not is_local_remote_addr(request.remote_addr):
            return _permission_error("MemoLens API is loopback-only.")

        # Native loopback clients deliberately have no browser Origin. Opaque
        # desktop renderer origins must also prove possession of the per-launch
        # token; trusted development origins remain restricted to loopback.
        raw_origin = request.headers.get("Origin")
        if raw_origin is None:
            return None
        allowed_origin = resolve_allowed_origin(raw_origin, trusted_origins)
        if allowed_origin is None:
            return _permission_error("Request origin is not trusted.")
        if allowed_origin not in {"null", "file://"} or request.method == "OPTIONS":
            return None
        if not _desktop_token_authenticated(app):
            return _permission_error("Desktop session authentication failed.")
        return None

    @app.after_request
    def add_cors_and_security_headers(response):
        request_origin = request.headers.get("Origin")
        allowed_origin = resolve_allowed_origin(request_origin, trusted_origins)
        if request_origin is not None:
            _append_vary_header(response, "Origin")
        if allowed_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Headers"] = (
                f"Content-Type, Authorization, Idempotency-Key, {DESKTOP_TOKEN_HEADER}"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"

        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response
