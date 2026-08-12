from __future__ import annotations

import hmac
import os
from ipaddress import ip_address

from flask import Flask, jsonify, request

from core.config import Settings
from core.db import ImageIndexRepository
from core.photo_atlas import PhotoAtlasService
from core.text_embeddings import TextEmbeddingService
from .retrieval import (
    OpenAICompatibleQueryPlanner,
    RetrievalCopywriter,
    RetrievalService,
)
from indexing.embeddings import EmbeddingService
from indexing.geocoder import ReverseGeocoder
from indexing.pipeline import IndexingService
from indexing.vision import OpenAICompatibleVisionClient


MEMOLENS_SERVICE_ID = "memolens-backend"
MEMOLENS_API_VERSION = "1"
DESKTOP_TOKEN_HEADER = "X-MemoLens-Desktop-Token"

# The packaged renderer has an opaque `null` origin because it is loaded from
# file://. Development uses one explicitly configured loopback port. Keep this
# list exact:
# localhost is a meaningful security boundary for a service that can read a
# user's photo library and mutate its SQLite index.
_configured_frontend_port = os.environ.get("MEMOLENS_FRONTEND_PORT", "5173").strip()
if not _configured_frontend_port.isdigit() or not 1 <= int(_configured_frontend_port) <= 65535:
    _configured_frontend_port = "5173"
TRUSTED_CORS_ORIGINS = {
    "null",
    "file://",
    f"http://127.0.0.1:{_configured_frontend_port}",
    f"http://localhost:{_configured_frontend_port}",
    f"http://[::1]:{_configured_frontend_port}",
}


def _resolve_allowed_origin(origin: str | None) -> str | None:
    if not isinstance(origin, str):
        return None

    normalized = origin.strip()
    if not normalized:
        return None
    return normalized if normalized in TRUSTED_CORS_ORIGINS else None


def _append_vary_header(response, value: str) -> None:
    existing = {
        item.strip().lower()
        for item in response.headers.get("Vary", "").split(",")
        if item.strip()
    }
    if value.lower() not in existing:
        response.headers.add("Vary", value)


def configure_runtime(app: Flask, settings: Settings) -> None:
    resolved_settings = settings
    resolved_settings.ensure_directories()
    app.config["SETTINGS"] = resolved_settings

    repository = ImageIndexRepository(resolved_settings.db_path)
    repository.ensure_schema()

    app.extensions["image_index_repository"] = repository
    app.extensions["photo_atlas_service"] = PhotoAtlasService(repository)
    app.extensions["vision_client"] = OpenAICompatibleVisionClient(resolved_settings)
    app.extensions["embedding_service"] = EmbeddingService(resolved_settings)
    app.extensions["text_embedding_service"] = TextEmbeddingService(resolved_settings)
    app.extensions["geocoder"] = ReverseGeocoder(resolved_settings)
    app.extensions["query_planner"] = OpenAICompatibleQueryPlanner(resolved_settings)
    app.extensions["retrieval_copywriter"] = RetrievalCopywriter(resolved_settings)
    app.extensions["indexing_service"] = IndexingService(
        settings=resolved_settings,
        repository=repository,
        vision_client=app.extensions["vision_client"],
        embedding_service=app.extensions["embedding_service"],
        text_embedding_service=app.extensions["text_embedding_service"],
        geocoder=app.extensions["geocoder"],
    )
    app.extensions["retrieval_service"] = RetrievalService(
        settings=resolved_settings,
        repository=repository,
        planner=app.extensions["query_planner"],
        text_embedding_service=app.extensions["text_embedding_service"],
    )


def reload_runtime(app: Flask) -> Settings:
    resolved_settings = Settings.from_env()
    configure_runtime(app, resolved_settings)
    return resolved_settings


def create_app(settings: Settings | None = None) -> Flask:
    from .api import api_blueprint

    app = Flask(__name__)
    app.config["DESKTOP_SESSION_TOKEN"] = os.environ.get(
        "MEMOLENS_DESKTOP_SESSION_TOKEN", ""
    ).strip()
    configure_runtime(app, settings or Settings.from_env())

    @app.before_request
    def enforce_local_api_boundary():
        if not request.path.startswith("/v1/"):
            return None

        remote_addr = str(request.remote_addr or "").strip()
        try:
            address = ip_address(remote_addr)
            ipv4_mapped = getattr(address, "ipv4_mapped", None)
            is_loopback = address.is_loopback or bool(
                ipv4_mapped and ipv4_mapped.is_loopback
            )
        except ValueError:
            is_loopback = False
        if not is_loopback:
            return jsonify({"object": "error", "type": "permission_error", "message": "MemoLens API is loopback-only."}), 403

        raw_origin = request.headers.get("Origin")
        expected = str(app.config.get("DESKTOP_SESSION_TOKEN") or "")
        supplied = request.headers.get(DESKTOP_TOKEN_HEADER, "")
        token_authenticated = bool(
            expected and supplied and hmac.compare_digest(supplied, expected)
        )

        # Native loopback callers (curl, the CLI, and the Photon bot) do not
        # send a browser Origin. Preserve that local API contract; the desktop
        # token protects opaque renderer origins rather than acting as a
        # general-purpose credential that independent local clients cannot
        # possess.
        if raw_origin is None:
            return None
        allowed_origin = _resolve_allowed_origin(raw_origin)
        if allowed_origin is None:
            return jsonify({"object": "error", "type": "permission_error", "message": "Request origin is not trusted."}), 403

        if allowed_origin in {"null", "file://"}:
            # Let browser preflight discover the token header; the following
            # actual request is still required to present it.
            if request.method == "OPTIONS":
                return None
            if not token_authenticated:
                return jsonify({"object": "error", "type": "permission_error", "message": "Desktop session authentication failed."}), 403
        return None

    @app.after_request
    def add_cors_headers(response):
        request_origin = request.headers.get("Origin")
        allowed_origin = _resolve_allowed_origin(request_origin)
        if request_origin is not None:
            _append_vary_header(response, "Origin")
        if allowed_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Access-Control-Allow-Headers"] = (
                f"Content-Type, Authorization, {DESKTOP_TOKEN_HEADER}"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
            response.headers["Access-Control-Max-Age"] = "600"

        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    app.register_blueprint(api_blueprint)
    return app
