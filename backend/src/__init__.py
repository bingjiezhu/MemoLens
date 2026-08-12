from __future__ import annotations

import os

from flask import Flask

from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
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
from .security import (
    DESKTOP_TOKEN_HEADER as DESKTOP_TOKEN_HEADER,
    TRUSTED_CORS_ORIGINS as TRUSTED_CORS_ORIGINS,
    install_local_api_security,
)


MEMOLENS_SERVICE_ID = "memolens-backend"
MEMOLENS_API_VERSION = "1"


def configure_runtime(app: Flask, settings: Settings) -> None:
    previous_media_runner = app.extensions.get("media_job_runner")
    previous_render_runner = app.extensions.get("render_job_runner")
    if previous_media_runner is not None:
        previous_media_runner.shutdown()
    if previous_render_runner is not None:
        previous_render_runner.shutdown()
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
    app.extensions["retrieval_service"] = RetrievalService(
        settings=resolved_settings,
        repository=repository,
        planner=app.extensions["query_planner"],
        text_embedding_service=app.extensions["text_embedding_service"],
    )

    # Media jobs remain permanently bound to this repository/database UUID.
    from .media.director import CreativeDirector
    from .media.render import RenderJobRunner
    from .media.retrieval import MixedRetrievalService
    from .media.timeline import TimelineService
    from .media.video import MediaJobRunner

    media_repository = MediaRepository(resolved_settings.db_path)
    media_repository.ensure_schema(resolved_settings.image_library_dir)
    media_repository.mark_running_jobs_interrupted()
    media_cache_root = resolved_settings.app_state_dir / "media-cache"
    preview_root = media_repository.register_preview_root(media_cache_root / "previews")
    mixed_retrieval = MixedRetrievalService(media_repository)
    app.extensions["media_repository"] = media_repository
    app.extensions["mixed_retrieval_service"] = mixed_retrieval
    app.extensions["creative_director"] = CreativeDirector(media_repository, mixed_retrieval)
    app.extensions["timeline_service"] = TimelineService(media_repository)
    app.extensions["media_job_runner"] = MediaJobRunner(media_repository, media_cache_root)
    app.extensions["render_job_runner"] = RenderJobRunner(media_repository, media_cache_root)
    app.extensions["app_preview_root_id"] = preview_root["id"]
    app.extensions["indexing_service"] = IndexingService(
        settings=resolved_settings,
        repository=repository,
        vision_client=app.extensions["vision_client"],
        embedding_service=app.extensions["embedding_service"],
        text_embedding_service=app.extensions["text_embedding_service"],
        geocoder=app.extensions["geocoder"],
        media_repository=media_repository,
    )


def reload_runtime(app: Flask) -> Settings:
    resolved_settings = Settings.from_env()
    configure_runtime(app, resolved_settings)
    return resolved_settings


def create_app(settings: Settings | None = None) -> Flask:
    from .api import api_blueprint

    app = Flask(__name__)
    app.config["DESKTOP_SESSION_TOKEN"] = os.environ.get("MEMOLENS_DESKTOP_SESSION_TOKEN", "").strip()
    configure_runtime(app, settings or Settings.from_env())
    install_local_api_security(app)
    app.register_blueprint(api_blueprint)
    return app
