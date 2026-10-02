from __future__ import annotations

import os
import secrets
import threading
from typing import Mapping

from flask import Flask

from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import SCHEMA_VERSION, MediaRepository
from core.network_policy import install_python_network_audit_hook
from core.photo_atlas import PhotoAtlasService
from core.sqlite_runtime import require_safe_sqlite_runtime
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
    MAIN_AUTHORITY_HEADER as MAIN_AUTHORITY_HEADER,
    TRUSTED_CORS_ORIGINS as TRUSTED_CORS_ORIGINS,
    install_local_api_security,
)
from .runtime import RuntimeBundle, RuntimeHealthIdentity, RuntimeManager

if False:  # pragma: no cover - typing-only import without a runtime cycle
    from .media.library_bootstrap import BootstrapCandidateBinding


MEMOLENS_SERVICE_ID = "memolens-backend"
MEMOLENS_API_VERSION = "1"
_RUNNER_EXTENSION_KEYS = (
    "canonical_export_job_runner",
    "media_job_runner",
    "render_job_runner",
)


def shutdown_runtime_extensions(extensions: Mapping[str, object]) -> None:
    """Stop runners, then close the repository owned by a discarded runtime."""

    runners_quiesced = True
    for key in _RUNNER_EXTENSION_KEYS:
        runner = extensions.get(key)
        if runner is not None:
            try:
                runner.shutdown()
            except Exception:
                runners_quiesced = False
    # A failed shutdown may mean an executor is still using this repository.
    # Keep its lifetime resources open rather than racing a live worker.
    if not runners_quiesced:
        return
    media_repository = extensions.get("media_repository")
    close = getattr(media_repository, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def build_runtime_extensions(
    settings: Settings,
    *,
    schema_prepared: bool = False,
) -> dict[str, object]:
    """Fully construct and validate a runtime before it becomes active."""

    require_safe_sqlite_runtime()
    resolved_settings = settings
    resolved_settings.ensure_directories()

    repository = ImageIndexRepository(resolved_settings.db_path)
    if not schema_prepared:
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
    from .media.blueprint import BlueprintService
    from .media.coverage import CoverageService
    from .media.canonical_export import CanonicalExportJobRunner, CanonicalExportService
    from .media.creator_memory import CreatorMemoryService
    from .media.inbox import MediaInboxService
    from .media.provider_egress import ProviderImageVisionEgress
    from .media.render import RenderJobRunner
    from .media.retrieval import MixedRetrievalService
    from .media.timeline import TimelineService
    from .media.timeline_lowering import TimelineLoweringService
    from .media.video import MediaJobRunner

    media_repository = MediaRepository(resolved_settings.db_path)
    if not schema_prepared:
        media_repository.ensure_schema(resolved_settings.image_library_dir)
    media_cache_root = resolved_settings.app_state_dir / "media-cache"
    preview_root = media_repository.register_preview_root(media_cache_root / "previews")
    mixed_retrieval = MixedRetrievalService(media_repository)
    creator_memory = CreatorMemoryService(media_repository)
    # A runtime always owns the egress boundary, but it has no grant resolver
    # by default. Configured credentials or a desktop token alone therefore
    # cannot enable provider sends; a native policy owner must inject an exact
    # sealed grant through a future high-authority integration.
    provider_image_vision_egress = ProviderImageVisionEgress(media_repository)
    extensions: dict[str, object] = {
        "image_index_repository": repository,
        "photo_atlas_service": PhotoAtlasService(
            repository,
            media_repository,
        ),
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
        "provider_image_vision_egress": provider_image_vision_egress,
        "creative_director": CreativeDirector(
            media_repository,
            mixed_retrieval,
            creator_memory,
        ),
        "blueprint_service": BlueprintService(media_repository),
        "coverage_service": CoverageService(media_repository),
        "timeline_service": TimelineService(media_repository),
        "timeline_lowering_service": TimelineLoweringService(media_repository),
        "app_preview_root_id": preview_root["id"],
    }
    try:
        extensions["media_job_runner"] = MediaJobRunner(
            media_repository,
            media_cache_root,
            image_embedding_service=embedding_service,
            image_provider_vision_egress=provider_image_vision_egress,
        )
        extensions["render_job_runner"] = RenderJobRunner(
            media_repository,
            media_cache_root,
            reconcile_on_start=False,
        )
        canonical_export_runner = CanonicalExportJobRunner(
            media_repository,
            media_cache_root,
        )
        extensions["canonical_export_job_runner"] = canonical_export_runner
        extensions["canonical_export_service"] = CanonicalExportService(
            media_repository,
            canonical_export_runner,
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
        # Bind the complete runtime generation to the database inode before it
        # can admit durable image work.  A same-path replacement during this
        # runtime is therefore rejected even when a copied database preserves
        # its database_uuid.
        media_repository.bind_image_database_identity()
        # Provider egress is one-shot authority. A reservation that never
        # reached the transport is released, while any interrupted send is
        # closed as ambiguous. Recovery is a hard activation gate so no new
        # worker can observe a runtime with stale network authority.
        media_repository.recover_provider_egress_state()
        try:
            media_repository.mark_running_jobs_interrupted()
        except Exception:
            app.logger.exception("Failed to mark interrupted MemoLens jobs during activation")
        # Canonical egress recovery is an activation gate. Exposing a runtime
        # after this fails would leave approved jobs active with no live owner.
        media_repository.mark_canonical_export_jobs_interrupted()
    render_runner = bundle.extensions.get("render_job_runner")
    if render_runner is not None:
        try:
            render_runner.reconcile_interrupted_storage()
        except Exception:
            app.logger.exception("Failed to reconcile MemoLens render storage during activation")
    canonical_export_runner = bundle.extensions.get("canonical_export_job_runner")
    if canonical_export_runner is not None:
        canonical_export_runner.reconcile_interrupted_storage()
    media_job_runner = bundle.extensions.get("media_job_runner")
    if media_job_runner is not None:
        activate = getattr(media_job_runner, "activate_runtime_generation", None)
        recover = getattr(media_job_runner, "recover", None)
        if callable(activate):
            active_library = bundle.settings.image_library_dir.resolve(strict=True)
            active_library_root_id = MediaRepository._root_id(active_library)
            active_library_record = (
                media_repository.library_root(active_library_root_id)
                if isinstance(media_repository, MediaRepository)
                else None
            )
            if (
                active_library_record is None
                or active_library_record.get("status") != "active"
                or active_library_record.get("canonical_path") != str(active_library)
            ):
                raise RuntimeError("Active image Library authority is unavailable.")
            activate(
                bundle.generation_id,
                active_library_root_id=active_library_root_id,
            )
        if callable(recover):
            # Image attempts are generation-bound.  Recovery must append a new
            # authority for this exact immutable bundle before it is visible
            # to requests; generic media-job resume is not sufficient.
            recover()

    # These values remain compatibility mirrors for existing integrations and
    # tests. Request handlers use RuntimeManager leases as the authoritative
    # source and therefore never observe a partially updated generation.
    missing = object()
    previous_settings = app.config.get("SETTINGS", missing)
    previous_health_identity = app.config.get("RUNTIME_HEALTH_IDENTITY", missing)
    previous_extensions = {key: app.extensions.get(key, missing) for key in extensions}
    manager = app.extensions.get("runtime_manager")
    created_manager = not isinstance(manager, RuntimeManager)
    if created_manager:
        manager = RuntimeManager(lambda retired: shutdown_runtime_extensions(retired.extensions))
    assert isinstance(manager, RuntimeManager)
    bootstrap_binding = app.extensions.get("library_bootstrap_candidate_binding")
    bootstrap_request_id: str | None = None
    candidate_binding_sha256: str | None = None
    if bootstrap_binding is not None:
        bound_settings = getattr(bootstrap_binding, "settings", None)
        if bound_settings is not settings:
            raise RuntimeError("Bootstrap runtime settings do not match the exact candidate.")
        bootstrap_request_id = str(getattr(bootstrap_binding, "request_id", ""))
        candidate_binding_sha256 = str(
            getattr(bootstrap_binding, "candidate_binding_sha256", "")
        )
    health_identity = (
        RuntimeHealthIdentity.active(
            database_uuid=media_repository.database_uuid,
            schema_version=SCHEMA_VERSION,
            runtime_generation_id=bundle.generation_id,
            bootstrap_request_id=bootstrap_request_id,
            candidate_binding_sha256=candidate_binding_sha256,
        )
        if isinstance(media_repository, MediaRepository)
        else None
    )
    try:
        app.extensions["runtime_manager"] = manager
        app.config["SETTINGS"] = settings
        app.extensions.update(extensions)
        preview_broker = app.extensions.get("agent_preview_broker")
        drop_all = getattr(preview_broker, "drop_all", None)
        if callable(drop_all):
            # A settings/runtime generation swap invalidates every process-local
            # media lease, including already-active stream reservations.  Drop
            # before retiring the old bundle so a slow runner shutdown cannot
            # leave its fd-pinned stream active during the transition.
            drop_all()
        manager.swap(bundle)
        if health_identity is not None:
            app.config["RUNTIME_HEALTH_IDENTITY"] = health_identity
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
        if previous_health_identity is missing:
            app.config.pop("RUNTIME_HEALTH_IDENTITY", None)
        else:
            app.config["RUNTIME_HEALTH_IDENTITY"] = previous_health_identity
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


def create_app(
    settings: Settings | None = None,
    *,
    bootstrap_candidate: "BootstrapCandidateBinding | None" = None,
) -> Flask:
    install_python_network_audit_hook()
    sqlite_runtime_capability = require_safe_sqlite_runtime()

    from .api import api_blueprint
    from .agent_authority import AgentPairingBroker
    from .agent_preview import PreviewLeaseBroker

    # MemoLens has no Flask-owned static surface.  Leaving Flask's implicit
    # ``/static/<path>`` route enabled would bypass the explicit ``/v1``
    # loopback boundary and turn an unused framework default into a production
    # file-read endpoint.
    app = Flask(__name__, static_folder=None)
    # Settings activation and native export admission must be serialized
    # before a request pins a RuntimeBundle. Otherwise an export can wait on
    # the old bundle while Settings swaps in a new writer for the same DB.
    app.extensions["runtime_admission_lock"] = threading.RLock()
    app.config["SQLITE_RUNTIME_CAPABILITY"] = sqlite_runtime_capability
    desktop_token = os.environ.get("MEMOLENS_DESKTOP_SESSION_TOKEN", "").strip()
    main_token = os.environ.get("MEMOLENS_MAIN_AUTHORITY_TOKEN", "").strip()
    # A configuration that aliases renderer and main credentials is disabled,
    # never silently accepted as equivalent authority.
    if desktop_token and main_token and secrets.compare_digest(desktop_token, main_token):
        main_token = ""
    runtime_epoch = os.environ.get("MEMOLENS_RUNTIME_AUTHORITY_EPOCH", "").strip()
    if not runtime_epoch:
        # Keeps the runtime broker well-formed for read/B0-only launches.  Main
        # authority remains disabled until a separate token is injected.
        runtime_epoch = f"epoch_{secrets.token_urlsafe(18)}"
    app.config["DESKTOP_SESSION_TOKEN"] = desktop_token
    app.config["MAIN_AUTHORITY_TOKEN"] = main_token
    app.config["RUNTIME_AUTHORITY_EPOCH"] = runtime_epoch
    app.extensions["runtime_manager"] = RuntimeManager(
        lambda retired: shutdown_runtime_extensions(retired.extensions)
    )
    if settings is not None and bootstrap_candidate is not None:
        raise ValueError("Explicit settings and a bootstrap candidate are mutually exclusive.")
    if bootstrap_candidate is None:
        configure_runtime(app, settings or Settings.from_env())
    else:
        app.extensions["library_bootstrap_candidate_binding"] = bootstrap_candidate
        app.config["RUNTIME_HEALTH_IDENTITY"] = RuntimeHealthIdentity.bootstrap_candidate(
            request_id=bootstrap_candidate.request_id,
            binding_sha256=bootstrap_candidate.candidate_binding_sha256,
        )
    app.extensions["agent_pairing_broker"] = AgentPairingBroker(runtime_epoch)
    app.extensions["agent_preview_broker"] = PreviewLeaseBroker(runtime_epoch)
    install_local_api_security(app)
    app.register_blueprint(api_blueprint)
    return app
