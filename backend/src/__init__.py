from __future__ import annotations

from urllib.parse import urlparse

from flask import Flask, request

from core.config import Settings
from core.db import ImageIndexRepository
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


LOCAL_CORS_HOSTS = {"127.0.0.1", "localhost"}


def _resolve_allowed_origin(origin: str | None) -> str | None:
    if not isinstance(origin, str):
        return None

    normalized = origin.strip()
    if not normalized:
        return None
    if normalized == "null":
        return "null"

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in LOCAL_CORS_HOSTS:
        return None
    return normalized


def configure_runtime(app: Flask, settings: Settings) -> None:
    resolved_settings = settings
    resolved_settings.ensure_directories()
    app.config["SETTINGS"] = resolved_settings

    repository = ImageIndexRepository(resolved_settings.db_path)
    repository.ensure_schema()

    app.extensions["image_index_repository"] = repository
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
    configure_runtime(app, settings or Settings.from_env())

    @app.after_request
    def add_cors_headers(response):
        allowed_origin = _resolve_allowed_origin(request.headers.get("Origin"))
        if allowed_origin is not None:
            response.headers["Access-Control-Allow-Origin"] = allowed_origin
            response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
        return response

    app.register_blueprint(api_blueprint)
    return app
