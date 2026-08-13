from __future__ import annotations

import os
from typing import Mapping

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
from .runtime import RuntimeBundle, RuntimeManager


MEMOLENS_SERVICE_ID = "memolens-backend"
MEMOLENS_API_VERSION = "1"
_RUNNER_EXTENSION_KEYS = ("media_job_runner", "render_job_runner")


def shutdown_runtime_extensions(extensions: Mapping[str, object]) -> None:
    """Stop candidate runners without touching the app's active runtime."""

    for key in _RUNNER_EXTENSION_KEYS:
        runner = extensions.get(key)
        if runner is not None:
            try:
                runner.shutdown()
            except Exception:
                pass


def build_runtime_extensions(settings: Settings) -> dict[str, object]:
    """Fully construct and validate a runtime before it becomes active."""

    resolved_settings = settings
    resolved_settings.ensure_directories()

    repository = ImageIndexRepository(resolved_settings.db_path)
    repository.ensure_schema()

    vision_client = OpenAICompatibleVisionClient(resolved_settings)
    embedding_service = EmbeddingService(resolved_settings)
    text_embedding_service = TextEmbeddingService(resolved_settings)
    geocoder = ReverseGeocoder(resolved_settings)
    query_planner = OpenAICompatibleQueryPlanner(resolved_settings)
    retrieval_copywriter = RetrievalCopywriter(resolved_settings)
    retrieval_service = RetrievalService(
        settings=resolved_settings,
        repository=repository,
        planner=query_planner,
        text_embedding_service=text_embedding_service,
    )

    # Media jobs remain permanently bound to this repository/database UUID.
    from .media.director import CreativeDirector
    from .media.creator_memory import CreatorMemoryService
    from .media.inbox import MediaInboxService
    from .media.render import RenderJobRunner
    from .media.retrieval import MixedRetrievalService
    from .media.timeline import TimelineService
    from .media.video import MediaJobRunner

    media_repository = MediaRepository(resolved_settings.db_path)
    media_repository.ensure_schema(resolved_settings.image_library_dir)
    media_cache_root = resolved_settings.app_state_dir / "media-cache"
    preview_root = media_repository.register_preview_root(media_cache_root / "previews")
    mixed_retrieval = MixedRetrievalService(media_repository)
    creator_memory = CreatorMemoryService(media_repository)
    extensions: dict[str, object] = {
        "image_index_repository": repository,
        "photo_atlas_service": PhotoAtlasService(repository),
        "vision_client": vision_client,
        "embedding_service": embedding_service,
        "text_embedding_service": text_embedding_service,
        "geocoder": geocoder,
        "query_planner": query_planner,
        "retrieval_copywriter": retrieval_copywriter,
        "retrieval_service": retrieval_service,
        "media_repository": media_repository,
        "mixed_retrieval_service": mixed_retrieval,
        "media_inbox_service": MediaInboxService(media_repository),
        "creator_memory_service": creator_memory,
        "creative_director": CreativeDirector(
            media_repository,
            mixed_retrieval,
            creator_memory,
        ),
        "timeline_service": TimelineService(media_repository),
        "app_preview_root_id": preview_root["id"],
    }
    try:
        extensions["media_job_runner"] = MediaJobRunner(media_repository, media_cache_root)
        extensions["render_job_runner"] = RenderJobRunner(
            media_repository,
            media_cache_root,
            reconcile_on_start=False,
        )
        extensions["indexing_service"] = IndexingService(
            settings=resolved_settings,
            repository=repository,
            vision_client=vision_client,
            embedding_service=embedding_service,
            text_embedding_service=text_embedding_service,
            geocoder=geocoder,
            media_repository=media_repository,
        )
    except Exception:
        shutdown_runtime_extensions(extensions)
        raise
    return extensions


def swap_runtime(
    app: Flask,
    settings: Settings,
    extensions: dict[str, object],
) -> None:
    """Activate and atomically expose one immutable runtime generation."""

    bundle = RuntimeBundle.freeze(settings, extensions)

    # Recovery is an activation step, not a constructor side effect. A
    # prebuilt candidate may therefore be discarded without touching jobs in
    # a database that was never adopted. Activation completes before the
    # bundle becomes visible to new requests.
    media_repository = bundle.extensions.get("media_repository")
    if isinstance(media_repository, MediaRepository):
        try:
            media_repository.mark_running_jobs_interrupted()
        except Exception:
            app.logger.exception("Failed to mark interrupted MemoLens jobs during activation")
    render_runner = bundle.extensions.get("render_job_runner")
    if render_runner is not None:
        try:
            render_runner.reconcile_interrupted_storage()
        except Exception:
            app.logger.exception("Failed to reconcile MemoLens render storage during activation")

    # These values remain compatibility mirrors for existing integrations and
    # tests. Request handlers use RuntimeManager leases as the authoritative
    # source and therefore never observe a partially updated generation.
    missing = object()
    previous_settings = app.config.get("SETTINGS", missing)
    previous_extensions = {key: app.extensions.get(key, missing) for key in extensions}
    manager = app.extensions.get("runtime_manager")
    created_manager = not isinstance(manager, RuntimeManager)
    if created_manager:
        manager = RuntimeManager(lambda retired: shutdown_runtime_extensions(retired.extensions))
    assert isinstance(manager, RuntimeManager)
    try:
        app.extensions["runtime_manager"] = manager
        app.config["SETTINGS"] = settings
        app.extensions.update(extensions)
        manager.swap(bundle)
    except Exception:
        if previous_settings is missing:
            app.config.pop("SETTINGS", None)
        else:
            app.config["SETTINGS"] = previous_settings
        for key, previous in previous_extensions.items():
            if previous is missing:
                app.extensions.pop(key, None)
            else:
                app.extensions[key] = previous
        if created_manager:
            app.extensions.pop("runtime_manager", None)
        raise


def configure_runtime(app: Flask, settings: Settings) -> None:
    extensions = build_runtime_extensions(settings)
    try:
        swap_runtime(app, settings, extensions)
    except Exception:
        shutdown_runtime_extensions(extensions)
        raise


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
