from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, g, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from core.app_settings import load_persisted_app_settings, save_persisted_app_settings
from core.config import Settings, _load_yaml, _resolve_vlm_profile
from core.canonical_image_observation import (
    is_verified_current_canonical_image_observation,
)
from core.db import ImageIndexDatabaseScopeChangedError, ImageIndexRepository
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import (
    BlueprintHeadConflictError,
    BlueprintIntegrityError,
    BlueprintReceiptConflictError,
    CanonicalTimelineHeadConflictError,
    CanonicalTimelineIntegrityError,
    CanonicalTimelineReceiptConflictError,
    CanonicalTimelineSourceUnavailableError,
    CanonicalExportIntegrityError,
    CanonicalExportJobConflictError,
    CanonicalExportNativeNonceConflictError,
    CanonicalExportReceiptConflictError,
    CoverageHeadConflictError,
    CoverageIntegrityError,
    CoverageReceiptConflictError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    LibraryScanConflictError,
    LibraryScanIntegrityError,
    LibraryScanValidationError,
    canonical_json,
)
from core.local_model_runtime import detect_local_model_runtime
from core.photo_atlas import (
    AtlasFilters,
    AtlasAssetObservationConflictError,
    AtlasProjectionNotReadyError,
    PhotoAtlasService,
    normalize_lens,
    normalize_mode,
    parse_bool,
    parse_float,
    parse_limit,
    validate_atlas_asset_observation_request,
)
from core.schemas import RetrievedImageSummary, parse_indexing_request, parse_retrieval_request
from backend.src import (
    MEMOLENS_API_VERSION,
    MEMOLENS_SERVICE_ID,
    build_runtime_extensions,
    shutdown_runtime_extensions,
    swap_runtime,
)
from backend.src.runtime import RuntimeHealthIdentity, RuntimeLease, RuntimeManager
from indexing.files import (
    PinnedLibraryRoot,
    ensure_heif_support,
    open_pinned_library_root,
)
from backend.src.media.importing import MediaImportService
from backend.src.media.import_plan import ImageImportEnqueueAuthority
from backend.src.media.legacy_image_backfill import (
    LegacyImageBackfillError,
    LegacyImageBackfillService,
)
from backend.src.media.library_bootstrap import (
    BOOTSTRAP_IDEMPOTENCY_HEADER,
    BootstrapCandidateBinding,
    LibraryBootstrapError,
    commit_library_bootstrap,
    validate_bootstrap_command,
)
from backend.src.media.library_scan import (
    LibraryScanContractError,
    public_library_scan_snapshot,
)
from backend.src.media.render import ffmpeg_encode_capability
from backend.src.media.render_plan import build_render_plan
from backend.src.media.render_sources import SourceVerifier
from backend.src.media.director import CreativeBriefError
from backend.src.media.blueprint import BlueprintServiceError
from backend.src.media.coverage import CoverageServiceError
from backend.src.media.canonical_export import CanonicalExportServiceError
from backend.src.media.timeline_lowering import (
    TimelineLoweringServiceError,
)
from backend.src.media.timeline_preview import TimelinePreviewService
from backend.src.media.creator_memory import ProfileRevisionConflictError
from backend.src.media.inbox import (
    InboxAssetNotFoundError,
    ReviewRevisionConflictError,
)
from backend.src.media.timeline import (
    BlueprintLegacyTimelineWriteUnavailable,
    BlueprintTimelineCompilerUnavailable,
    TimelineValidationError,
)
from backend.src.media.residual_parent import ResidualParentResolutionError
from backend.src.api.strict_json import MAX_RAW_BYTES, StrictJsonRequestError, strict_json_object
from backend.src.agent_authority import (
    AGENT_CAPABILITY_HEADER,
    AGENT_NONCE_HEADER,
    AGENT_PROOF_HEADER,
    AGENT_STATUS_PROOF_HEADER,
    AgentPairingBroker,
    AgentProtocolError,
    sha256_text,
)
from backend.src.agent_preview import (
    PREVIEW_LEASE_HEADER,
    PREVIEW_NONCE_HEADER,
    PREVIEW_PROOF_HEADER,
    AgentPreviewError,
    PreviewLeaseBroker,
)
from backend.src.security import MAIN_AUTHORITY_HEADER
from backend.src.media.video import (
    IMAGE_EXTENSIONS,
    MediaCapabilityError,
    VIDEO_EXTENSIONS,
    binary_capability,
)
from backend.src.security import is_local_remote_addr as _is_local_remote_addr
from core.timeline_lowering_contract import TimelineLoweringContractError
from core.timeline_edit_contract import TimelineEditContractError
from core.timeline_structural_edit_contract import TimelineStructuralEditContractError
from core.timeline_restore_contract import TimelineRestoreContractError


ensure_heif_support()

api_blueprint = Blueprint("api", __name__)


@api_blueprint.errorhandler(AtlasProjectionNotReadyError)
def _atlas_projection_not_ready(exc: AtlasProjectionNotReadyError):
    return jsonify(exc.to_payload()), exc.status_code


@api_blueprint.errorhandler(AtlasAssetObservationConflictError)
def _atlas_asset_observation_conflict(exc: AtlasAssetObservationConflictError):
    return jsonify(exc.to_payload()), exc.status_code


def _request_is_local() -> bool:
    return _is_local_remote_addr(request.remote_addr)


def _local_only_error(message: str = "This endpoint is only available to local clients."):
    return (
        jsonify(
            {
                "object": "error",
                "message": message,
                "type": "permission_error",
            }
        ),
        403,
    )


def _media_error(code: str, message: str, status: int = 400, *, details: object | None = None):
    return jsonify(_media_error_payload(code, message, status, details=details)), status


def _media_error_payload(code: str, message: str, status: int, *, details: object | None = None) -> dict[str, object]:
    body: dict[str, object] = {
        "object": "error",
        "error": {"code": code, "message": message, "retryable": status >= 500},
        "code": code,
        "message": message,
        "type": "permission_error" if status in {401, 403} else "invalid_request_error",
    }
    if details is not None:
        body["error"]["details"] = details
        body["details"] = details
    return body


def _desktop_token_authenticated() -> bool:
    expected = str(current_app.config.get("DESKTOP_SESSION_TOKEN") or "")
    supplied = request.headers.get("X-MemoLens-Desktop-Token", "")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def _main_authority_authenticated() -> bool:
    """Authenticate Electron main without accepting renderer/browser authority."""

    expected = str(current_app.config.get("MAIN_AUTHORITY_TOKEN") or "")
    supplied = request.headers.get(MAIN_AUTHORITY_HEADER, "")
    return bool(
        request.headers.get("Origin") is None
        and expected
        and supplied
        and hmac.compare_digest(expected, supplied)
    )


def _require_main_authority():
    if not _request_is_local():
        return _media_error("loopback_required", "MemoLens main routes are loopback-only.", 403)
    if "/blueprint/authority/" in request.path and any(
        request.headers.get(header)
        for header in (AGENT_CAPABILITY_HEADER, AGENT_NONCE_HEADER, AGENT_PROOF_HEADER)
    ):
        return _media_error(
            "agent_confirmation_forbidden",
            "Paired Agents cannot confirm or revoke Blueprint decision authority.",
            403,
        )
    if request.headers.get("Origin") is not None:
        return _media_error(
            "native_confirmation_required",
            "This operation requires an originless Electron main request.",
            403,
        )
    if not _main_authority_authenticated():
        return _media_error(
            "native_confirmation_required",
            "This operation requires the independent Electron main authority credential.",
            403,
        )
    return None


def _require_agent_originless():
    if not _request_is_local():
        return _media_error("loopback_required", "MemoLens Agent routes are loopback-only.", 403)
    if request.headers.get("Origin") is not None:
        return _media_error(
            "agent_pairing_required",
            "Agent protocol routes accept originless native loopback clients only.",
            401,
        )
    return None


def _agent_broker() -> AgentPairingBroker:
    broker = current_app.extensions.get("agent_pairing_broker")
    if not isinstance(broker, AgentPairingBroker):
        raise AgentProtocolError(
            "agent_pairing_required",
            "The Agent pairing broker is unavailable in this runtime.",
            status=401,
        )
    return broker


def _preview_broker() -> PreviewLeaseBroker:
    broker = current_app.extensions.get("agent_preview_broker")
    if not isinstance(broker, PreviewLeaseBroker):
        raise AgentPreviewError(
            "agent_preview_lease_expired",
            "Timeline preview authority is unavailable in this runtime.",
            status=403,
        )
    return broker


def _drop_preview_project_leases(project_id: str) -> None:
    broker = current_app.extensions.get("agent_preview_broker")
    if isinstance(broker, PreviewLeaseBroker):
        broker.drop_project(project_id)


def _runtime_generation_id() -> str:
    lease = getattr(g, "memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        return lease.generation_id
    manager = current_app.extensions.get("runtime_manager")
    if isinstance(manager, RuntimeManager):
        return manager.current_generation_id
    value = str(current_app.config.get("RUNTIME_GENERATION_ID") or "")
    if not value:
        raise AgentPreviewError(
            "agent_preview_lease_expired",
            "Timeline preview runtime generation is unavailable.",
            status=403,
        )
    return value


def _timeline_preview_service() -> TimelinePreviewService:
    return TimelinePreviewService(
        _media_repository(),
        _agent_broker(),
        _preview_broker(),
    )


def _runtime_authority_epoch() -> str:
    value = str(current_app.config.get("RUNTIME_AUTHORITY_EPOCH") or "")
    if not value:
        raise AgentProtocolError(
            "agent_pairing_required",
            "Runtime authority is unavailable.",
            status=401,
        )
    return value


def _native_gesture_nonce(value: object) -> str:
    if (
        type(value) is not str
        or not 16 <= len(value) <= 200
        or re.fullmatch(r"[A-Za-z0-9._:-]+", value) is None
    ):
        raise AgentProtocolError(
            "native_confirmation_required",
            "A bounded native gesture nonce is required.",
            status=403,
        )
    return value


def _protocol_identifier(value: object, *, code: str = "agent_pairing_required") -> str:
    if (
        type(value) is not str
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is None
    ):
        status = (
            422
            if code == "invalid_blueprint_decision_units"
            else 403
            if code.endswith("scope_denied")
            else 401
        )
        raise AgentProtocolError(code, "Protocol identity is invalid.", status=status)
    return value


def _sha256_digest(value: object, *, code: str) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise AgentProtocolError(code, "Presentation digest is invalid.", status=409)
    return value


def _main_idempotency_key() -> str:
    value = request.headers.get("Idempotency-Key", "")
    if not value or len(value) > 200 or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", value) is None:
        raise AgentProtocolError(
            "invalid_idempotency_key",
            "A bounded ASCII Idempotency-Key is required.",
            status=400,
        )
    return value


def _parse_utc(value: object) -> datetime:
    if type(value) is not str:
        raise AgentProtocolError(
            "blueprint_integrity_error",
            "Persisted capability time is invalid.",
            status=409,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgentProtocolError(
            "blueprint_integrity_error",
            "Persisted capability time is invalid.",
            status=409,
        ) from exc
    if parsed.tzinfo is None:
        raise AgentProtocolError(
            "blueprint_integrity_error",
            "Persisted capability time is invalid.",
            status=409,
        )
    return parsed.astimezone(timezone.utc)


def _require_media_desktop_token():
    if not _request_is_local():
        return _media_error("loopback_required", "MemoLens media routes are loopback-only.", 403)
    if not _desktop_token_authenticated():
        return _media_error(
            "desktop_auth_required",
            "This media operation requires the per-launch desktop session token.",
            401,
        )
    return None


def _require_media_read_access():
    """Read-only media surfaces use the existing loopback + trusted-Origin boundary."""
    if not _request_is_local():
        return _media_error("loopback_required", "MemoLens media routes are loopback-only.", 403)
    return None


def _media_repository():
    return _runtime_extension("media_repository")


def _runtime_extension(name: str):
    lease = getattr(g, "memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        return lease.bundle.extension(name)
    # Small unit-test apps may register this blueprint without create_app.
    return current_app.extensions[name]


def _runtime_extension_optional(name: str):
    lease = getattr(g, "memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        return lease.bundle.extensions.get(name)
    return current_app.extensions.get(name)


def _runtime_settings():
    lease = getattr(g, "memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        return lease.bundle.settings
    return current_app.config["SETTINGS"]


def _legacy_provider_egress_authorized() -> bool:
    # A desktop token proves the local caller, but it is not a provider opt-in
    # grant and does not bind a payload manifest.  Until the canonical grant
    # ledger can validate both, these legacy read routes remain deterministic
    # local operations even when provider credentials are configured.
    return False


class _DeterministicLocalPlanner:
    """Narrow adapter exposing only the planner's deterministic algorithms."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def plan(
        self,
        text: str,
        current_datetime: str,
        top_k_override: int | None = None,
    ):
        return self._delegate._fallback_plan(
            text=text,
            current_datetime=current_datetime,
            top_k_override=top_k_override,
        )

    def generate_search_suggestions(
        self,
        *,
        library_summary: dict[str, object],
        memories: list[dict[str, object]],
        context_assets: list[dict[str, object]] | None = None,
        count: int = 5,
    ) -> list[str]:
        return self._delegate._fallback_search_suggestions(
            library_summary=library_summary,
            memories=memories,
            context_assets=context_assets or [],
            count=max(3, min(int(count or 5), 8)),
        )


def _deterministic_local_copy(
    copywriter,
    *,
    query_text: str,
    retrieved_images: list[RetrievedImageSummary],
):
    return copywriter._fallback_generated_copy(
        query_text=query_text,
        retrieved_images=retrieved_images,
    )


MEDIA_ROUTE_PREFIXES = (
    "/v1/media/",
    "/v1/assets/",
    "/v1/index/jobs",
    "/v1/image-analysis/",
    "/v1/search/mixed",
    "/v1/video-segments/",
    "/v1/keyframes/",
    "/v1/creative/",
    "/v1/timelines/",
    "/v1/renders",
    "/v1/inbox",
    "/v1/creator/",
)

MEDIA_MUTATION_ENDPOINTS = frozenset(
    {
        "cancel_media_index_job",
        "cancel_timeline_render",
        "commit_blueprint_proposal",
        "materialize_coverage_baseline",
        "materialize_project_timeline",
        "reconcile_project_timeline",
        "edit_project_timeline",
        "structurally_edit_project_timeline",
        "restore_project_timeline",
        "create_creative_brief",
        "create_project_timeline",
        "import_media_assets",
        "create_legacy_image_backfill_batch",
        "resume_media_index_job",
        "restore_blueprint_revision",
        "revise_timeline",
        "start_timeline_render",
        "update_creator_profile",
        "update_media_inbox_asset",
    }
)
MEDIA_PRIVILEGED_ENDPOINTS = MEDIA_MUTATION_ENDPOINTS | frozenset(
    {
        "download_timeline_render",
        "get_media_capabilities",
        "stream_media_asset",
        "validate_timeline",
    }
)
STRICT_QUERY_BOUND_MUTATION_ENDPOINTS = frozenset(
    {
        "commit_blueprint_proposal",
        "materialize_coverage_baseline",
        "materialize_project_timeline",
        "reconcile_project_timeline",
        "edit_project_timeline",
        "structurally_edit_project_timeline",
        "restore_project_timeline",
        "restore_blueprint_revision",
        "create_legacy_image_backfill_batch",
    }
)
RUNTIME_PINNED_DATABASE_MUTATION_ENDPOINTS = frozenset(
    {
        "cancel_media_index_job",
        "create_legacy_image_backfill_batch",
        "resume_media_index_job",
    }
)
RUNTIME_ADMISSION_SERIALIZED_ENDPOINTS = frozenset(
    {"bootstrap_main_library", "start_main_canonical_export", "update_settings"}
)
RUNTIME_UNPINNED_ENDPOINTS = frozenset({"bootstrap_main_library", "healthz"})

# These legacy routes predate the media capability boundary.  Loopback is a
# transport property, not user authority: any local process can originate an
# originless HTTP request.  High-impact legacy reads/writes therefore require
# the per-launch desktop credential before Flask parses a JSON body or a route
# can dispatch a service.
LEGACY_DESKTOP_AUTHORITY_ENDPOINTS = frozenset(
    {
        "create_atlas_feedback",
        "create_atlas_stack_action",
        "create_indexing_job",
        "rebuild_atlas",
        "save_atlas_basket",
        "update_settings",
    }
)
LEGACY_HIGH_IMPACT_SCOPE_ENDPOINTS = LEGACY_DESKTOP_AUTHORITY_ENDPOINTS
LEGACY_PROVIDER_EGRESS_ENDPOINTS = frozenset(
    {
        "create_retrieval_copy",
        "create_retrieval_query",
        "generate_from_atlas",
        "generate_search_inspiration",
    }
)

# Electron main authority is admitted before runtime leasing, JSON decoding,
# repository access, or endpoint dispatch.  The route-local checks remain as
# defence in depth, while this closed set lets the production oracle prove that
# a renderer, browser, or unrelated local process cannot reach main-only
# business logic by presenting the desktop credential instead.
MAIN_AUTHORITY_ENDPOINTS = frozenset(
    {
        "approve_main_agent_pairing",
        "build_main_blueprint_authority_presentation",
        "build_main_canonical_export_presentation",
        "bootstrap_main_library",
        "confirm_main_blueprint_authority",
        "get_main_agent_capability_revoke_presentation",
        "get_main_agent_pairing_presentation",
        "list_main_agent_capabilities",
        "list_main_agent_pairings",
        "reject_main_agent_pairing",
        "revoke_main_agent_capability",
        "revoke_main_blueprint_authority",
        "start_main_canonical_export",
    }
)


@api_blueprint.before_request
def _admit_legacy_authority():
    """Close the historical originless-loopback authority shortcut."""

    if request.method == "OPTIONS":
        return None
    endpoint = (request.endpoint or "").rsplit(".", 1)[-1]
    if endpoint in MAIN_AUTHORITY_ENDPOINTS:
        return _require_main_authority()
    if endpoint in LEGACY_DESKTOP_AUTHORITY_ENDPOINTS:
        return _require_media_desktop_token()
    if endpoint in LEGACY_PROVIDER_EGRESS_ENDPOINTS:
        if not _request_is_local():
            return _media_error(
                "loopback_required",
                "MemoLens provider routes are loopback-only.",
                403,
            )
        # Provider credentials and a desktop session are insufficient on their
        # own.  A future provider adapter must consume an explicit opt-in grant
        # plus a bounded payload manifest before this branch can be enabled.
        g.memolens_legacy_provider_egress = False
    return None


@api_blueprint.before_request
def _pin_runtime_bundle():
    endpoint = (request.endpoint or "").rsplit(".", 1)[-1]
    admission_lock = current_app.extensions.get("runtime_admission_lock")
    if endpoint in RUNTIME_ADMISSION_SERIALIZED_ENDPOINTS and admission_lock is not None:
        admission_lock.acquire()
        g.memolens_runtime_admission_lock = admission_lock
    manager = current_app.extensions.get("runtime_manager")
    try:
        if isinstance(manager, RuntimeManager) and endpoint not in RUNTIME_UNPINNED_ENDPOINTS:
            g.memolens_runtime_lease = manager.acquire()
    except BaseException:
        held_lock = g.pop("memolens_runtime_admission_lock", None)
        if held_lock is not None:
            held_lock.release()
        raise
    return None


def _legacy_scope_error(field_name: str):
    return _media_error(
        "filesystem_scope_mismatch",
        f"`{field_name}` does not match the active MemoLens runtime scope.",
        409,
    )


def _query_scope_path(field_name: str) -> Path | None:
    raw_value = request.args.get(field_name)
    if raw_value is None:
        return None
    if not raw_value.strip():
        raise ValueError(field_name)
    try:
        return Path(raw_value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(field_name) from exc


@api_blueprint.before_request
def _verify_legacy_filesystem_scope():
    """Validate optional legacy runtime-scope proofs before parsing JSON."""

    if request.method == "OPTIONS":
        return None
    endpoint = (request.endpoint or "").rsplit(".", 1)[-1]
    if endpoint not in LEGACY_HIGH_IMPACT_SCOPE_ENDPOINTS:
        return None

    settings = _runtime_settings()
    try:
        requested_db = _query_scope_path("db_path")
    except ValueError:
        return _legacy_scope_error("db_path")
    active_db = settings.db_path.expanduser().resolve(strict=False)
    if requested_db is not None and requested_db != active_db:
        return _legacy_scope_error("db_path")

    for root_field in ("image_dir", "image_library_dir", "root_path"):
        try:
            requested_root = _query_scope_path(root_field)
        except ValueError:
            return _legacy_scope_error(root_field)
        if requested_root is not None:
            active_root = settings.image_library_dir.expanduser().resolve(strict=True)
            if requested_root != active_root:
                return _legacy_scope_error(root_field)

    raw_source = request.args.get("source_path")
    if raw_source is None:
        return None
    if not raw_source.strip():
        return _legacy_scope_error("source_path")
    active_root = settings.image_library_dir.expanduser().resolve(strict=True)
    candidate = Path(raw_source).expanduser()
    lexical_candidate = candidate if candidate.is_absolute() else active_root / candidate
    try:
        normalized = Path(os.path.abspath(lexical_candidate))
        relative = normalized.relative_to(active_root)
        resolved = lexical_candidate.resolve(strict=True)
        resolved.relative_to(active_root)
        if not resolved.is_file():
            raise ValueError("source_path")
        cursor = active_root
        for component in relative.parts:
            cursor /= component
            if cursor.is_symlink():
                raise ValueError("source_path")
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return _legacy_scope_error("source_path")
    return None


@api_blueprint.teardown_request
def _release_runtime_bundle(_error: BaseException | None):
    lease = g.pop("memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        lease.release()
    admission_lock = g.pop("memolens_runtime_admission_lock", None)
    if admission_lock is not None:
        admission_lock.release()


@api_blueprint.before_request
def _verify_media_database_binding():
    if not request.path.startswith(MEDIA_ROUTE_PREFIXES):
        return None
    # A browser preflight carries neither the JSON body nor query parameters
    # of the eventual write. The app-level security boundary has already
    # validated its Origin; token and database binding belong to the real
    # request, not to OPTIONS.
    if request.method == "OPTIONS":
        return None
    endpoint = (request.endpoint or "").rsplit(".", 1)[-1]
    if endpoint in MEDIA_PRIVILEGED_ENDPOINTS:
        denied = _require_media_desktop_token()
        if denied:
            return denied
    raw_path = request.args.get("db_path")
    if endpoint in STRICT_QUERY_BOUND_MUTATION_ENDPOINTS:
        content_length = request.content_length
        if content_length is not None and content_length > MAX_RAW_BYTES:
            return _media_error(
                "json_transport_too_large",
                "Request JSON exceeds the transport size limit.",
                400,
            )
    if (
        raw_path is None
        and endpoint not in STRICT_QUERY_BOUND_MUTATION_ENDPOINTS
        and request.is_json
    ):
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            raw_path = payload.get("db_path")
    if (
        raw_path is None
        and endpoint in MEDIA_MUTATION_ENDPOINTS
        and endpoint not in RUNTIME_PINNED_DATABASE_MUTATION_ENDPOINTS
    ):
        return _media_error(
            "database_binding_required",
            "Media writes require the active MemoLens database path.",
            409,
        )
    if raw_path is None:
        return None
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _media_error("database_binding_mismatch", "Media database binding is invalid.", 409)
    try:
        requested = Path(raw_path).expanduser().resolve(strict=False)
    except (OSError, ValueError):
        return _media_error("database_binding_mismatch", "Media database binding is invalid.", 409)
    if requested != _media_repository().db_path:
        return _media_error(
            "database_binding_mismatch",
            "This backend process is bound to a different MemoLens database. Reload Settings before continuing.",
            409,
        )
    return None


def _json_object() -> dict[str, object]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object.")
    return payload


class InvalidIdempotencyKeyError(ValueError):
    """A write authority credential is absent or outside its closed bounds."""


def _atomic_idempotency_context(scope: str, payload: dict[str, object]) -> tuple[str, str, str]:
    """Validate a key without opening the legacy claim/write/finish transaction gap."""
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 200:
        raise InvalidIdempotencyKeyError(
            "A valid `Idempotency-Key` header is required."
        )
    request_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return f"desktop:{scope}", key, request_hash


def _idempotency_exception(exc: IdempotencyConflictError):
    if isinstance(exc, IdempotencyInProgressError):
        body = _media_error_payload(
            "request_in_progress",
            str(exc),
            409,
            details={"retry_after_seconds": 1},
        )
        body["error"]["retryable"] = True
        return jsonify(body), 409
    return _media_error("idempotency_conflict", str(exc), 409)


def _blueprint_exception(exc: BaseException):
    if isinstance(exc, StrictJsonRequestError):
        return _media_error(exc.code, str(exc), 400)
    if isinstance(exc, BlueprintServiceError):
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, BlueprintHeadConflictError):
        current = exc.current
        details = None
        if isinstance(current, dict):
            details = {
                "current": {
                    key: current.get(key)
                    for key in ("revision", "content_sha256", "semantic_sha256", "operation_id")
                }
            }
        return _media_error(
            "blueprint_head_conflict",
            "Creative Blueprint head changed; reload the exact current revision before retrying.",
            409,
            details=details,
        )
    if isinstance(exc, BlueprintReceiptConflictError):
        return _media_error(
            "blueprint_idempotency_conflict",
            "This Blueprint Idempotency-Key is permanently bound to a different request.",
            409,
        )
    if isinstance(exc, BlueprintIntegrityError):
        return _media_error(
            "blueprint_integrity_error",
            "Persisted Creative Blueprint state failed integrity validation.",
            409,
        )
    raise exc


def _coverage_exception(exc: BaseException):
    if isinstance(exc, StrictJsonRequestError):
        return _media_error(exc.code, str(exc), 400)
    if isinstance(exc, CoverageServiceError):
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, CoverageHeadConflictError):
        return _media_error(
            "coverage_head_conflict",
            "Coverage Plan head changed; reload the exact current revision before retrying.",
            409,
            details={"current": exc.current} if exc.current is not None else None,
        )
    if isinstance(exc, CoverageReceiptConflictError):
        return _media_error(
            "coverage_idempotency_conflict",
            "This Coverage Idempotency-Key is permanently bound to a different request.",
            409,
        )
    if isinstance(exc, (CoverageIntegrityError, BlueprintIntegrityError)):
        return _media_error(
            "coverage_integrity_error",
            "Persisted Coverage Plan state failed integrity validation.",
            409,
        )
    raise exc


def _timeline_lowering_exception(exc: BaseException):
    if isinstance(exc, StrictJsonRequestError):
        return _media_error(exc.code, str(exc), 400)
    if isinstance(exc, TimelineLoweringServiceError):
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, CanonicalTimelineHeadConflictError):
        return _media_error(
            "timeline_manual_current_conflict",
            "A canonical Timeline became current and was preserved; reload before continuing.",
            409,
            details={"current": exc.current} if exc.current is not None else None,
        )
    if isinstance(exc, CanonicalTimelineReceiptConflictError):
        return _media_error(
            "timeline_idempotency_conflict",
            "This Timeline Idempotency-Key is permanently bound to a different request.",
            409,
        )
    if isinstance(exc, CanonicalTimelineSourceUnavailableError):
        code = (
            "coverage_not_lowerable"
            if exc.code == "coverage_not_lowerable"
            else "timeline_source_unavailable"
        )
        return _media_error(
            code,
            (
                "Coverage Plan cannot be lowered into a canonical first cut."
                if code == "coverage_not_lowerable"
                else "A selected Coverage assignment has no current exact source."
            ),
            422,
        )
    if isinstance(exc, TimelineLoweringContractError):
        return _media_error(
            "coverage_not_lowerable",
            "Coverage Plan cannot be lowered into a canonical first cut.",
            422,
            details={"errors": exc.errors},
        )
    if isinstance(exc, TimelineEditContractError):
        return _media_error(
            "invalid_timeline_edit",
            "Canonical Timeline edit is invalid.",
            422,
            details={"errors": exc.errors},
        )
    if isinstance(exc, TimelineStructuralEditContractError):
        return _media_error(
            "invalid_timeline_structural_edit",
            "Canonical Timeline structural edit is invalid.",
            422,
            details={"errors": exc.errors},
        )
    if isinstance(exc, TimelineRestoreContractError):
        code = (
            exc.errors[0]["code"]
            if exc.errors
            else "timeline_restore_target_invalid"
        )
        return _media_error(
            code,
            "Canonical Timeline restore is invalid.",
            409,
            details={"errors": exc.errors},
        )
    if isinstance(
        exc,
        (
            CanonicalTimelineIntegrityError,
            CoverageIntegrityError,
            BlueprintIntegrityError,
        ),
    ):
        return _media_error(
            "timeline_integrity_error",
            "Persisted canonical Timeline state failed integrity validation.",
            409,
        )
    raise exc


def _agent_timeline_exception(exc: BaseException):
    """Preserve Agent authority errors while exposing Timeline CAS vocabulary."""

    if isinstance(exc, CanonicalTimelineHeadConflictError):
        return _media_error(
            "timeline_head_conflict",
            "Canonical Timeline head changed; reload before retrying.",
            409,
            details={"current": exc.current} if exc.current is not None else None,
        )
    if isinstance(
        exc,
        (
            StrictJsonRequestError,
            TimelineLoweringServiceError,
            CanonicalTimelineReceiptConflictError,
            CanonicalTimelineSourceUnavailableError,
            TimelineLoweringContractError,
            TimelineEditContractError,
            TimelineStructuralEditContractError,
            TimelineRestoreContractError,
            CanonicalTimelineIntegrityError,
            CoverageIntegrityError,
            BlueprintIntegrityError,
        ),
    ):
        return _timeline_lowering_exception(exc)
    return _b1_exception(exc)


def _agent_preview_range_error(exc: AgentPreviewError):
    details = exc.details if isinstance(exc.details, Mapping) else {}
    mime_type = details.get("mime_type")
    opaque_etag = details.get("opaque_etag")
    response = Response(
        b"",
        status=416,
        mimetype=mime_type if mime_type == "video/mp4" else "video/mp4",
    )
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if (
        type(opaque_etag) is str
        and len(opaque_etag) == 64
        and all(character in "0123456789abcdef" for character in opaque_etag)
    ):
        response.headers["ETag"] = f'"{opaque_etag}"'
    source_size = details.get("source_size")
    if type(source_size) is int and source_size > 0:
        response.headers["Content-Range"] = f"bytes */{source_size}"
    return _private_media(response)


def _agent_preview_exception(exc: BaseException, *, media: bool = False):
    if isinstance(exc, AgentPreviewError):
        if media and exc.status == 416:
            return _agent_preview_range_error(exc)
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, StrictJsonRequestError):
        return _media_error(exc.code, str(exc), 400)
    if isinstance(exc, AgentProtocolError):
        code = (
            "agent_preview_rate_limited"
            if exc.code == "agent_pairing_rate_limited"
            else (
                "agent_preview_proof_invalid"
                if exc.code == "agent_pairing_proof_invalid"
                else "agent_preview_scope_denied"
            )
        )
        status = 429 if code == "agent_preview_rate_limited" else (
            401 if code == "agent_preview_proof_invalid" else 403
        )
        return _media_error(code, str(exc), status)
    if isinstance(exc, (BlueprintIntegrityError, CanonicalTimelineIntegrityError, CoverageIntegrityError)):
        return _media_error(
            "agent_preview_source_changed",
            "Canonical Timeline preview authority failed integrity validation.",
            409,
        )
    return _b1_exception(exc)


def _canonical_export_exception(exc: BaseException):
    if isinstance(exc, StrictJsonRequestError):
        return _media_error(exc.code, str(exc), 400)
    if isinstance(exc, AgentProtocolError):
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, CanonicalExportServiceError):
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, CanonicalExportReceiptConflictError):
        return _media_error(
            "canonical_export_idempotency_conflict",
            "This canonical Export Idempotency-Key is permanently bound to a different request.",
            409,
        )
    if isinstance(exc, CanonicalExportNativeNonceConflictError):
        return _media_error(
            "canonical_export_native_nonce_conflict",
            "This native export confirmation was already consumed.",
            409,
        )
    if isinstance(exc, CanonicalExportJobConflictError):
        code = str(exc) or "canonical_export_conflict"
        status = 404 if code == "canonical_export_job_missing" else 409
        return _media_error(
            code,
            (
                "Canonical export job does not exist."
                if status == 404
                else "Canonical export state changed; refresh the exact project before retrying."
            ),
            status,
        )
    if isinstance(
        exc,
        (
            CanonicalExportIntegrityError,
            CanonicalTimelineIntegrityError,
            CoverageIntegrityError,
            BlueprintIntegrityError,
        ),
    ):
        return _media_error(
            "canonical_export_integrity_error",
            "Persisted canonical Export or Usage state failed integrity validation.",
            409,
        )
    if isinstance(exc, ValueError):
        return _media_error(
            "invalid_canonical_export_request",
            "Canonical export request is invalid.",
            400,
        )
    raise exc


_B1_CORE_ERROR_STATUSES = {
    "agent_pairing_required": 401,
    "agent_pairing_proof_invalid": 401,
    "agent_pairing_pending": 409,
    "agent_pairing_denied": 403,
    "agent_pairing_expired": 403,
    "agent_pairing_revoked": 403,
    "agent_pairing_scope_denied": 403,
    "agent_pairing_rate_limited": 429,
    "agent_operation_nonce_invalid": 403,
    "agent_operation_request_mismatch": 409,
    "agent_confirmation_forbidden": 403,
    "native_confirmation_required": 403,
    "blueprint_confirmation_stale": 409,
    "invalid_blueprint_decision_units": 422,
    "blueprint_confirmation_not_active": 409,
    "blueprint_authority_idempotency_conflict": 409,
    "blueprint_authority_integrity_error": 409,
    "blueprint_authority_capacity_exceeded": 409,
    "blueprint_idempotency_conflict": 409,
    "blueprint_head_conflict": 409,
    "timeline_head_conflict": 409,
    "blueprint_not_found": 404,
    "canonical_timeline_not_found": 404,
    "timeline_idempotency_conflict": 409,
    "project_archived": 409,
    "invalid_blueprint_authority_operation": 422,
    "invalid_agent_pairing_request": 400,
    "invalid_idempotency_key": 400,
}


def _b1_exception(exc: BaseException):
    if isinstance(exc, (AgentProtocolError, StrictJsonRequestError)):
        if isinstance(exc, StrictJsonRequestError):
            return _media_error(exc.code, str(exc), 400)
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    if isinstance(exc, BlueprintIntegrityError) and "/blueprint/authority/" in request.path:
        return _media_error(
            "blueprint_authority_integrity_error",
            "Persisted Blueprint decision authority failed integrity validation.",
            409,
        )
    if isinstance(
        exc,
        (
            BlueprintServiceError,
            BlueprintHeadConflictError,
            BlueprintReceiptConflictError,
            BlueprintIntegrityError,
        ),
    ):
        return _blueprint_exception(exc)
    class_name = type(exc).__name__
    code = getattr(exc, "code", None)
    if class_name in {
        "AgentCapabilityError",
        "AgentCapabilityReceiptConflictError",
        "BlueprintAuthorityError",
        "BlueprintAuthorityReceiptConflictError",
    }:
        if class_name == "AgentCapabilityReceiptConflictError":
            if "timeline_idempotency" in str(exc):
                code = "timeline_idempotency_conflict"
            elif "blueprint_idempotency" in str(exc):
                code = "blueprint_idempotency_conflict"
            else:
                code = "agent_operation_request_mismatch"
        elif class_name == "BlueprintAuthorityReceiptConflictError":
            code = "blueprint_authority_idempotency_conflict"
        if type(code) is not str or code not in _B1_CORE_ERROR_STATUSES:
            code = (
                "blueprint_authority_integrity_error"
                if class_name.startswith("BlueprintAuthority")
                else "agent_pairing_scope_denied"
            )
        details = getattr(exc, "current", None)
        return _media_error(
            code,
            str(exc) or code,
            _B1_CORE_ERROR_STATUSES[code],
            details={"current": details} if details is not None else None,
        )
    raise exc


def _blueprint_command_response(result):
    response = jsonify(result.response)
    response.status_code = result.response_status
    response.headers["Idempotency-Replayed"] = "true" if result.replayed else "false"
    return response


def _public_job(job: dict[str, object], *, render: bool = False) -> dict[str, object]:
    if not render and job.get("kind") == "library_scan":
        raise LibraryScanContractError("library_scan_receipt_snapshot_required")
    hidden = {"database_uuid", "ffmpeg_command", "checkpoint", "stderr_tail"}
    value = {key: item for key, item in job.items() if key not in hidden}
    value["object"] = "render.job" if render else "media.job"
    value["schema_version"] = "1"
    if render and value.get("status") == "succeeded":
        value["download_url"] = f"/v1/renders/{value['id']}/download"
        value["artifact_url"] = value["download_url"]
        value["output"] = {
            "download_url": value["download_url"],
            "output_sha256": value.get("output_sha256"),
            "duration_ms": value.get("duration_ms"),
            "size_bytes": value.get("size_bytes"),
        }
    return value


def _receipt_anchored_public_job(
    repository,
    job: dict[str, object],
) -> dict[str, object]:
    if job.get("kind") != "library_scan":
        return _public_job(job)
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise LibraryScanContractError("library_scan_authority_invalid")
    snapshot = repository.get_library_scan_public_job(job_id)
    return public_library_scan_snapshot(snapshot)


def _library_scan_read_error():
    return _media_error(
        "library_scan_integrity_error",
        "Library scan state failed its receipt-bound integrity check.",
        409,
    )


def _private_media(response):
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    return response


def _stream_verified_handle(
    handle,
    size: int,
    *,
    mimetype: str,
    etag: str,
    filename: str | None = None,
):
    start, end = 0, size - 1
    status = 200
    range_header = request.headers.get("Range")
    if range_header:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match or (not match.group(1) and not match.group(2)):
            handle.close()
            response = _media_error("invalid_range", "Only one valid byte range is supported.", 416)[0]
            response.headers["Content-Range"] = f"bytes */{size}"
            return _private_media(response), 416
        if match.group(1):
            start = int(match.group(1))
            end = int(match.group(2)) if match.group(2) else size - 1
        else:
            suffix = int(match.group(2))
            start = max(0, size - suffix)
            end = size - 1
        if start >= size or end < start:
            handle.close()
            response = _media_error("range_not_satisfiable", "Requested byte range is unavailable.", 416)[0]
            response.headers["Content-Range"] = f"bytes */{size}"
            return _private_media(response), 416
        end = min(end, size - 1)
        status = 206
    length = end - start + 1

    def stream():
        remaining = length
        try:
            handle.seek(start)
            while remaining:
                chunk = handle.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            handle.close()

    response = Response(stream(), status=status, mimetype=mimetype, direct_passthrough=True)
    response.call_on_close(handle.close)
    response.headers["Accept-Ranges"] = "bytes"
    response.headers["Content-Length"] = str(length)
    response.headers["ETag"] = f'"{etag}"'
    if filename:
        response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    if status == 206:
        response.headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return _private_media(response)


SUPPORTED_LIBRARY_FILE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".bmp",
    ".gif",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
}
DEFAULT_PREVIEW_WIDTH = 1800
MIN_PREVIEW_WIDTH = 128
MAX_PREVIEW_WIDTH = 3200


def _error_response(message: str, status_code: int = 400):
    return (
        jsonify(
            {
                "object": "error",
                "message": message,
                "type": "invalid_request_error",
            }
        ),
        status_code,
    )


def _validate_existing_image_library(path: Path, *, field_name: str) -> None:
    if not path.exists() or not path.is_dir():
        raise ValueError(f"`{field_name}` must point to an existing directory.")


def _validate_db_path_for_settings(path: Path) -> None:
    if path.exists() and not path.is_file():
        raise ValueError("`db_path` must point to a SQLite file, not a directory.")


def _candidate_settings_for_update(
    settings: Settings,
    payload: dict[str, object],
) -> Settings:
    """Resolve a complete candidate without changing process or persisted state."""

    candidate = replace(
        settings,
        image_library_dir=Path(payload.get("image_library_dir", settings.image_library_dir)).resolve(),
        db_path=Path(payload.get("db_path", settings.db_path)).resolve(),
        process_image_width=int(payload.get("process_image_width", settings.process_image_width)),
    )
    if not ({"vision_profile_name", "query_profile_name"} & payload.keys()):
        return candidate

    config = _load_yaml(settings.config_path)
    vlm_config = config.get("vlm") if isinstance(config.get("vlm"), dict) else {}
    raw_profiles = vlm_config.get("profiles") if isinstance(vlm_config.get("profiles"), dict) else {}
    if "vision_profile_name" in payload:
        vision = _resolve_vlm_profile(
            raw_profiles=raw_profiles,
            profile_name=str(payload["vision_profile_name"]),
            role="vision",
            model_override_env="VISION_MODEL",
            legacy_model_override_env="VLM_MODEL",
            base_url_override_env="VISION_BASE_URL",
            legacy_base_url_override_env="OPENAI_BASE_URL",
        )
        candidate = replace(
            candidate,
            vision_base_url=vision.base_url,
            vision_api_key=vision.api_key,
            vision_model=vision.model,
            vision_provider=vision.provider,
            vision_profile_name=vision.name,
            vision_temperature=vision.temperature,
            vision_max_tokens=vision.max_tokens,
            vision_response_format=vision.response_format,
        )
    if "query_profile_name" in payload:
        query = _resolve_vlm_profile(
            raw_profiles=raw_profiles,
            profile_name=str(payload["query_profile_name"]),
            role="query",
            model_override_env="QUERY_MODEL",
            legacy_model_override_env=None,
            base_url_override_env="QUERY_BASE_URL",
            legacy_base_url_override_env=None,
        )
        candidate = replace(
            candidate,
            query_base_url=query.base_url,
            query_api_key=query.api_key,
            query_model=query.model,
            query_provider=query.provider,
            query_profile_name=query.name,
            query_temperature=query.temperature,
            query_max_tokens=query.max_tokens,
            query_response_format=query.response_format,
        )
    return candidate


def _settings_file_snapshot(path: Path) -> tuple[bool, bytes]:
    try:
        return True, path.read_bytes()
    except FileNotFoundError:
        return False, b""


def _save_settings_atomically(
    settings: Settings,
    payload: dict[str, object],
):
    merged = load_persisted_app_settings(settings.app_state_dir).to_dict()
    merged.update(payload)
    settings.app_state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".memolens-settings-commit-",
        dir=settings.app_state_dir,
    ) as temporary:
        temporary_state_dir = Path(temporary)
        persisted = save_persisted_app_settings(temporary_state_dir, merged)
        source_path = temporary_state_dir / settings.persisted_settings_path.name
        settings.persisted_settings_path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source_path, settings.persisted_settings_path)
    return persisted


def _restore_settings_file(path: Path, snapshot: tuple[bool, bytes]) -> None:
    existed, content = snapshot
    if not existed:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    rollback_path = path.with_name(f".{path.name}.rollback-{uuid.uuid4().hex}")
    rollback_path.write_bytes(content)
    os.replace(rollback_path, path)


def _resolve_existing_db_path(raw_value: str) -> Path:
    path = Path(raw_value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    if not path.is_file():
        raise ValueError("`db_path` must point to a SQLite file, not a directory.")
    return path


def _validate_indexing_authority_binding(
    indexing_request,
    settings,
    pinned_root: PinnedLibraryRoot,
) -> None:
    """Bind legacy indexing to the active root and writer database.

    This validation intentionally creates no directories and opens no files.
    The active runtime is the sole source of root/database authority.
    """

    active_root = Path(
        os.path.realpath(os.path.abspath(settings.image_library_dir.expanduser()))
    )
    requested_root = Path(
        os.path.realpath(
            os.path.abspath(Path(indexing_request.input.image_dir).expanduser())
        )
    )
    if requested_root != active_root or pinned_root.root_path != active_root:
        raise PermissionError("`image_dir` must match the active MemoLens library root.")

    active_db_path = _runtime_extension("image_index_repository").db_path.expanduser().resolve(
        strict=False
    )
    if indexing_request.db_path is not None:
        requested_db_path = Path(indexing_request.db_path).expanduser().resolve(strict=False)
        if requested_db_path != active_db_path:
            raise PermissionError("`db_path` must match the active MemoLens database.")

    for raw_file in indexing_request.input.files:
        candidate = Path(raw_file).expanduser()
        lexical_candidate = candidate if candidate.is_absolute() else active_root / candidate
        try:
            normalized_lexical = Path(os.path.abspath(lexical_candidate))
            lexical_relative = normalized_lexical.relative_to(active_root)
            if not lexical_relative.parts or any(
                part in {"", ".", ".."} for part in lexical_relative.parts
            ):
                raise ValueError("invalid relative source")
            file_fd = pinned_root.open_relative_regular(lexical_relative)
            os.close(file_fd)
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            raise PermissionError(
                "Indexing files must be existing sources inside the active library root."
            ) from exc


def _resolve_image_library_dir(
    *,
    settings,
    raw_value: object,
    allow_remote_override: bool,
) -> Path:
    if raw_value is not None and (not isinstance(raw_value, str) or not raw_value.strip()):
        raise ValueError("`image_library_dir` must be a non-empty string when set.")
    if not allow_remote_override and isinstance(raw_value, str):
        raise PermissionError("Path overrides are only available to local clients.")

    image_library_dir = (
        Path(raw_value).expanduser().resolve()
        if isinstance(raw_value, str) and raw_value.strip()
        else settings.image_library_dir.resolve()
    )
    if not image_library_dir.exists() or not image_library_dir.is_dir():
        raise FileNotFoundError(f"Image library directory does not exist: {image_library_dir}")
    return image_library_dir


def _library_root_fingerprint(root_path: Path, metadata: os.stat_result) -> str:
    """Match the identity material persisted by MediaRepository.

    The digest is recomputed from the already-open directory descriptor.  A
    path-level stat would reintroduce the replacement race this binding exists
    to close.
    """

    material = (
        f"{root_path}\0{metadata.st_dev}\0{metadata.st_ino}\0{metadata.st_mode}"
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _open_registered_library_root() -> PinnedLibraryRoot:
    """Open the active runtime root and bind it to its persisted identity."""

    settings = _runtime_settings()
    repository = _media_repository()
    try:
        pinned_root = open_pinned_library_root(settings.image_library_dir)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        abort(404)

    try:
        with repository.transaction() as connection:
            row = connection.execute(
                """SELECT permission_fingerprint,status FROM library_roots
                   WHERE canonical_path=?""",
                (str(pinned_root.root_path),),
            ).fetchone()
        observed_fingerprint = _library_root_fingerprint(
            pinned_root.root_path,
            pinned_root.metadata,
        )
        if (
            row is None
            or str(row["status"]) != "active"
            or not hmac.compare_digest(
                str(row["permission_fingerprint"]),
                observed_fingerprint,
            )
        ):
            abort(404)
        pinned_root.verify_current_path_identity()
        return pinned_root
    except Exception:
        pinned_root.close()
        raise


def _open_library_file(relative_path: str, *, root_path_override: str | None):
    if Path(relative_path).suffix.lower() not in SUPPORTED_LIBRARY_FILE_EXTENSIONS:
        abort(404)

    pinned_root = _open_registered_library_root()
    file_fd: int | None = None
    try:
        if root_path_override is not None:
            if not isinstance(root_path_override, str) or not root_path_override.strip():
                abort(404)
            try:
                requested_root = Path(root_path_override).expanduser().resolve(strict=True)
            except OSError:
                abort(404)
            if requested_root != pinned_root.root_path:
                abort(403)

        file_fd = pinned_root.open_relative_regular(Path(relative_path))
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            abort(404)
        identity = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(file_fd), "rb") as verification:
            for chunk in iter(lambda: verification.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(file_fd)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != identity:
            abort(404)
        pinned_root.verify_current_path_identity()
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle, metadata.st_size, digest.hexdigest()
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        abort(404)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        pinned_root.close()


def _parse_preview_width(raw_value: str | None) -> int:
    if raw_value is None or not raw_value.strip():
        return DEFAULT_PREVIEW_WIDTH
    try:
        parsed = int(raw_value)
    except ValueError:
        return DEFAULT_PREVIEW_WIDTH
    return max(MIN_PREVIEW_WIDTH, min(parsed, MAX_PREVIEW_WIDTH))


def _parse_copywriter_images(payload: object) -> list[RetrievedImageSummary]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("`images` must be a non-empty list.")

    parsed: list[RetrievedImageSummary] = []
    for item in payload[:12]:
        if not isinstance(item, dict):
            raise ValueError("`images` entries must be JSON objects.")

        relative_path = str(item.get("relative_path") or "").strip()
        filename = str(item.get("filename") or "").strip()
        description = str(item.get("description") or "").strip()
        if not relative_path or not filename:
            raise ValueError(
                "`images` entries must include `filename` and `relative_path`."
            )

        raw_tags = item.get("tags")
        tags = [str(tag).strip() for tag in raw_tags] if isinstance(raw_tags, list) else []
        parsed.append(
            RetrievedImageSummary(
                id=str(item.get("id") or relative_path),
                filename=filename,
                relative_path=relative_path,
                taken_at=str(item.get("taken_at") or "").strip() or None,
                place_name=str(item.get("place_name") or "").strip() or None,
                country=str(item.get("country") or "").strip() or None,
                description=description,
                tags=[tag for tag in tags if tag],
                score=float(item.get("score") or 0.0),
                matched_terms=[str(term).strip() for term in item.get("matched_terms", []) if str(term).strip()]
                if isinstance(item.get("matched_terms"), list)
                else [],
            )
        )

    return parsed


def _atlas_service_for_db_path(raw_db_path: object) -> PhotoAtlasService:
    if raw_db_path is None:
        return _runtime_extension("photo_atlas_service")
    if not isinstance(raw_db_path, str) or not raw_db_path.strip():
        raise ValueError("`db_path` must be a non-empty string when set.")
    requested = _resolve_existing_db_path(raw_db_path)
    active = _runtime_extension("image_index_repository").db_path.resolve(strict=True)
    if requested != active:
        raise ValueError("`db_path` must match the active MemoLens database.")
    return _runtime_extension("photo_atlas_service")


def _atlas_filters_from_mapping(payload: dict[str, object]) -> AtlasFilters:
    raw_asset_ids = payload.get("asset_ids")
    asset_ids: list[str] | None = None
    if isinstance(raw_asset_ids, list):
        asset_ids = [str(item).strip() for item in raw_asset_ids if isinstance(item, str) and item.strip()]

    return AtlasFilters(
        mode=normalize_mode(payload.get("mode") if isinstance(payload.get("mode"), str) else None),
        query=payload.get("text")
        if isinstance(payload.get("text"), str)
        else payload.get("query")
        if isinstance(payload.get("query"), str)
        else None,
        no_people=parse_bool(payload.get("no_people")),
        min_quality=parse_float(payload.get("min_quality")),
        show_duplicates=parse_bool(payload.get("show_duplicates")),
        limit=parse_limit(payload.get("limit")),
        cluster_id=payload.get("cluster_id")
        if isinstance(payload.get("cluster_id"), str) and payload.get("cluster_id")
        else None,
        asset_ids=asset_ids,
    )


def _string_list_from_payload(payload: dict[str, object], key: str) -> list[str]:
    raw_value = payload.get(key)
    if not isinstance(raw_value, list):
        return []
    return [str(item).strip() for item in raw_value if isinstance(item, str) and item.strip()]


def _health_value(value: object) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _healthz_v2():
    """Return a closed proof of the exact candidate or active runtime."""

    expected_token = str(current_app.config.get("DESKTOP_SESSION_TOKEN") or "")
    challenge = request.args.get("challenge", "")
    admitted_runtime = current_app.config.get("SQLITE_RUNTIME_CAPABILITY")
    if not isinstance(admitted_runtime, Mapping):
        admitted_runtime = {}
    public_runtime = {
        "policy_id": admitted_runtime.get("policy_id"),
        "sqlite_version": admitted_runtime.get("sqlite_version"),
        "wal_reset_safe": admitted_runtime.get("wal_reset_safe") is True,
        "journal_policy": admitted_runtime.get("journal_policy"),
    }
    identity = current_app.config.get("RUNTIME_HEALTH_IDENTITY")
    if not isinstance(identity, RuntimeHealthIdentity):
        return _media_error(
            "runtime_health_unavailable",
            "The exact MemoLens runtime identity is unavailable.",
            503,
        )
    runtime = identity.to_dict()
    proof = None
    if expected_token and re.fullmatch(r"[0-9a-f]{64}", challenge):
        proof_message = "\n".join(
            [
                "memolens.health.v2",
                f"challenge={challenge}",
                "object=health.check",
                "status=ok",
                f"service={MEMOLENS_SERVICE_ID}",
                f"api_version={MEMOLENS_API_VERSION}",
                "schema_version=2",
                f"policy_id={public_runtime['policy_id']}",
                f"sqlite_version={public_runtime['sqlite_version']}",
                "wal_reset_safe=true"
                if public_runtime["wal_reset_safe"]
                else "wal_reset_safe=false",
                f"journal_policy={public_runtime['journal_policy']}",
                f"runtime_mode={runtime['mode']}",
                "bootstrap_request_id="
                + _health_value(runtime["bootstrap_request_id"]),
                "candidate_binding_sha256="
                + _health_value(runtime["candidate_binding_sha256"]),
                f"database_uuid={_health_value(runtime['database_uuid'])}",
                "runtime_schema_version="
                + _health_value(runtime["schema_version"]),
                "runtime_generation_id="
                + _health_value(runtime["runtime_generation_id"]),
            ]
        )
        proof = hmac.new(
            expected_token.encode("utf-8"),
            proof_message.encode("utf-8"),
            "sha256",
        ).hexdigest()
    return jsonify(
        {
            "status": "ok",
            "object": "health.check",
            "service": MEMOLENS_SERVICE_ID,
            "api_version": MEMOLENS_API_VERSION,
            "schema_version": "2",
            "challenge_proof": proof,
            "sqlite_runtime": public_runtime,
            "runtime": runtime,
        }
    )


@api_blueprint.route("/healthz", methods=["GET"])
def healthz():
    if request.args.get("contract") == "2":
        return _healthz_v2()
    expected_token = str(current_app.config.get("DESKTOP_SESSION_TOKEN") or "")
    challenge = request.args.get("challenge", "")
    admitted_runtime = current_app.config.get("SQLITE_RUNTIME_CAPABILITY")
    if not isinstance(admitted_runtime, Mapping):
        admitted_runtime = {}
    public_runtime = {
        "policy_id": admitted_runtime.get("policy_id"),
        "sqlite_version": admitted_runtime.get("sqlite_version"),
        "wal_reset_safe": admitted_runtime.get("wal_reset_safe") is True,
        "journal_policy": admitted_runtime.get("journal_policy"),
    }
    proof = None
    if expected_token and re.fullmatch(r"[0-9a-f]{64}", challenge):
        proof_message = "\n".join(
            [
                "memolens.health.v1",
                f"challenge={challenge}",
                "object=health.check",
                "status=ok",
                f"service={MEMOLENS_SERVICE_ID}",
                f"api_version={MEMOLENS_API_VERSION}",
                f"policy_id={public_runtime['policy_id']}",
                f"sqlite_version={public_runtime['sqlite_version']}",
                "wal_reset_safe=true"
                if public_runtime["wal_reset_safe"]
                else "wal_reset_safe=false",
                f"journal_policy={public_runtime['journal_policy']}",
            ]
        )
        proof = hmac.new(
            expected_token.encode("utf-8"),
            proof_message.encode("utf-8"),
            "sha256",
        ).hexdigest()
    return jsonify(
        {
            "status": "ok",
            "object": "health.check",
            "service": MEMOLENS_SERVICE_ID,
            "api_version": MEMOLENS_API_VERSION,
            "challenge_proof": proof,
            "sqlite_runtime": public_runtime,
        }
    )


def _bootstrap_error(exc: LibraryBootstrapError):
    return _media_error(exc.code, str(exc), exc.status)


@api_blueprint.route("/v1/main/library-bootstrap", methods=["POST"])
def bootstrap_main_library():
    """Commit and activate one exact native-selected Library candidate."""

    binding = current_app.extensions.get("library_bootstrap_candidate_binding")
    if not isinstance(binding, BootstrapCandidateBinding):
        return _media_error(
            "library_bootstrap_candidate_unavailable",
            "The exact Library bootstrap candidate is unavailable.",
            503,
        )
    try:
        payload = strict_json_object(request)
        validate_bootstrap_command(
            payload,
            idempotency_key=request.headers.get(BOOTSTRAP_IDEMPOTENCY_HEADER, ""),
            binding=binding,
        )
        committed = commit_library_bootstrap(binding)
    except StrictJsonRequestError as exc:
        return _media_error(exc.code, str(exc), 400)
    except LibraryBootstrapError as exc:
        return _bootstrap_error(exc)
    except ValueError as exc:
        code = str(getattr(exc, "code", "library_bootstrap_request_conflict"))
        return _media_error(
            code,
            "The Library bootstrap request conflicts with permanent Core state.",
            409,
        )
    except (OSError, sqlite3.DatabaseError) as exc:
        current_app.logger.warning(
            "Library bootstrap Core storage is unavailable: %s",
            type(exc).__name__,
        )
        return _media_error(
            "library_bootstrap_storage_unavailable",
            "Core storage is unavailable for Library bootstrap.",
            503,
        )
    except RuntimeError as exc:
        code = str(getattr(exc, "code", "library_bootstrap_integrity_error"))
        current_app.logger.warning("Library bootstrap Core rejected persisted state: %s", code)
        return _media_error(
            code,
            "Core could not validate the persisted Library bootstrap state.",
            409,
        )

    identity = current_app.config.get("RUNTIME_HEALTH_IDENTITY")
    already_active = (
        isinstance(identity, RuntimeHealthIdentity)
        and identity.mode == "active"
        and identity.bootstrap_request_id == binding.request_id
        and identity.candidate_binding_sha256 == binding.candidate_binding_sha256
        and identity.database_uuid == committed.database_uuid
    )
    if not already_active:
        candidate_extensions: dict[str, object] | None = None
        try:
            binding.verify_current_identity()
            candidate_extensions = build_runtime_extensions(
                binding.settings,
                schema_prepared=True,
            )
            swap_runtime(current_app, binding.settings, candidate_extensions)
        except LibraryBootstrapError as exc:
            if candidate_extensions is not None:
                shutdown_runtime_extensions(candidate_extensions)
            return _bootstrap_error(exc)
        except Exception:
            if candidate_extensions is not None:
                shutdown_runtime_extensions(candidate_extensions)
            current_app.logger.exception(
                "Exact Library bootstrap runtime activation failed after Core commit"
            )
            return _media_error(
                "library_bootstrap_activation_unavailable",
                "Core committed the Library bootstrap, but the exact runtime is not active yet.",
                503,
            )

    response = jsonify(committed.response)
    response.status_code = committed.response_status
    if committed.replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return response


@api_blueprint.route("/v1/index/status", methods=["GET"])
def get_index_status():
    raw_db_path = request.args.get("db_path")
    if raw_db_path is None:
        repository = _runtime_extension("image_index_repository")
        resolved_db_path = repository.db_path.resolve()
    else:
        if not raw_db_path.strip():
            return _error_response("`db_path` must be a non-empty string when set.")
        try:
            resolved_db_path = _resolve_existing_db_path(raw_db_path)
        except (FileNotFoundError, ValueError) as exc:
            return _error_response(str(exc))
        repository = ImageIndexRepository(resolved_db_path)

    try:
        observed = os.stat(resolved_db_path, follow_symlinks=False)
        if not stat.S_ISREG(observed.st_mode):
            raise ImageIndexDatabaseScopeChangedError(
                ImageIndexDatabaseScopeChangedError.code
            )
        expected_file_identity = (int(observed.st_dev), int(observed.st_ino))
        if raw_db_path is None:
            media_repository = _runtime_extension_optional("media_repository")
            if media_repository is not None:
                expected_file_identity = (
                    media_repository.bind_image_database_identity()
                )
        index_stats = repository.summarize_index_health(
            expected_file_identity=expected_file_identity,
        )
    except (ImageIndexDatabaseScopeChangedError, ImageAnalysisPersistenceError) as exc:
        return _media_error(
            "image_index_database_scope_changed",
            str(exc),
            409,
        )
    except sqlite3.DatabaseError:
        return _error_response("`db_path` must point to a valid MemoLens SQLite database.")

    return jsonify(
        {
            "object": "image_index.status",
            "db_path": str(resolved_db_path),
            "index_stats": index_stats,
        }
    )


@api_blueprint.route("/v1/settings", methods=["GET"])
def get_settings():
    settings = _runtime_settings()
    persisted = load_persisted_app_settings(settings.app_state_dir)
    local_model_runtime = detect_local_model_runtime(settings.vlm_profile_catalog)
    try:
        expected_file_identity = _runtime_extension(
            "media_repository"
        ).bind_image_database_identity()
        index_stats = _runtime_extension(
            "image_index_repository"
        ).summarize_index_health(
            expected_file_identity=expected_file_identity,
        )
    except (ImageIndexDatabaseScopeChangedError, ImageAnalysisPersistenceError) as exc:
        return _media_error(
            "image_index_database_scope_changed",
            str(exc),
            409,
        )
    return jsonify(
        {
            "object": "memolens.settings",
            "effective": {
                "image_library_dir": str(settings.image_library_dir),
                "db_path": str(settings.db_path),
                "app_state_dir": str(settings.app_state_dir),
                "settings_path": str(settings.persisted_settings_path),
                "process_image_width": settings.process_image_width,
                "vision_profile_name": settings.vision_profile_name,
                "query_profile_name": settings.query_profile_name,
                "embedding_backend": settings.embedding_backend,
            },
            "persisted": persisted.to_dict(),
            "available_vlm_profiles": list(settings.available_vlm_profiles),
            "vlm_profile_catalog": [entry.to_dict() for entry in settings.vlm_profile_catalog],
            "local_model_runtime": local_model_runtime.to_dict(),
            "index_stats": index_stats,
        }
    )


@api_blueprint.route("/v1/settings", methods=["PUT"])
def update_settings():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return (
            jsonify(
                {
                    "object": "error",
                    "message": "Settings payload must be a JSON object.",
                    "type": "invalid_request_error",
                }
            ),
            400,
        )
    allowed_fields = frozenset(
        {
            "db_path",
            "image_library_dir",
            "process_image_width",
            "query_profile_name",
            "vision_profile_name",
        }
    )
    unknown_fields = sorted(set(payload) - allowed_fields)
    if unknown_fields:
        return _error_response(
            "Unsupported settings fields: " + ", ".join(unknown_fields) + "."
        )

    media_repository = _runtime_extension_optional("media_repository")
    if media_repository is not None and media_repository.active_job_count() > 0:
        return _media_error(
            "media_jobs_active",
            "Cancel or wait for active media jobs before changing MemoLens settings.",
            409,
        )

    settings = _runtime_settings()

    normalized_payload: dict[str, object] = {}

    for path_key in ("image_library_dir", "db_path"):
        value = payload.get(path_key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return _error_response(f"`{path_key}` must be a non-empty string when set.")
        resolved_path = Path(value).expanduser().resolve()
        try:
            if path_key == "image_library_dir":
                _validate_existing_image_library(resolved_path, field_name=path_key)
            else:
                _validate_db_path_for_settings(resolved_path)
        except ValueError as exc:
            return _error_response(str(exc))
        normalized_payload[path_key] = str(resolved_path)

    process_image_width = payload.get("process_image_width")
    if process_image_width is not None:
        if (
            isinstance(process_image_width, bool)
            or not isinstance(process_image_width, int)
            or process_image_width <= 0
        ):
            return _error_response("`process_image_width` must be a positive integer when set.")
        normalized_payload["process_image_width"] = process_image_width

    for profile_key in ("vision_profile_name", "query_profile_name"):
        value = payload.get(profile_key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return _error_response(f"`{profile_key}` must be a non-empty string when set.")
        if value.strip() not in settings.available_vlm_profiles:
            return _error_response(f"`{profile_key}` must be one of: " + ", ".join(settings.available_vlm_profiles))
        normalized_payload[profile_key] = value.strip()

    candidate_extensions: dict[str, object] | None = None
    try:
        candidate_settings = _candidate_settings_for_update(settings, normalized_payload)
        _validate_existing_image_library(
            candidate_settings.image_library_dir,
            field_name="image_library_dir",
        )
        candidate_extensions = build_runtime_extensions(candidate_settings)
        local_model_runtime = detect_local_model_runtime(candidate_settings.vlm_profile_catalog).to_dict()
    except sqlite3.DatabaseError:
        if candidate_extensions is not None:
            shutdown_runtime_extensions(candidate_extensions)
        return _error_response("`db_path` must point to a valid MemoLens SQLite database.")
    except (OSError, ValueError) as exc:
        if candidate_extensions is not None:
            shutdown_runtime_extensions(candidate_extensions)
        return _error_response(str(exc))
    except Exception:
        if candidate_extensions is not None:
            shutdown_runtime_extensions(candidate_extensions)
        return _media_error(
            "settings_preflight_failed",
            "MemoLens could not validate the requested runtime settings.",
            500,
        )

    try:
        settings_snapshot = _settings_file_snapshot(settings.persisted_settings_path)
    except OSError:
        shutdown_runtime_extensions(candidate_extensions)
        return _media_error(
            "settings_persist_failed",
            "MemoLens could not persist the requested settings; the previous runtime is still active.",
            500,
        )

    try:
        persisted = _save_settings_atomically(settings, normalized_payload)
    except ValueError as exc:
        shutdown_runtime_extensions(candidate_extensions)
        return _error_response(str(exc))
    except OSError:
        shutdown_runtime_extensions(candidate_extensions)
        return _media_error(
            "settings_persist_failed",
            "MemoLens could not persist the requested settings; the previous runtime is still active.",
            500,
        )

    try:
        swap_runtime(current_app, candidate_settings, candidate_extensions)
    except Exception:
        _restore_settings_file(settings.persisted_settings_path, settings_snapshot)
        shutdown_runtime_extensions(candidate_extensions)
        return _media_error(
            "settings_reload_failed",
            "MemoLens kept the previous settings because the runtime swap failed.",
            500,
        )

    return jsonify(
        {
            "object": "memolens.settings",
            "effective": {
                "image_library_dir": str(candidate_settings.image_library_dir),
                "db_path": str(candidate_settings.db_path),
                "app_state_dir": str(candidate_settings.app_state_dir),
                "settings_path": str(candidate_settings.persisted_settings_path),
                "process_image_width": candidate_settings.process_image_width,
                "vision_profile_name": candidate_settings.vision_profile_name,
                "query_profile_name": candidate_settings.query_profile_name,
                "embedding_backend": candidate_settings.embedding_backend,
            },
            "persisted": persisted.to_dict(),
            "available_vlm_profiles": list(candidate_settings.available_vlm_profiles),
            "vlm_profile_catalog": [entry.to_dict() for entry in candidate_settings.vlm_profile_catalog],
            "local_model_runtime": local_model_runtime,
        }
    )


def _legacy_indexing_compatibility_rejection(
    indexing_request,
    *,
    payload: Mapping[str, object],
    settings,
):
    # The legacy inline-image branch never creates a managed, reopenable source
    # identity.  It therefore cannot be translated into the canonical durable
    # image-analysis request without inventing authority for caller-owned
    # bytes.  Callers must first place the file under an approved Library root
    # and use its normalized relative path.
    if indexing_request.input.image is not None:
        return _media_error(
            "legacy_inline_image_unmanaged",
            "Inline legacy image payloads are not managed Library sources; import the file into the approved Library root first.",
            409,
        )
    if not indexing_request.persist_to_server:
        return _media_error(
            "legacy_nonpersistent_indexing_unsupported",
            "Legacy process-only indexing cannot be represented by the durable canonical image job.",
            409,
        )
    if indexing_request.limit is not None:
        return _media_error(
            "legacy_indexing_option_unsupported",
            "Legacy `limit` is not accepted by the canonical image import adapter; provide explicit `files` instead.",
            409,
        )
    supplied_model = payload.get("model")
    if supplied_model is not None and supplied_model != settings.vision_model:
        return _media_error(
            "legacy_indexing_option_unsupported",
            "Legacy model overrides cannot change the server-owned canonical image analysis profile.",
            409,
        )
    return None


def _submit_legacy_index_jobs(service, value: object) -> None:
    jobs = (
        [dict(job) for job in value if isinstance(job, dict)]
        if isinstance(value, list)
        else []
    )
    service.submit_jobs(jobs)


@api_blueprint.route("/v1/indexing/jobs", methods=["POST"])
def create_indexing_job():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response("Indexing payload must be a JSON object.")
    settings = _runtime_settings()
    include_records = payload.get("include_records", False)
    if not isinstance(include_records, bool):
        return (
            jsonify(
                {
                    "object": "error",
                    "message": "`include_records` must be a boolean.",
                    "type": "invalid_request_error",
                }
            ),
            400,
        )

    try:
        indexing_request = parse_indexing_request(
            payload=payload,
            default_image_dir=str(settings.image_library_dir),
            default_model=settings.vision_model,
        )
    except ValueError as exc:
        return (
            jsonify(
                {
                    "object": "error",
                    "message": str(exc),
                    "type": "invalid_request_error",
                }
            ),
            400,
        )

    rejected = _legacy_indexing_compatibility_rejection(
        indexing_request,
        payload=payload,
        settings=settings,
    )
    if rejected is not None:
        return rejected

    if indexing_request.library_root_identity is None:
        return _media_error(
            "indexing_authority_mismatch",
            "Local-file indexing requires the native-approved library root identity.",
            409,
        )

    # Old clients did not send an Idempotency-Key.  Derive one from the closed
    # request in that case so an HTTP retry cannot mint a second run/attempt.
    # This replay check deliberately precedes fallible filesystem preparation:
    # a lost HTTP response remains recoverable even if the source later moves.
    request_sha256 = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    raw_idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if len(raw_idempotency_key) > 200:
        return _media_error(
            "invalid_idempotency_key",
            "A bounded `Idempotency-Key` header is required when set.",
            400,
        )
    idempotency_key = raw_idempotency_key or f"legacy-index-{request_sha256}"
    idempotency_scope = "desktop:POST:/v1/indexing/jobs"
    operation_material = (
        f"{idempotency_scope}\0{idempotency_key}\0{request_sha256}"
    )
    operation_id = (
        "legacy_idx_"
        f"{hashlib.sha256(operation_material.encode()).hexdigest()[:24]}"
    )
    repository = _media_repository()
    media_job_runner = _runtime_extension("media_job_runner")
    service = MediaImportService(
        repository,
        media_job_runner,
    )
    try:
        replay = repository.replay_idempotent_write(
            scope=idempotency_scope,
            key=idempotency_key,
            request_sha256=request_sha256,
        )
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    if replay is not None:
        frozen_jobs = replay.response.get("jobs")
        _submit_legacy_index_jobs(service, frozen_jobs)
        response = jsonify(replay.response)
        response.status_code = replay.response_status
        response.headers["Idempotency-Replayed"] = "true"
        return response

    pinned_root: PinnedLibraryRoot | None = None
    relative_paths: list[str] | None = None
    try:
        expected_identity = (
            None
            if indexing_request.library_root_identity is None
            else (
                indexing_request.library_root_identity.device,
                indexing_request.library_root_identity.inode,
            )
        )
        pinned_root = open_pinned_library_root(
            Path(indexing_request.input.image_dir),
            expected_identity=expected_identity,
        )
        _validate_indexing_authority_binding(indexing_request, settings, pinned_root)
        if indexing_request.input.files:
            relative_paths = []
            for raw_file in indexing_request.input.files:
                candidate = Path(raw_file).expanduser()
                lexical_candidate = (
                    candidate if candidate.is_absolute() else pinned_root.root_path / candidate
                )
                normalized = Path(os.path.abspath(lexical_candidate))
                relative_paths.append(
                    normalized.relative_to(pinned_root.root_path).as_posix()
                )
    except PermissionError as exc:
        return _media_error("indexing_authority_mismatch", str(exc), 409)
    except (FileNotFoundError, RuntimeError, ValueError, OSError) as exc:
        return _error_response(str(exc))

    finally:
        if pinned_root is not None:
            pinned_root.close()

    try:
        root_id = repository._root_id(settings.image_library_dir.resolve(strict=True))
        root_record = repository.library_root(root_id)
        if root_record is None or root_record.get("status") != "active":
            return _media_error(
                "indexing_authority_mismatch",
                "The active Library root is not registered as an approved managed source.",
                409,
            )
        root = repository.validate_library_root(root_id)
        import_payload: dict[str, object] = {
            "relative_paths": relative_paths,
            "recursive": indexing_request.input.recursive,
            "dry_run": False,
            "kinds": ["image"],
        }
        # ``None`` means scan the approved root; the canonical import parser
        # intentionally rejects a literal null relative_paths field.
        if relative_paths is None:
            import_payload.pop("relative_paths")
        plan = service.prepare_import(
            root_id=root_id,
            root=root,
            payload=import_payload,
        )

        image_enqueue = None
        image_authority = None
        if plan.has_enqueueable_image:
            lease = getattr(g, "memolens_runtime_lease", None)
            if not isinstance(lease, RuntimeLease):
                plan.close()
                raise RuntimeError(
                    "Legacy image import requires one pinned MemoLens runtime generation."
                )
            candidate = getattr(
                repository,
                "enqueue_image_analysis_in_transaction",
                None,
            )
            if not callable(candidate):
                plan.close()
                raise RuntimeError(
                    "Canonical image transaction enqueue is unavailable."
                )
            image_enqueue = candidate
            image_authority = ImageImportEnqueueAuthority(
                runtime_generation=lease.generation_id,
                database_file_identity=repository.bind_image_database_identity(),
                operation_id=operation_id,
            )

        def apply_legacy_import(connection: sqlite3.Connection):
            if image_enqueue is not None and image_authority is not None:
                result = service.apply_prepared(
                    connection,
                    root_id=root_id,
                    plan=plan,
                    image_enqueue=image_enqueue,
                    image_authority=image_authority,
                    retain_source_bindings=True,
                )
            else:
                result = service.apply_prepared(
                    connection,
                    root_id=root_id,
                    plan=plan,
                    retain_source_bindings=True,
                )
            jobs = [_public_job(job) for job in result.jobs]
            queued_asset_ids = {
                str(job.get("asset_id") or "") for job in jobs if job.get("asset_id")
            }
            queued = [
                {
                    "object": "image_index_record",
                    "id": str(asset.get("id") or ""),
                    "filename": str(asset.get("filename") or ""),
                    "relative_path": str(asset.get("relative_path") or ""),
                    "status": "queued",
                    "tags": [],
                    "description": None,
                    "taken_at": None,
                    "place_name": None,
                    "country": None,
                    "message": "Canonical image analysis is queued.",
                }
                for asset in result.assets
                if str(asset.get("id") or "") in queued_asset_ids
            ]
            errors = [
                {
                    "object": "image_index_record",
                    "id": (
                        "failed_"
                        + hashlib.sha256(
                            str(item.get("relative_path") or "").encode()
                        ).hexdigest()[:16]
                    ),
                    "filename": Path(
                        str(item.get("relative_path") or "unknown")
                    ).name,
                    "relative_path": str(item.get("relative_path") or ""),
                    "status": "failed",
                    "tags": [],
                    "description": None,
                    "taken_at": None,
                    "place_name": None,
                    "country": None,
                    "message": str(item.get("message") or item.get("code") or "Import rejected."),
                    "code": str(item.get("code") or "import_rejected"),
                }
                for item in result.rejected
            ]
            if jobs:
                status = "queued"
                message = (
                    "Canonical image analysis queued with some rejected sources."
                    if errors
                    else "Canonical image analysis queued."
                )
                response_status = 202
            elif errors:
                status = "failed"
                message = "All candidate images were rejected before enqueue."
                response_status = 200
            else:
                status = "empty"
                message = "No supported managed images were found to enqueue."
                response_status = 200
            response_body: dict[str, object] = {
                "id": operation_id,
                "object": "image_index.job",
                "created": int(datetime.now(timezone.utc).timestamp()),
                "status": status,
                "message": message,
                "model": settings.vision_model,
                "data": [],
                "queued": queued,
                "skipped": [],
                "errors": errors,
                "jobs": jobs,
                "job": jobs[0] if jobs else None,
                "job_id": jobs[0]["id"] if jobs else None,
                "meta": {
                    "image_dir": str(root),
                    "db_path": str(repository.db_path),
                    "embedding_backend": settings.embedding_backend,
                    "indexed_count": 0,
                    "queued_count": len(queued),
                    "skipped_count": 0,
                    "error_count": len(errors),
                },
                "external_analysis": False,
            }
            if include_records:
                response_body["records"] = []
            return response_body, response_status, operation_id

        try:
            committed = repository.execute_idempotent_write(
                scope=idempotency_scope,
                key=idempotency_key,
                request_sha256=request_sha256,
                resource_type="legacy_image_index_import",
                mutation=apply_legacy_import,
            )
        finally:
            plan.close()
        frozen_jobs = committed.response.get("jobs")
        _submit_legacy_index_jobs(service, frozen_jobs)
        response = jsonify(committed.response)
        response.status_code = committed.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if committed.replayed else "false"
        )
        return response
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _media_error("invalid_import", str(exc), 400)
    except RuntimeError as exc:
        return _media_error(
            "image_analysis_enqueue_unavailable",
            str(exc),
            503,
        )


@api_blueprint.route("/v1/retrieval/query", methods=["POST"])
def create_retrieval_query():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response("Retrieval payload must be a JSON object.")
    settings = _runtime_settings()
    include_copy = payload.get("include_copy", True)

    if not isinstance(include_copy, bool):
        return _error_response("`include_copy` must be a boolean when set.")

    try:
        retrieval_request = parse_retrieval_request(payload)
    except ValueError as exc:
        return _error_response(str(exc))
    if retrieval_request.top_k is not None and retrieval_request.top_k > 512:
        return _error_response("`top_k` must be at most 512.")

    db_path_override = payload.get("db_path")
    image_library_dir_override = payload.get("image_library_dir")

    if db_path_override is not None and (not isinstance(db_path_override, str) or not db_path_override.strip()):
        return _error_response("`db_path` must be a non-empty string when set.")
    try:
        image_library_dir = _resolve_image_library_dir(
            settings=settings,
            raw_value=image_library_dir_override,
            allow_remote_override=_request_is_local(),
        )
    except PermissionError as exc:
        return _local_only_error(str(exc))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))

    if isinstance(db_path_override, str) and db_path_override.strip():
        try:
            requested_db_path = _resolve_existing_db_path(db_path_override)
        except (FileNotFoundError, ValueError) as exc:
            return _error_response(str(exc))
        active_db_path = _runtime_extension("image_index_repository").db_path.resolve(strict=True)
        if requested_db_path != active_db_path:
            return _media_error(
                "database_binding_mismatch",
                "`db_path` must match the active MemoLens database.",
                409,
            )

    retrieval_service = _runtime_extension("retrieval_service")
    if not _legacy_provider_egress_authorized():
        local_settings = replace(
            settings,
            embedding_backend="semantic_hash",
            text_embedding_model_id="semantic-hash-v1",
        )
        local_text_embedding_service = _runtime_extension(
            "text_embedding_service"
        ).__class__(local_settings)
        retrieval_service = retrieval_service.__class__(
            settings=local_settings,
            repository=_runtime_extension("image_index_repository"),
            planner=_DeterministicLocalPlanner(_runtime_extension("query_planner")),
            text_embedding_service=local_text_embedding_service,
        )

    copywriter = _runtime_extension("retrieval_copywriter")
    result = retrieval_service.run(retrieval_request)
    body = result.to_response()
    result_items = body.get("data")
    if (
        not isinstance(result_items, list)
        or len(result_items) != len(result.data)
        or len(result_items) > 512
    ):
        return _media_error(
            "canonical_image_retrieval_invalid",
            "Retrieval did not produce a closed result set.",
            409,
        )
    if result_items:
        projection_bindings = getattr(
            result,
            "canonical_projection_bindings",
            None,
        )
        consumer_state_reader = getattr(
            _media_repository(),
            "canonical_image_consumer_states",
            None,
        )
        if not isinstance(projection_bindings, dict) or not callable(
            consumer_state_reader
        ):
            return _media_error(
                "canonical_image_retrieval_unavailable",
                "Verified canonical image retrieval is unavailable.",
                409,
            )
        asset_ids = [item.id for item in result.data]
        if len(set(asset_ids)) != len(asset_ids) or any(
            re.fullmatch(r"asset_[0-9a-f]{24}", asset_id) is None
            for asset_id in asset_ids
        ):
            return _media_error(
                "canonical_image_retrieval_invalid",
                "Retrieval did not produce canonical image identities.",
                409,
            )
        consumer_states = consumer_state_reader(asset_ids)
        if not isinstance(consumer_states, dict):
            return _media_error(
                "canonical_image_retrieval_unavailable",
                "Verified canonical image retrieval is unavailable.",
                409,
            )
        for item, serialized in zip(result.data, result_items, strict=True):
            expected = projection_bindings.get(item.id)
            state = consumer_states.get(item.id)
            if (
                not isinstance(serialized, dict)
                or serialized.get("id") != item.id
                or not isinstance(expected, dict)
                or not isinstance(state, dict)
                or state.get("analysis_status") != "current"
            ):
                return _media_error(
                    "canonical_image_projection_changed",
                    "Canonical image projection changed during retrieval; retry the query.",
                    409,
                )
            observation = state.get("canonical_image_observation")
            if not is_verified_current_canonical_image_observation(
                observation,
                asset_id=item.id,
            ):
                return _media_error(
                    "canonical_image_projection_changed",
                    "Canonical image projection changed during retrieval; retry the query.",
                    409,
                )
            assert isinstance(observation, dict)
            observation_projection = observation["projection"]
            if (
                observation["analysis_binding"] != expected.get("analysis_binding")
                or not isinstance(observation_projection, dict)
                or observation_projection.get("generation_id")
                != expected.get("generation_id")
                or observation_projection.get("row_sha256")
                != expected.get("row_sha256")
            ):
                return _media_error(
                    "canonical_image_projection_changed",
                    "Canonical image projection changed during retrieval; retry the query.",
                    409,
                )
            serialized["asset_id"] = item.id
            serialized["analysis_status"] = "current"
            serialized["analysis_binding"] = observation["analysis_binding"]
            serialized["projection"] = observation_projection
            serialized["canonical_image_observation"] = observation
    body["candidate_count"] = len(result.data)

    if include_copy and result.status == "completed" and result.data:
        try:
            if _legacy_provider_egress_authorized():
                generated_copy = copywriter.generate(
                    query_text=result.query_text,
                    retrieved_images=result.data,
                    image_library_dir=image_library_dir,
                    # Compose from text already stored in the index. Retrieval
                    # must never upload original photo bytes to a copy provider.
                    image_limit=0,
                )
            else:
                generated_copy = _deterministic_local_copy(
                    copywriter,
                    query_text=result.query_text,
                    retrieved_images=result.data,
                )
            body["generated_copy"] = generated_copy.to_dict()
            body["title"] = generated_copy.title
            body["caption"] = generated_copy.body
            body["notes"] = generated_copy.highlights
        except Exception as exc:
            body["generated_copy"] = None
            body["copywriting_error"] = str(exc)
    else:
        body["generated_copy"] = None

    return jsonify(body)


@api_blueprint.route("/v1/retrieval/copy", methods=["POST"])
def create_retrieval_copy():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    settings = _runtime_settings()
    query_text = payload.get("query_text")

    if not isinstance(query_text, str) or not query_text.strip():
        return _error_response("`query_text` must be a non-empty string.")

    try:
        image_library_dir = _resolve_image_library_dir(
            settings=settings,
            raw_value=payload.get("image_library_dir"),
            allow_remote_override=True,
        )
        retrieved_images = _parse_copywriter_images(payload.get("images"))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))

    copywriter = _runtime_extension("retrieval_copywriter")
    try:
        if _legacy_provider_egress_authorized():
            generated_copy = copywriter.generate(
                query_text=query_text.strip(),
                retrieved_images=retrieved_images,
                image_library_dir=image_library_dir,
                # This endpoint is a text-only composition boundary. Photo bytes
                # may leave the device only during an explicit indexing request.
                image_limit=0,
            )
        else:
            generated_copy = _deterministic_local_copy(
                copywriter,
                query_text=query_text.strip(),
                retrieved_images=retrieved_images,
            )
    except Exception as exc:
        return (
            jsonify(
                {
                    "object": "error",
                    "message": str(exc),
                    "type": "copywriting_error",
                }
            ),
            500,
        )

    return jsonify(
        {
            "object": "generated_copy",
            "generated_copy": generated_copy.to_dict(),
            "title": generated_copy.title,
            "caption": generated_copy.body,
            "notes": generated_copy.highlights,
        }
    )


@api_blueprint.route("/v1/inspiration/generate", methods=["POST"])
def generate_search_inspiration():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Inspiration payload must be a JSON object.")

    raw_count = payload.get("count", 5)
    count = raw_count if isinstance(raw_count, int) else 5
    count = max(3, min(count, 8))
    context_asset_ids = _string_list_from_payload(payload, "context_asset_ids")[:24]

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        workbench = service.workbench(
            lens="explore",
            show_duplicates=True,
            limit=900,
        )
        library_summary = workbench.get("library_summary")
        memories = workbench.get("memories")
        if not isinstance(library_summary, dict):
            library_summary = {}
        if not isinstance(memories, list):
            memories = []
        context_assets = service.assets_by_ids(context_asset_ids) if context_asset_ids else []
        planner = _runtime_extension("query_planner")
        if not _legacy_provider_egress_authorized():
            planner = _DeterministicLocalPlanner(planner)
        suggestions = planner.generate_search_suggestions(
            library_summary=library_summary,
            memories=[memory for memory in memories if isinstance(memory, dict)],
            context_assets=context_assets,
            count=count,
        )
    except AtlasProjectionNotReadyError as exc:
        return _atlas_projection_not_ready(exc)
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    except Exception as exc:
        return (
            jsonify(
                {
                    "object": "error",
                    "message": str(exc),
                    "type": "inspiration_error",
                }
            ),
            500,
        )

    return jsonify(
        {
            "object": "inspiration.suggestions",
            "status": "completed",
            "suggestions": suggestions[:count],
            "source": "query_profile",
        }
    )


@api_blueprint.route("/v1/atlas/status", methods=["GET"])
def get_atlas_status():
    if not _request_is_local():
        return _local_only_error()

    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(service.status())


@api_blueprint.route("/v1/atlas/rebuild", methods=["POST"])
def rebuild_atlas():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas rebuild payload must be a JSON object.")
    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.rebuild()
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    except Exception as exc:
        return (
            jsonify(
                {
                    "object": "error",
                    "message": str(exc),
                    "type": "atlas_rebuild_error",
                }
            ),
            500,
        )
    return jsonify(result)


@api_blueprint.route("/v1/atlas/overview", methods=["GET"])
def get_atlas_overview():
    if not _request_is_local():
        return _local_only_error()

    query_payload: dict[str, object] = {
        "mode": request.args.get("mode"),
        "query": request.args.get("query"),
        "no_people": request.args.get("no_people"),
        "min_quality": request.args.get("min_quality"),
        "show_duplicates": request.args.get("show_duplicates"),
        "limit": request.args.get("limit"),
        "cluster_id": request.args.get("cluster_id"),
    }
    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
        result = service.overview(_atlas_filters_from_mapping(query_payload))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/workbench", methods=["GET"])
def get_atlas_workbench():
    if not _request_is_local():
        return _local_only_error()

    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
        result = service.workbench(
            lens=normalize_lens(request.args.get("lens")),
            query=request.args.get("query"),
            no_people=parse_bool(request.args.get("no_people")),
            min_quality=parse_float(request.args.get("min_quality")),
            show_duplicates=parse_bool(request.args.get("show_duplicates")),
            limit=parse_limit(request.args.get("limit")),
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/memory/<memory_id>", methods=["GET"])
def get_atlas_memory(memory_id: str):
    if not _request_is_local():
        return _local_only_error()

    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
        result = service.memory(memory_id)
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc), 404 if "does not exist" in str(exc) else 400)
    return jsonify(result)


@api_blueprint.route("/v1/atlas/cleanup", methods=["GET"])
def get_atlas_cleanup():
    if not _request_is_local():
        return _local_only_error()

    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
        result = service.cleanup()
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/search", methods=["POST"])
def search_atlas():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas search payload must be a JSON object.")
    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.overview(_atlas_filters_from_mapping(payload))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify({**result, "object": "atlas.search"})


@api_blueprint.route("/v1/atlas/select", methods=["POST"])
def select_atlas_assets():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas select payload must be a JSON object.")
    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.overview(_atlas_filters_from_mapping(payload))
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify({**result, "object": "atlas.selection"})


@api_blueprint.route("/v1/atlas/query-preview", methods=["POST"])
def preview_atlas_query():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas query preview payload must be a JSON object.")
    text = payload.get("text") if isinstance(payload.get("text"), str) else payload.get("query_text")
    if not isinstance(text, str) or not text.strip():
        return _error_response("`text` must be a non-empty string.")

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.query_preview(
            text=text.strip(),
            lens=normalize_lens(payload.get("lens") if isinstance(payload.get("lens"), str) else None),
            no_people=parse_bool(payload.get("no_people")),
            min_quality=parse_float(payload.get("min_quality")),
            show_duplicates=parse_bool(payload.get("show_duplicates")),
            limit=parse_limit(payload.get("limit")),
            selected_memory_ids=_string_list_from_payload(payload, "selected_memory_ids"),
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/feedback", methods=["POST"])
def create_atlas_feedback():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas feedback payload must be a JSON object.")

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.record_feedback(
            target_kind=str(payload.get("target_kind") or "asset"),
            target_id=str(payload.get("target_id") or ""),
            action=str(payload.get("action") or ""),
            weight=float(payload.get("weight") or 1.0),
            note=payload.get("note") if isinstance(payload.get("note"), str) else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/basket", methods=["GET"])
def get_atlas_basket():
    if not _request_is_local():
        return _local_only_error()

    try:
        service = _atlas_service_for_db_path(request.args.get("db_path"))
        result = service.load_basket()
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/basket", methods=["POST"])
def save_atlas_basket():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas basket payload must be a JSON object.")
    raw_asset_ids = payload.get("asset_ids")
    if not isinstance(raw_asset_ids, list):
        return _error_response("`asset_ids` must be a list.")
    asset_ids = [str(item).strip() for item in raw_asset_ids if isinstance(item, str) and item.strip()]

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.save_basket(
            asset_ids=asset_ids,
            name=payload.get("name") if isinstance(payload.get("name"), str) else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/stack/action", methods=["POST"])
def create_atlas_stack_action():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas stack action payload must be a JSON object.")
    stack_id = payload.get("stack_id")
    action = payload.get("action")
    if not isinstance(stack_id, str) or not stack_id.strip():
        return _error_response("`stack_id` must be a non-empty string.")
    if not isinstance(action, str) or not action.strip():
        return _error_response("`action` must be a non-empty string.")

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.stack_action(
            stack_id=stack_id.strip(),
            action=action.strip(),
            keep_asset_id=payload.get("keep_asset_id") if isinstance(payload.get("keep_asset_id"), str) else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))
    return jsonify(result)


@api_blueprint.route("/v1/atlas/generate", methods=["POST"])
def generate_from_atlas():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return _error_response("Atlas generate payload must be a JSON object.")
    text = payload.get("text") if isinstance(payload.get("text"), str) else payload.get("query_text")
    if not isinstance(text, str) or not text.strip():
        return _error_response("`text` must be a non-empty string.")
    top_k = payload.get("top_k", 9)
    if not isinstance(top_k, int) or top_k <= 0:
        return _error_response("`top_k` must be a positive integer.")

    if "asset_ids" in payload:
        raw_asset_ids = payload.get("asset_ids")
        if not isinstance(raw_asset_ids, list):
            raise AtlasAssetObservationConflictError("asset_ids_invalid")
        asset_ids = list(raw_asset_ids)
    else:
        asset_ids = None
    if "asset_observations" in payload:
        raw_asset_observations = payload.get("asset_observations")
        if not isinstance(raw_asset_observations, list):
            raise AtlasAssetObservationConflictError(
                "asset_observations_invalid"
            )
        asset_observations = list(raw_asset_observations)
    else:
        asset_observations = None
    validate_atlas_asset_observation_request(
        asset_ids,
        asset_observations,
    )
    selected_memory_ids = _string_list_from_payload(payload, "selected_memory_ids")

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        result = service.generate(
            text=text.strip(),
            top_k=top_k,
            mode=normalize_mode(payload.get("mode") if isinstance(payload.get("mode"), str) else None),
            cluster_id=payload.get("cluster_id") if isinstance(payload.get("cluster_id"), str) else None,
            asset_ids=asset_ids,
            asset_observations=asset_observations,
            selected_memory_ids=selected_memory_ids,
            no_people=parse_bool(payload.get("no_people")),
            min_quality=parse_float(payload.get("min_quality")),
            show_duplicates=parse_bool(payload.get("show_duplicates")),
        )
    except (FileNotFoundError, ValueError) as exc:
        return _error_response(str(exc))

    result["candidate_count"] = result.get("candidate_count", len(result.get("data", [])))
    include_copy = payload.get("include_copy", False)
    if not isinstance(include_copy, bool):
        return _error_response("`include_copy` must be a boolean when set.")

    if include_copy and result.get("data"):
        settings = _runtime_settings()
        try:
            image_library_dir = _resolve_image_library_dir(
                settings=settings,
                raw_value=payload.get("image_library_dir"),
                allow_remote_override=True,
            )
            copywriter = _runtime_extension("retrieval_copywriter")
            retrieved_images = _parse_copywriter_images(result.get("data"))
            if _legacy_provider_egress_authorized():
                generated_copy = copywriter.generate(
                    query_text=text.strip(),
                    retrieved_images=retrieved_images,
                    image_library_dir=image_library_dir,
                    image_limit=0,
                )
            else:
                generated_copy = _deterministic_local_copy(
                    copywriter,
                    query_text=text.strip(),
                    retrieved_images=retrieved_images,
                )
            result["generated_copy"] = generated_copy.to_dict()
            result["title"] = generated_copy.title
            result["caption"] = generated_copy.body
            result["notes"] = generated_copy.highlights
        except Exception as exc:
            result["generated_copy"] = None
            result["copywriting_error"] = str(exc)
    else:
        result["generated_copy"] = None

    return jsonify(result)


@api_blueprint.route("/v1/library/files/<path:relative_path>", methods=["GET"])
def get_library_file(relative_path: str):
    if not _request_is_local() or request.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
        return _local_only_error()
    denied = _require_media_desktop_token()
    if denied:
        return denied

    handle, size, digest = _open_library_file(
        relative_path,
        root_path_override=request.args.get("root_path"),
    )
    return _stream_verified_handle(
        handle,
        size,
        mimetype=mimetypes.guess_type(relative_path)[0] or "application/octet-stream",
        etag=digest,
    )


@api_blueprint.route("/v1/library/previews/<path:relative_path>", methods=["GET"])
def get_library_preview(relative_path: str):
    if not _request_is_local() or request.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
        return _local_only_error()

    handle, _, _ = _open_library_file(
        relative_path,
        root_path_override=request.args.get("root_path"),
    )
    preview_width = _parse_preview_width(request.args.get("width"))

    try:
        with Image.open(handle) as source:
            try:
                source.seek(0)
            except EOFError:
                pass
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (OSError, UnidentifiedImageError):
        abort(415)
    finally:
        handle.close()

    if image.width > preview_width:
        target_height = max(1, round(image.height * preview_width / image.width))
        image = image.resize(
            (preview_width, target_height),
            getattr(Image, "Resampling", Image).LANCZOS,
        )

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    buffer.seek(0)

    response = send_file(
        buffer,
        mimetype="image/jpeg",
        conditional=False,
    )
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


# Video Creative Workbench -------------------------------------------------


@api_blueprint.route("/v1/inbox", methods=["GET"])
def get_media_inbox():
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        limit = int(request.args.get("limit", "50"))
        page = _runtime_extension("media_inbox_service").list_assets(
            state=request.args.get("state", "inbox"),
            kinds=request.args.get("kinds"),
            limit=limit,
            cursor=request.args.get("cursor"),
        )
    except (TypeError, ValueError) as exc:
        return _media_error("invalid_inbox_query", str(exc), 400)
    return jsonify(
        {
            "object": "media.inbox",
            "schema_version": "1",
            "data": page.items,
            "items": page.items,
            "summary": page.summary,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }
    )


@api_blueprint.route("/v1/inbox/assets/<asset_id>", methods=["PUT"])
def update_media_inbox_asset(asset_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"PUT:/v1/inbox/assets/{asset_id}"
    try:
        payload = _json_object()
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        review, _ = _runtime_extension("media_inbox_service").update_review(
            asset_id,
            payload,
            idempotency_scope=idempotency_scope,
            idempotency_key=key,
            request_sha256=request_hash,
        )
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except ReviewRevisionConflictError as exc:
        return _media_error(
            "review_revision_conflict",
            str(exc),
            409,
            details={"current_review": exc.current_review},
        )
    except InboxAssetNotFoundError as exc:
        return _media_error("asset_not_found", str(exc), 404)
    except ValueError as exc:
        return _media_error("invalid_asset_review", str(exc), 400)
    return jsonify(
        {
            "object": "asset.review",
            "schema_version": "1",
            "asset_id": asset_id,
            "review": review,
        }
    )


@api_blueprint.route("/v1/creator/profile", methods=["GET"])
def get_creator_profile():
    denied = _require_media_read_access()
    if denied:
        return denied
    profile = _runtime_extension("creator_memory_service").current_profile()
    return jsonify(
        {
            "object": "creator.profile",
            "schema_version": "1",
            "profile": profile,
        }
    )


@api_blueprint.route("/v1/creator/profile", methods=["PUT"])
def update_creator_profile():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "PUT:/v1/creator/profile"
    try:
        payload = _json_object()
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        profile, _ = _runtime_extension("creator_memory_service").update_profile(
            payload,
            idempotency_scope=idempotency_scope,
            idempotency_key=key,
            request_sha256=request_hash,
        )
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except ProfileRevisionConflictError as exc:
        return _media_error(
            "profile_revision_conflict",
            str(exc),
            409,
            details={"current_profile": exc.current_profile},
        )
    except ValueError as exc:
        return _media_error("invalid_creator_profile", str(exc), 400)
    return jsonify(
        {
            "object": "creator.profile",
            "schema_version": "1",
            "profile": profile,
        }
    )


@api_blueprint.route("/v1/creator/profile/suggestions", methods=["GET"])
def get_creator_profile_suggestions():
    denied = _require_media_read_access()
    if denied:
        return denied
    suggestions = _runtime_extension("creator_memory_service").suggestions()
    return jsonify(
        {
            "object": "creator.profile.suggestion.list",
            "schema_version": "1",
            "data": suggestions,
            "suggestions": suggestions,
        }
    )


@api_blueprint.route("/v1/media/capabilities", methods=["GET"])
def get_media_capabilities():
    denied = _require_media_read_access()
    if denied:
        return denied
    ffmpeg = binary_capability("ffmpeg")
    ffprobe = binary_capability("ffprobe")
    codec = (
        ffmpeg_encode_capability()
        if ffmpeg.get("available") and ffprobe.get("available")
        else {
            "available": False,
            "code": "ffmpeg_unsupported",
        }
    )
    ready = bool(ffmpeg.get("available") and ffprobe.get("available") and codec.get("available"))
    renderable_image_extensions = sorted(
        extension for extension in IMAGE_EXTENSIONS if Image.registered_extensions().get(extension)
    )
    return jsonify(
        {
            "object": "media.capabilities",
            "schema_version": "1",
            "status": "ready" if ready else "degraded",
            "local_only": True,
            "external_video_analysis": "disabled_not_authorized",
            "ffmpeg": ffmpeg,
            "ffprobe": ffprobe,
            "encoder_probe": codec,
            "supported": {
                "image_extensions": renderable_image_extensions,
                "video_extensions": sorted(VIDEO_EXTENSIONS),
                "render_profiles": ["preview-low"] if ready else [],
                "verified_preview_save_as": ready,
                "direct_export_requires_export_grant": True,
            },
            "preview_root_id": _runtime_extension("app_preview_root_id"),
            "write_requires_desktop_token": True,
        }
    )


@api_blueprint.route("/v1/assets/import", methods=["POST"])
def import_media_assets():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/assets/import"
    try:
        payload = _json_object()
        idempotency_scope, idempotency_key, request_hash = _atomic_idempotency_context(
            scope,
            payload,
        )
        repository = _media_repository()
        replay = repository.replay_idempotent_write(
            scope=idempotency_scope,
            key=idempotency_key,
            request_sha256=request_hash,
        )
        if replay is not None:
            service = MediaImportService(
                repository,
                _runtime_extension("media_job_runner"),
            )
            frozen_jobs = replay.response.get("jobs")
            service.submit_jobs(
                [dict(job) for job in frozen_jobs if isinstance(job, dict)] if isinstance(frozen_jobs, list) else []
            )
            return jsonify(replay.response), replay.response_status

        raw_root_id = payload.get("library_root_id")
        if isinstance(raw_root_id, str) and raw_root_id:
            root_id = raw_root_id
            root_record = repository.library_root(root_id)
            if not root_record or root_record.get("status") != "active":
                raise ValueError("Approved library root is unavailable.")
            root = repository.validate_library_root(root_id)
        else:
            raw_path = payload.get("root_path")
            configured = _runtime_settings().image_library_dir.resolve(strict=True)
            if raw_path is not None and (
                not isinstance(raw_path, str) or Path(raw_path).expanduser().resolve(strict=True) != configured
            ):
                raise ValueError("`root_path` must equal the directory already approved in MemoLens Settings.")
            root_id = repository._root_id(configured)
            root_record = repository.library_root(root_id)
            if not root_record or root_record.get("status") != "active":
                raise ValueError("Approved library root is unavailable.")
            root = repository.validate_library_root(root_id)

        service = MediaImportService(
            repository,
            _runtime_extension("media_job_runner"),
        )
        plan = service.prepare_import(root_id=root_id, root=root, payload=payload)
        operation_material = f"{idempotency_scope}\0{idempotency_key}\0{request_hash}"
        operation_id = f"import_{hashlib.sha256(operation_material.encode()).hexdigest()[:24]}"

        image_enqueue = None
        image_authority = None
        if plan.has_enqueueable_image:
            lease = getattr(g, "memolens_runtime_lease", None)
            if not isinstance(lease, RuntimeLease):
                plan.close()
                raise RuntimeError(
                    "Image import requires one pinned MemoLens runtime generation."
                )
            candidate = getattr(
                repository,
                "enqueue_image_analysis_in_transaction",
                None,
            )
            if not callable(candidate):
                plan.close()
                raise RuntimeError(
                    "Canonical image transaction enqueue is unavailable."
                )
            image_enqueue = candidate
            image_authority = ImageImportEnqueueAuthority(
                runtime_generation=lease.generation_id,
                database_file_identity=repository.bind_image_database_identity(),
                operation_id=operation_id,
            )

        def apply_import(connection: sqlite3.Connection):
            if image_enqueue is not None and image_authority is not None:
                result = service.apply_prepared(
                    connection,
                    root_id=root_id,
                    plan=plan,
                    image_enqueue=image_enqueue,
                    image_authority=image_authority,
                    retain_source_bindings=True,
                )
            else:
                result = service.apply_prepared(
                    connection,
                    root_id=root_id,
                    plan=plan,
                )
            jobs = [_public_job(job) for job in result.jobs]
            body: dict[str, object] = {
                "object": "asset.import",
                "schema_version": "1",
                "id": operation_id,
                "status": result.status,
                "dry_run": result.dry_run,
                "kinds": result.kinds,
                "assets": result.assets,
                "asset_ids": [str(value.get("id")) for value in result.assets if value.get("id")],
                "jobs": jobs,
                "job": jobs[0] if jobs else None,
                "job_id": jobs[0]["id"] if jobs else None,
                "imported": result.imported,
                "skipped": result.skipped,
                "rejected": result.rejected,
                "external_analysis": False,
            }
            return body, 202 if jobs else 200, operation_id

        try:
            committed = repository.execute_idempotent_write(
                scope=idempotency_scope,
                key=idempotency_key,
                request_sha256=request_hash,
                resource_type="asset_import",
                mutation=apply_import,
            )
        finally:
            plan.close()
        frozen_jobs = committed.response.get("jobs")
        service.submit_jobs(
            [dict(job) for job in frozen_jobs if isinstance(job, dict)] if isinstance(frozen_jobs, list) else []
        )
        return jsonify(committed.response), committed.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _media_error("invalid_import", str(exc), 400)


@api_blueprint.route(
    "/v1/image-analysis/legacy-backfill-batches",
    methods=["POST"],
)
def create_legacy_image_backfill_batch():
    """Admit legacy-only image rows through one pinned canonical runtime."""

    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/image-analysis/legacy-backfill-batches"
    try:
        payload = strict_json_object(request)
        idempotency_scope, idempotency_key, request_sha256 = (
            _atomic_idempotency_context(scope, payload)
        )
        lease = getattr(g, "memolens_runtime_lease", None)
        if not isinstance(lease, RuntimeLease):
            return _media_error(
                "legacy_backfill_runtime_unavailable",
                "Legacy image backfill requires one pinned MemoLens runtime generation.",
                503,
            )
        repository = _media_repository()
        service = LegacyImageBackfillService(
            repository,
            _runtime_extension("media_job_runner"),
            runtime_generation=lease.generation_id,
            database_file_identity=repository.bind_image_database_identity(),
        )
        result = service.run(
            payload,
            idempotency_key=idempotency_key,
            request_sha256=request_sha256,
        )
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except StrictJsonRequestError as exc:
        return _media_error(exc.code, str(exc), 400)
    except LegacyImageBackfillError as exc:
        return _media_error(
            exc.code,
            str(exc),
            exc.status,
            details=exc.details,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return _media_error(
            "legacy_backfill_unavailable",
            str(exc),
            503 if isinstance(exc, RuntimeError) else 409,
        )


@api_blueprint.route("/v1/index/jobs/<job_id>", methods=["GET"])
def get_media_index_job(job_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    repository = _media_repository()
    job = repository.get_media_job(job_id)
    if not job:
        return _media_error("job_not_found", "Media job does not exist.", 404)
    try:
        public = _receipt_anchored_public_job(repository, job)
    except (
        LibraryScanConflictError,
        LibraryScanContractError,
        LibraryScanIntegrityError,
        LibraryScanValidationError,
    ):
        return _library_scan_read_error()
    return jsonify({"job": public, **public})


@api_blueprint.route("/v1/index/jobs", methods=["GET"])
def list_media_index_jobs():
    denied = _require_media_read_access()
    if denied:
        return denied
    active = request.args.get("active", "true").casefold() != "false"
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 100))
    except ValueError:
        return _media_error("invalid_limit", "`limit` must be an integer.", 400)
    repository = _media_repository()
    try:
        generic_jobs = repository.list_media_jobs(active=active, limit=limit)
        scan_snapshots = repository.list_library_scan_public_jobs(
            active=active,
            limit=limit,
        )
        public_scans = {
            str(snapshot["job"]["id"]): public_library_scan_snapshot(snapshot)
            for snapshot in scan_snapshots
        }
        jobs = []
        for job in generic_jobs:
            if job.get("kind") != "library_scan":
                jobs.append(_public_job(job))
                continue
            job_id = str(job.get("id") or "")
            public = public_scans.get(job_id)
            if public is None:
                # A valid receipt-bound scan in the generic top-N must also be
                # in the scan top-N. This exact lookup distinguishes a benign
                # ordering tie from an unreceipted rogue row.
                public = public_library_scan_snapshot(
                    repository.get_library_scan_public_job(job_id)
                )
            jobs.append(public)
    except (
        LibraryScanConflictError,
        LibraryScanContractError,
        LibraryScanIntegrityError,
        LibraryScanValidationError,
    ):
        return _library_scan_read_error()
    return jsonify({"object": "media.job.list", "schema_version": "1", "data": jobs, "jobs": jobs})


@api_blueprint.route("/v1/index/jobs/<job_id>/cancel", methods=["POST"])
def cancel_media_index_job(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/index/jobs/{job_id}/cancel"
    try:
        payload = request.get_json(silent=True) or {}
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        repository = _media_repository()
        current_job = repository.get_media_job(job_id)

        if current_job is not None and current_job.get("kind") == "library_scan":
            replay = repository.replay_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
            )
            if replay is not None:
                return jsonify(replay.response), replay.response_status
            runner = _runtime_extension("media_job_runner")
            if not runner.cancel(job_id):
                latest = repository.get_media_job(job_id)
                if (
                    latest is None
                    or latest.get("kind") != "library_scan"
                    or not latest.get("cancel_requested")
                    or latest.get("status") not in {"cancelling", "cancelled"}
                ):
                    raise ValueError("Library scan cannot be cancelled.")
            anchored_public = _receipt_anchored_public_job(
                repository,
                repository.get_media_job(job_id) or {},
            )

            def freeze_library_scan_cancel(connection):
                job = repository.get_media_job_in_transaction(connection, job_id)
                if (
                    job is None
                    or job.get("kind") != "library_scan"
                    or not job.get("cancel_requested")
                    or job.get("status") not in {"cancelling", "cancelled"}
                ):
                    raise ValueError(
                        "Library scan cancellation changed before its response was committed."
                    )
                if (
                    job.get("status") != anchored_public["status"]
                    or job.get("stage") != anchored_public["stage"]
                    or job.get("attempt") != anchored_public["attempt"]
                ):
                    raise ValueError(
                        "Library scan cancellation changed before its response was committed."
                    )
                return {"job": anchored_public}, 202, job_id

            result = repository.execute_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
                resource_type="media_job_cancel",
                mutation=freeze_library_scan_cancel,
            )
            return jsonify(result.response), result.response_status

        def mutation(connection):
            if not repository.request_media_job_cancel(job_id, connection=connection):
                raise ValueError("Media job cannot be cancelled.")
            job = repository.get_media_job_in_transaction(connection, job_id)
            return {"job": _public_job(job)}, 202, job_id

        result = repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=key,
            request_sha256=request_hash,
            resource_type="media_job_cancel",
            mutation=mutation,
        )
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except (
        LibraryScanConflictError,
        LibraryScanContractError,
        LibraryScanIntegrityError,
        LibraryScanValidationError,
    ):
        return _library_scan_read_error()
    except ValueError as exc:
        return _media_error("job_not_cancellable", str(exc), 409)


@api_blueprint.route("/v1/index/jobs/<job_id>/resume", methods=["POST"])
def resume_media_index_job(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/index/jobs/{job_id}/resume"
    try:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _media_error(
                "invalid_request",
                "Media job resume payload must be a JSON object.",
                400,
            )
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        repository = _media_repository()

        current_job = repository.get_media_job(job_id)
        if current_job is None:
            return _media_error(
                "job_not_resumable",
                "Media job does not exist.",
                409,
            )

        if current_job.get("kind") == "library_scan":
            replay = repository.replay_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
            )
            if replay is not None:
                response = jsonify(replay.response)
                response.status_code = replay.response_status
                response.headers["Idempotency-Replayed"] = "true"
                return response
            runner = _runtime_extension("media_job_runner")
            resumed = runner.resume(job_id, submit=False)
            latest = repository.get_media_job(job_id)
            if not resumed and (
                latest is None
                or latest.get("kind") != "library_scan"
                or latest.get("status") != "queued"
                or int(latest.get("attempt") or 0) <= 1
                or latest.get("checkpoint") == {}
                or latest.get("cancel_requested")
            ):
                raise ValueError("Library scan cannot be resumed.")
            anchored_public = _receipt_anchored_public_job(
                repository,
                repository.get_media_job(job_id) or {},
            )

            def freeze_library_scan_resume(connection):
                job = repository.get_media_job_in_transaction(connection, job_id)
                if (
                    job is None
                    or job.get("kind") != "library_scan"
                    or job.get("status") != "queued"
                    or int(job.get("attempt") or 0) <= 1
                    or job.get("checkpoint") == {}
                    or job.get("cancel_requested")
                ):
                    raise ValueError(
                        "Library scan resume changed before its response was committed."
                    )
                if (
                    job.get("status") != anchored_public["status"]
                    or job.get("stage") != anchored_public["stage"]
                    or job.get("attempt") != anchored_public["attempt"]
                ):
                    raise ValueError(
                        "Library scan resume changed before its response was committed."
                    )
                return {"job": anchored_public}, 202, job_id

            result = repository.execute_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
                resource_type="media_job_resume",
                mutation=freeze_library_scan_resume,
            )
            latest = repository.get_media_job(job_id)
            if (
                latest is not None
                and latest.get("status") == "queued"
                and not latest.get("cancel_requested")
            ):
                runner.submit(job_id)
            response = jsonify(result.response)
            response.status_code = result.response_status
            response.headers["Idempotency-Replayed"] = (
                "true" if result.replayed else "false"
            )
            return response

        if current_job.get("kind") == "image_analysis":
            replay = repository.replay_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
            )
            if replay is not None:
                response = jsonify(replay.response)
                response.status_code = replay.response_status
                response.headers["Idempotency-Replayed"] = "true"
                return response

            lease = getattr(g, "memolens_runtime_lease", None)
            if not isinstance(lease, RuntimeLease):
                return _media_error(
                    "job_not_resumable",
                    "Image analysis resume requires one pinned runtime generation.",
                    409,
                )
            try:
                resumed_job = repository.resume_image_analysis_job(
                    job_id,
                    runtime_generation=lease.generation_id,
                    database_file_identity=repository.bind_image_database_identity(),
                    reason="explicit_resume",
                )
            except RuntimeError as exc:
                return _media_error("job_not_resumable", str(exc), 409)

            expected_attempt = int(resumed_job.get("attempt") or 0)

            def freeze_image_resume(connection):
                job = repository.get_media_job_in_transaction(connection, job_id)
                if (
                    job is None
                    or job.get("kind") != "image_analysis"
                    or job.get("status") != "queued"
                    or int(job.get("attempt") or 0) != expected_attempt
                    or bool(job.get("cancel_requested"))
                ):
                    raise ValueError(
                        "Image analysis resume changed before its response could be committed."
                    )
                return {"job": _public_job(job)}, 202, job_id

            result = repository.execute_idempotent_write(
                scope=idempotency_scope,
                key=key,
                request_sha256=request_hash,
                resource_type="media_job_resume",
                mutation=freeze_image_resume,
            )
            latest = repository.get_media_job(job_id)
            if (
                latest is not None
                and latest.get("status") == "queued"
                and not latest.get("cancel_requested")
            ):
                _runtime_extension("media_job_runner").submit(job_id)
            response = jsonify(result.response)
            response.status_code = result.response_status
            response.headers["Idempotency-Replayed"] = (
                "true" if result.replayed else "false"
            )
            return response

        def mutation(connection):
            if not repository.reset_media_job_for_resume(job_id, connection=connection):
                raise ValueError("Media job cannot be resumed.")
            job = repository.get_media_job_in_transaction(connection, job_id)
            return {"job": _public_job(job)}, 202, job_id

        result = repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=key,
            request_sha256=request_hash,
            resource_type="media_job_resume",
            mutation=mutation,
        )
        current = repository.get_media_job(job_id)
        if current and current.get("status") in {"queued", "interrupted"} and not current.get("cancel_requested"):
            _runtime_extension("media_job_runner").submit(job_id)
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except (
        LibraryScanConflictError,
        LibraryScanContractError,
        LibraryScanIntegrityError,
        LibraryScanValidationError,
    ):
        return _library_scan_read_error()
    except ValueError as exc:
        return _media_error("job_not_resumable", str(exc), 409)


@api_blueprint.route("/v1/search/mixed", methods=["POST"])
def mixed_media_search():
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        result = _runtime_extension("mixed_retrieval_service").search(_json_object())
    except CanonicalExportIntegrityError as exc:
        return _canonical_export_exception(exc)
    except ValueError as exc:
        return _media_error("invalid_search", str(exc), 400)
    return jsonify(result)


@api_blueprint.route("/v1/assets/<asset_id>/media", methods=["GET"])
def stream_media_asset(asset_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    opened = _media_repository().open_asset_file(asset_id)
    if not opened:
        return _media_error("asset_unavailable", "Asset media is unavailable.", 404)
    asset, handle, size = opened
    return _stream_verified_handle(
        handle,
        size,
        mimetype=str(asset.get("mime_type") or "application/octet-stream"),
        etag=str(asset["sha256"]),
    )


@api_blueprint.route("/v1/assets/<asset_id>/thumbnail", methods=["GET"])
def get_media_asset_thumbnail(asset_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    opened = _media_repository().open_asset_file(asset_id)
    if not opened:
        return _media_error("asset_unavailable", "Asset thumbnail is unavailable.", 404)
    asset, handle, _ = opened
    try:
        if asset.get("kind") != "image":
            handle.close()
            return _media_error("thumbnail_not_available", "Use a video segment thumbnail.", 404)
        with Image.open(handle) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((960, 960), getattr(Image, "Resampling", Image).LANCZOS)
    except (OSError, ValueError, UnidentifiedImageError):
        return _media_error("asset_unavailable", "Asset thumbnail is unavailable.", 404)
    finally:
        handle.close()
    buffer = BytesIO()
    image.save(buffer, "JPEG", quality=86, optimize=True)
    buffer.seek(0)
    return _private_media(send_file(buffer, mimetype="image/jpeg", conditional=False))


@api_blueprint.route("/v1/video-segments/<segment_id>", methods=["GET"])
def get_video_segment(segment_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    segment = _media_repository().get_segment(segment_id)
    if not segment:
        return _media_error("segment_not_found", "Video segment does not exist.", 404)
    keyframes = [
        {
            "id": item["id"],
            "timestamp_ms": item["timestamp_ms"],
            "thumbnail_url": f"/v1/keyframes/{item['id']}",
            "selection_reason": item["selection_reason"],
        }
        for item in segment.pop("keyframes")
    ]
    transcript = [
        {key: item.get(key) for key in ("id", "start_ms", "end_ms", "text")} for item in segment.pop("transcripts")
    ]
    hidden = {"semantic", "combined_text", "text_embedding", "relative_path"}
    body = {key: value for key, value in segment.items() if key not in hidden}
    body.update(
        {
            "object": "video.segment",
            "schema_version": "1",
            "keyframes": keyframes,
            "transcript": transcript,
            "media_url": f"/v1/assets/{segment['asset_id']}/media",
            "thumbnail_url": f"/v1/video-segments/{segment_id}/thumbnail",
        }
    )
    return jsonify(body)


def _open_keyframe_file(keyframe_id: str):
    """Return the exact verified keyframe inode that the response will read."""

    repository = _media_repository()
    with repository.transaction() as connection:
        row = connection.execute("SELECT cache_key,sha256 FROM keyframes WHERE id=?", (keyframe_id,)).fetchone()
    if not row:
        return None
    root = _runtime_settings().app_state_dir / "media-cache"
    expected_sha256 = str(row["sha256"])
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return None
    pinned_root: PinnedLibraryRoot | None = None
    file_fd: int | None = None
    try:
        pinned_root = open_pinned_library_root(root)
        file_fd = pinned_root.open_relative_regular(Path(str(row["cache_key"])))
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        digest = hashlib.sha256()
        with os.fdopen(os.dup(file_fd), "rb") as verification:
            for chunk in iter(lambda: verification.read(1024 * 1024), b""):
                digest.update(chunk)
        after = os.fstat(file_fd)
        observed_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if (
            observed_identity != identity
            or not hmac.compare_digest(digest.hexdigest(), expected_sha256)
        ):
            return None
        pinned_root.verify_current_path_identity()
        handle = os.fdopen(file_fd, "rb")
        file_fd = None
        return handle, before.st_size, expected_sha256
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if pinned_root is not None:
            pinned_root.close()


@api_blueprint.route("/v1/keyframes/<keyframe_id>", methods=["GET"])
def get_keyframe(keyframe_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    opened = _open_keyframe_file(keyframe_id)
    if opened is None:
        return _media_error("keyframe_unavailable", "Keyframe is unavailable.", 404)
    handle, size, digest = opened
    return _stream_verified_handle(
        handle,
        size,
        mimetype="image/jpeg",
        etag=digest,
    )


@api_blueprint.route("/v1/video-segments/<segment_id>/thumbnail", methods=["GET"])
def get_segment_thumbnail(segment_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    segment = _media_repository().get_segment(segment_id)
    if not segment or not segment.get("keyframes"):
        return _media_error("keyframe_unavailable", "Segment thumbnail is unavailable.", 404)
    opened = _open_keyframe_file(str(segment["keyframes"][0]["id"]))
    if opened is None:
        return _media_error("keyframe_unavailable", "Segment thumbnail is unavailable.", 404)
    handle, size, digest = opened
    return _stream_verified_handle(
        handle,
        size,
        mimetype="image/jpeg",
        etag=digest,
    )


@api_blueprint.route("/v1/creative/briefs", methods=["POST"])
def create_creative_brief():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/creative/briefs"
    try:
        payload = _json_object()
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        result = _runtime_extension("creative_director").create_brief_idempotent(
            payload,
            idempotency_scope=idempotency_scope,
            idempotency_key=key,
            request_sha256=request_hash,
        )
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except CreativeBriefError as exc:
        return _media_error(exc.code, str(exc), exc.status, details=exc.details)
    except CanonicalExportIntegrityError as exc:
        return _canonical_export_exception(exc)
    except ValueError as exc:
        return _media_error("invalid_brief", str(exc), 400)


def _safe_core_capability(value: Mapping[str, object]) -> dict[str, object]:
    """Closed public projection of a durable Core capability row."""

    return {
        "capability_id": value.get("id") or value.get("capability_id"),
        "project_id": value.get("project_id"),
        "paired_subject_id": value.get("paired_subject_id"),
        "claimed_client_label": value.get("claimed_client_label"),
        "vendor_identity_verified": False,
        "user_authority_verified": False,
        "actions": value.get("actions", []),
        "issued_at": value.get("issued_at"),
        "expires_at": value.get("expires_at"),
        "max_operations": value.get("max_operations"),
        "used_operations": value.get("operations_used", 0),
        "remaining_operations": value.get("remaining_operations"),
        "status": value.get("status"),
    }


def _core_pairing_presentation(pending) -> dict[str, object]:
    repository = _media_repository()
    envelope = repository.build_agent_pairing_presentation(
        pairing_id=pending.pairing_id,
        runtime_authority_epoch=_runtime_authority_epoch(),
        project_id=pending.project_id,
        paired_subject_id=pending.paired_subject_id,
        claimed_client_label=pending.claimed_client_label,
        actions=pending.requested_actions,
        ttl_seconds=pending.requested_ttl_seconds,
        max_operations=pending.requested_max_operations,
        pairing_request_sha256=pending.pairing_digest,
    )
    if type(envelope) is not dict or set(envelope) != {"presentation", "presentation_sha256"}:
        raise BlueprintIntegrityError("agent_pairing_presentation_invalid")
    presentation = envelope.get("presentation")
    digest = envelope.get("presentation_sha256")
    if type(presentation) is not dict or type(digest) is not str:
        raise BlueprintIntegrityError("agent_pairing_presentation_invalid")
    observed = presentation.get("observed_head")
    if not isinstance(observed, Mapping) or any(
        observed.get(key) != pending.observed_head.get(key)
        for key in ("revision", "content_sha256", "semantic_sha256", "operation_id")
    ):
        raise AgentProtocolError(
            "blueprint_head_conflict",
            "The Creative Blueprint head changed while pairing was pending.",
            status=409,
            details={"current": observed},
        )
    return envelope


def _agent_query_must_be_empty():
    if request.query_string:
        raise AgentProtocolError(
            "agent_operation_request_mismatch",
            "Agent protocol routes do not accept query parameters or database paths.",
            status=409,
        )


@api_blueprint.route("/v1/agent/pairings", methods=["POST"])
def create_agent_pairing():
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        payload = strict_json_object(request)
        project_id = payload.get("project_id")
        if type(project_id) is not str:
            raise AgentProtocolError(
                "invalid_agent_pairing_request",
                "project_id is required.",
                status=400,
            )
        reservation = broker.reserve_pairing_intake(
            payload,
            preauth_already_consumed=True,
        )
        try:
            repository = _media_repository()
            with repository.transaction() as connection:
                project = repository.get_creative_project_in_transaction(
                    connection,
                    project_id,
                )
                if project is None:
                    raise BlueprintServiceError(
                        "project_not_found",
                        "Creative project does not exist.",
                        status=404,
                    )
                if project.get("status") == "archived":
                    raise BlueprintServiceError(
                        "project_archived",
                        "Archived creative projects cannot be paired.",
                        status=409,
                    )
                head = repository.get_blueprint_head_in_transaction(
                    connection,
                    project_id,
                )
                if head is None:
                    raise BlueprintServiceError(
                        "blueprint_not_found",
                        "Agent pairing requires an existing Creative Blueprint head.",
                        status=404,
                    )
            response = broker.create_pairing(
                payload,
                database_uuid=repository.database_uuid,
                observed_head=head,
                intake_reservation=reservation,
            )
        finally:
            broker.release_pairing_intake(reservation)
        return jsonify(response), 201
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route("/v1/agent/pairings/<pairing_id>", methods=["GET"])
def get_agent_pairing_status(pairing_id: str):
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        response = broker.agent_pairing_status(
            _protocol_identifier(pairing_id),
            request.headers.get(AGENT_STATUS_PROOF_HEADER, ""),
        )
        capability_id = response.get("capability_id")
        if isinstance(capability_id, str):
            capability = _media_repository().get_agent_project_capability(
                capability_id,
                runtime_authority_epoch=_runtime_authority_epoch(),
            )
            if capability is None:
                raise AgentProtocolError(
                    "agent_pairing_required",
                    "The approved capability is unavailable.",
                    status=401,
                )
            safe_capability = _safe_core_capability(capability)
            response["capability"] = safe_capability
            for key in (
                "issued_at",
                "expires_at",
                "max_operations",
                "used_operations",
                "remaining_operations",
            ):
                response[key] = safe_capability.get(key)
            durable_status = capability.get("status")
            response["status"] = "expired" if durable_status == "exhausted" else durable_status
            if durable_status == "exhausted":
                response["reason_code"] = "max_operations_exhausted"
        return jsonify(response)
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/agent/capabilities/<capability_id>/nonce",
    methods=["POST"],
)
def create_agent_operation_nonce(capability_id: str):
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        payload = strict_json_object(request)
        if (
            set(payload) != {"object", "schema_version", "action"}
            or payload.get("object") != "memolens.agent_operation_nonce_request"
            or payload.get("schema_version") != "1"
            or type(payload.get("action")) is not str
        ):
            raise AgentProtocolError(
                "agent_operation_request_mismatch",
                "Agent nonce request has an unsupported shape.",
                status=409,
            )
        capability_id = _protocol_identifier(capability_id)
        repository = _media_repository()
        durable = repository.get_agent_project_capability(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        if durable is None:
            raise AgentProtocolError(
                "agent_pairing_required",
                "The Agent capability is unavailable.",
                status=401,
            )
        if durable.get("status") == "revoked":
            raise AgentProtocolError(
                "agent_pairing_revoked",
                "The Agent capability was revoked.",
                status=403,
            )
        replay_only_exhausted = (
            durable.get("status") == "exhausted"
            and payload["action"]
            in {
                "timeline.apply_edit",
                "timeline.apply_structural_edit",
                "timeline.restore_revision",
            }
        )
        if durable.get("status") != "active" and not replay_only_exhausted:
            raise AgentProtocolError(
                "agent_pairing_expired",
                "The Agent capability is no longer active.",
                status=403,
            )
        response = broker.issue_nonce(
            capability_id,
            action=str(payload["action"]),
            database_uuid=repository.database_uuid,
            status_proof=request.headers.get(AGENT_STATUS_PROOF_HEADER, ""),
        )
        return jsonify(response), 201
    except Exception as exc:
        return _b1_exception(exc)


def _execute_agent_blueprint_command(project_id: str, *, action: str):
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        project_id = _protocol_identifier(project_id, code="agent_pairing_scope_denied")
        payload = strict_json_object(request)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        capability_id = _protocol_identifier(
            request.headers.get(AGENT_CAPABILITY_HEADER, "")
        )
        nonce = request.headers.get(AGENT_NONCE_HEADER, "")
        proof = request.headers.get(AGENT_PROOF_HEADER, "")
        body_sha256 = sha256_text(canonical_json(payload))
        repository = _media_repository()
        durable = repository.get_agent_project_capability(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        if durable is None:
            raise AgentProtocolError(
                "agent_pairing_required",
                "The Agent capability is unavailable.",
                status=401,
            )
        if durable.get("status") == "revoked":
            raise AgentProtocolError(
                "agent_pairing_revoked",
                "The Agent capability was revoked.",
                status=403,
            )
        if durable.get("status") != "active":
            raise AgentProtocolError(
                "agent_pairing_expired",
                "The Agent capability is no longer active.",
                status=403,
            )
        runtime_capability = broker.claim_command_proof(
            capability_id=capability_id,
            nonce=nonce,
            proof=proof,
            method=request.method,
            canonical_path=request.path,
            database_uuid=repository.database_uuid,
            project_id=project_id,
            action=action,
            body_sha256=body_sha256,
            idempotency_key=idempotency_key,
        )
        service = _runtime_extension("blueprint_service")
        arguments = {
            "idempotency_key": idempotency_key,
            "paired_capability_id": runtime_capability.capability_id,
            "runtime_authority_epoch": runtime_capability.runtime_authority_epoch,
            "presented_secret_sha256": runtime_capability.proof_secret_sha256,
        }
        if action == "blueprint.commit_proposal":
            result = service.commit_proposal(project_id, payload, **arguments)
        else:
            result = service.restore_revision(project_id, payload, **arguments)
        return _blueprint_command_response(result)
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/blueprint/commit",
    methods=["POST"],
)
def agent_commit_blueprint_proposal(project_id: str):
    return _execute_agent_blueprint_command(
        project_id,
        action="blueprint.commit_proposal",
    )


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/blueprint/restore",
    methods=["POST"],
)
def agent_restore_blueprint_revision(project_id: str):
    return _execute_agent_blueprint_command(
        project_id,
        action="blueprint.restore_revision",
    )


def _execute_agent_timeline_command(project_id: str, *, action: str):
    if action not in {
        "timeline.apply_edit",
        "timeline.apply_structural_edit",
        "timeline.restore_revision",
    }:
        raise RuntimeError("unsupported Agent Timeline command")
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        project_id = _protocol_identifier(
            project_id,
            code="agent_pairing_scope_denied",
        )
        payload = strict_json_object(request)
        idempotency_key = request.headers.get("Idempotency-Key", "")
        capability_id = _protocol_identifier(
            request.headers.get(AGENT_CAPABILITY_HEADER, "")
        )
        nonce = request.headers.get(AGENT_NONCE_HEADER, "")
        proof = request.headers.get(AGENT_PROOF_HEADER, "")
        body_sha256 = sha256_text(canonical_json(payload))
        repository = _media_repository()
        durable = repository.get_agent_project_capability(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        if durable is None:
            raise AgentProtocolError(
                "agent_pairing_required",
                "The Agent capability is unavailable.",
                status=401,
            )
        if durable.get("status") == "revoked":
            raise AgentProtocolError(
                "agent_pairing_revoked",
                "The Agent capability was revoked.",
                status=403,
            )
        if durable.get("status") not in {"active", "exhausted"}:
            raise AgentProtocolError(
                "agent_pairing_expired",
                "The Agent capability is no longer active.",
                status=403,
            )
        runtime_capability = broker.claim_command_proof(
            capability_id=capability_id,
            nonce=nonce,
            proof=proof,
            method=request.method,
            canonical_path=request.path,
            database_uuid=repository.database_uuid,
            project_id=project_id,
            action=action,
            body_sha256=body_sha256,
            idempotency_key=idempotency_key,
        )
        service = _runtime_extension("timeline_lowering_service")
        arguments = {
            "idempotency_key": idempotency_key,
            "expected_database_uuid": repository.database_uuid,
            "paired_capability_id": runtime_capability.capability_id,
            "runtime_authority_epoch": runtime_capability.runtime_authority_epoch,
            "presented_secret_sha256": runtime_capability.proof_secret_sha256,
        }
        # A valid caller has crossed the command authority boundary.  Invalidate
        # old-head playback before entering any service path that may commit;
        # a failed mutation conservatively requires the editor to mint again.
        _drop_preview_project_leases(project_id)
        if action == "timeline.apply_edit":
            result = service.apply_paired_edit(project_id, payload, **arguments)
        elif action == "timeline.apply_structural_edit":
            result = service.apply_paired_structural_edit(
                project_id,
                payload,
                **arguments,
            )
        else:
            result = service.restore_paired_revision(project_id, payload, **arguments)
        _drop_preview_project_leases(project_id)
        return _blueprint_command_response(result)
    except Exception as exc:
        return _agent_timeline_exception(exc)


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/timeline/edit",
    methods=["POST"],
)
def agent_edit_project_timeline(project_id: str):
    return _execute_agent_timeline_command(
        project_id,
        action="timeline.apply_edit",
    )


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/timeline/structural-edit",
    methods=["POST"],
)
def agent_structurally_edit_project_timeline(project_id: str):
    return _execute_agent_timeline_command(
        project_id,
        action="timeline.apply_structural_edit",
    )


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/timeline/restore",
    methods=["POST"],
)
def agent_restore_project_timeline(project_id: str):
    return _execute_agent_timeline_command(
        project_id,
        action="timeline.restore_revision",
    )


@api_blueprint.route(
    "/v1/agent/creative/projects/<project_id>/timeline/preview-leases",
    methods=["POST"],
)
def mint_agent_timeline_preview_lease(project_id: str):
    denied = _require_agent_originless()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        broker = _agent_broker()
        broker.consume_pre_auth_request()
        project_id = _protocol_identifier(
            project_id,
            code="agent_pairing_scope_denied",
        )
        if any(
            request.headers.get(header)
            for header in (
                AGENT_NONCE_HEADER,
                AGENT_PROOF_HEADER,
                AGENT_STATUS_PROOF_HEADER,
                PREVIEW_LEASE_HEADER,
                PREVIEW_NONCE_HEADER,
                MAIN_AUTHORITY_HEADER,
                "X-MemoLens-Desktop-Token",
            )
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview mint headers have an unsupported authority shape.",
                status=403,
            )
        payload = strict_json_object(request)
        capability_id = _protocol_identifier(
            request.headers.get(AGENT_CAPABILITY_HEADER, ""),
            code="agent_pairing_scope_denied",
        )
        canonical_path = (
            f"/v1/agent/creative/projects/{project_id}/timeline/preview-leases"
        )
        if request.path != canonical_path:
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview mint path is invalid.",
                status=403,
            )
        result = _timeline_preview_service().mint_lease(
            capability_id=capability_id,
            request_payload=payload,
            proof=request.headers.get(PREVIEW_PROOF_HEADER, ""),
            canonical_path=canonical_path,
            runtime_generation_id=_runtime_generation_id(),
        )
        response = jsonify(result)
        response.status_code = 201
        return _private_media(response)
    except Exception as exc:
        return _agent_preview_exception(exc)


def _release_preview_runtime_lease() -> None:
    """A prepared fd + app broker are sufficient for the response stream."""

    lease = g.pop("memolens_runtime_lease", None)
    if isinstance(lease, RuntimeLease):
        lease.release()


@api_blueprint.route(
    "/v1/agent/preview-leases/<lease_id>/clips/<clip_id>/media",
    methods=["GET", "HEAD"],
)
def stream_agent_timeline_preview_media(lease_id: str, clip_id: str):
    denied = _require_agent_originless()
    if denied:
        return denied
    prepared = None
    service = None
    try:
        _agent_query_must_be_empty()
        _agent_broker().consume_pre_auth_request()
        if (
            (request.content_length not in {None, 0})
            or request.headers.get("Transfer-Encoding") is not None
            or request.headers.get("Content-Type") is not None
            or any(
                request.headers.get(header)
                for header in (
                    AGENT_CAPABILITY_HEADER,
                    AGENT_NONCE_HEADER,
                    AGENT_PROOF_HEADER,
                    AGENT_STATUS_PROOF_HEADER,
                    MAIN_AUTHORITY_HEADER,
                    "X-MemoLens-Desktop-Token",
                )
            )
        ):
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview media request has an unsupported authority shape.",
                status=403,
            )
        lease_id = _protocol_identifier(
            lease_id,
            code="agent_pairing_scope_denied",
        )
        clip_id = _protocol_identifier(
            clip_id,
            code="agent_pairing_scope_denied",
        )
        if request.headers.get(PREVIEW_LEASE_HEADER, "") != lease_id:
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview lease header does not match the request path.",
                status=403,
            )
        canonical_path = (
            f"/v1/agent/preview-leases/{lease_id}/clips/{clip_id}/media"
        )
        if request.path != canonical_path:
            raise AgentPreviewError(
                "agent_preview_scope_denied",
                "Timeline preview media path is invalid.",
                status=403,
            )
        service = _timeline_preview_service()
        prepared = service.prepare_read(
            lease_id=lease_id,
            request_nonce=request.headers.get(PREVIEW_NONCE_HEADER, ""),
            proof=request.headers.get(PREVIEW_PROOF_HEADER, ""),
            method=request.method,
            canonical_path=canonical_path,
            range_header=request.headers.get("Range"),
            runtime_generation_id=_runtime_generation_id(),
        )
        byte_range = prepared.reservation.byte_range
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Length": str(byte_range.length),
            "ETag": f'"{prepared.opaque_etag}"',
            "Cache-Control": "private, no-store",
            "Pragma": "no-cache",
            "X-Content-Type-Options": "nosniff",
        }
        if byte_range.status_code == 206:
            headers["Content-Range"] = (
                f"bytes {byte_range.start}-{byte_range.end}/{prepared.size}"
            )
        _release_preview_runtime_lease()
        if request.method == "HEAD":
            service.finish_read(prepared)
            prepared = None
            response = Response(
                b"",
                status=byte_range.status_code,
                mimetype="video/mp4",
            )
            response.headers.update(headers)
            return response
        response = Response(
            service.iter_read(prepared),
            status=byte_range.status_code,
            mimetype="video/mp4",
            direct_passthrough=True,
        )
        response.headers.update(headers)
        response.call_on_close(
            lambda prepared_read=prepared, preview_service=service: (
                preview_service.finish_read(prepared_read)
            )
        )
        prepared = None
        return response
    except Exception as exc:
        if prepared is not None and service is not None:
            service.finish_read(prepared)
        return _agent_preview_exception(exc, media=True)


@api_blueprint.route("/v1/main/agent/pairings", methods=["GET"])
def list_main_agent_pairings():
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        pairings = _agent_broker().main_list_pairings(
            database_uuid=_media_repository().database_uuid,
        )
        return jsonify(
            {
                "object": "agent.pairing_list",
                "schema_version": "1",
                "pairings": pairings,
            }
        )
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/agent/pairings/<pairing_id>/presentation",
    methods=["GET"],
)
def get_main_agent_pairing_presentation(pairing_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        pairing_id = _protocol_identifier(pairing_id)
        pending = _agent_broker().main_pending_pairing(
            pairing_id,
            database_uuid=_media_repository().database_uuid,
        )
        envelope = _core_pairing_presentation(pending)
        return jsonify(
            {
                "object": "agent.pairing_presentation_envelope",
                "schema_version": "1",
                **envelope,
                "display_code": pending.short_code,
                "expires_at": pending.expires_at.isoformat().replace("+00:00", "Z"),
            }
        )
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/agent/pairings/<pairing_id>/approve",
    methods=["POST"],
)
def approve_main_agent_pairing(pairing_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        payload = strict_json_object(request)
        if set(payload) != {"presentation_sha256", "native_gesture_nonce"}:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Pairing approval has an unsupported shape.",
                status=403,
            )
        pairing_id = _protocol_identifier(pairing_id)
        native_nonce = _native_gesture_nonce(payload["native_gesture_nonce"])
        pending = _agent_broker().main_pending_pairing(
            pairing_id,
            database_uuid=_media_repository().database_uuid,
        )
        envelope = _core_pairing_presentation(pending)
        expected_digest = envelope["presentation_sha256"]
        supplied_digest = _sha256_digest(
            payload["presentation_sha256"],
            code="agent_operation_request_mismatch",
        )
        if (
            type(supplied_digest) is not str
            or type(expected_digest) is not str
            or not hmac.compare_digest(supplied_digest, expected_digest)
        ):
            raise AgentProtocolError(
                "agent_operation_request_mismatch",
                "The native pairing presentation no longer matches.",
                status=409,
            )
        capability_id = "cap_" + sha256_text(
            f"{pending.runtime_authority_epoch}\n{pending.pairing_id}\n{pending.pairing_digest}"
        )[:32]
        _agent_broker().require_activation_capacity(capability_id)
        issue = _media_repository().issue_agent_project_capability(
            capability_id=capability_id,
            pairing_id=pending.pairing_id,
            runtime_authority_epoch=pending.runtime_authority_epoch,
            project_id=pending.project_id,
            paired_subject_id=pending.paired_subject_id,
            claimed_client_label=pending.claimed_client_label,
            actions=pending.requested_actions,
            secret_sha256=pending.proof_secret_sha256,
            ttl_seconds=pending.requested_ttl_seconds,
            max_operations=pending.requested_max_operations,
            pairing_request_sha256=pending.pairing_digest,
            presentation=envelope["presentation"],
            presentation_sha256=expected_digest,
            native_gesture_nonce=native_nonce,
        )
        capability = issue.capability
        runtime_capability = _agent_broker().activate_capability(
            pairing_id,
            capability_id=str(capability["id"]),
            issued_at=_parse_utc(capability["issued_at"]),
            expires_at=_parse_utc(capability["expires_at"]),
            max_operations=int(capability["max_operations"]),
        )
        response = _safe_core_capability(capability)
        response.update(
            {
                "object": "agent.capability",
                "schema_version": "1",
                "pairing_id": runtime_capability.pairing_id,
                "replayed": bool(issue.replayed),
            }
        )
        return jsonify(response), 200 if issue.replayed else 201
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/agent/pairings/<pairing_id>/reject",
    methods=["POST"],
)
def reject_main_agent_pairing(pairing_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        payload = strict_json_object(request)
        if set(payload) != {"presentation_sha256", "native_gesture_nonce"}:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Pairing rejection has an unsupported shape.",
                status=403,
            )
        pairing_id = _protocol_identifier(pairing_id)
        _native_gesture_nonce(payload["native_gesture_nonce"])
        pending = _agent_broker().main_pending_pairing(
            pairing_id,
            database_uuid=_media_repository().database_uuid,
        )
        envelope = _core_pairing_presentation(pending)
        supplied = _sha256_digest(
            payload["presentation_sha256"],
            code="agent_operation_request_mismatch",
        )
        if type(supplied) is not str or not hmac.compare_digest(
            supplied,
            str(envelope["presentation_sha256"]),
        ):
            raise AgentProtocolError(
                "agent_operation_request_mismatch",
                "The native pairing presentation no longer matches.",
                status=409,
            )
        response = _agent_broker().reject_pairing(
            pairing_id,
            database_uuid=pending.database_uuid,
        )
        return jsonify(response)
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route("/v1/main/agent/capabilities", methods=["GET"])
def list_main_agent_capabilities():
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        rows = _media_repository().list_agent_project_capabilities(
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        return jsonify(
            {
                "object": "agent.capability_list",
                "schema_version": "1",
                "capabilities": [_safe_core_capability(row) for row in rows],
            }
        )
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/agent/capabilities/<capability_id>/revoke/presentation",
    methods=["GET"],
)
def get_main_agent_capability_revoke_presentation(capability_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        capability_id = _protocol_identifier(capability_id)
        envelope = _media_repository().build_agent_capability_revoke_presentation(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        return jsonify(
            {
                "object": "agent.capability_revoke_presentation_envelope",
                "schema_version": "1",
                **envelope,
            }
        )
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/agent/capabilities/<capability_id>/revoke",
    methods=["POST"],
)
def revoke_main_agent_capability(capability_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        payload = strict_json_object(request)
        if set(payload) != {
            "presentation",
            "presentation_sha256",
            "native_gesture_nonce",
        }:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Capability revocation has an unsupported shape.",
                status=403,
            )
        capability_id = _protocol_identifier(capability_id)
        native_nonce = _native_gesture_nonce(payload["native_gesture_nonce"])
        repository = _media_repository()
        if type(payload["presentation"]) is not dict:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Capability revocation presentation is invalid.",
                status=403,
            )
        presentation_sha256 = _sha256_digest(
            payload["presentation_sha256"],
            code="agent_operation_request_mismatch",
        )
        capability = repository.get_agent_project_capability(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        if capability is None:
            raise AgentProtocolError(
                "agent_pairing_required",
                "The Agent capability is unavailable.",
                status=401,
            )
        revoked = repository.revoke_agent_project_capability(
            capability_id,
            runtime_authority_epoch=_runtime_authority_epoch(),
            presentation=payload["presentation"],
            presentation_sha256=presentation_sha256,
            native_gesture_nonce=native_nonce,
        )
        _agent_broker().revoke_runtime_capability(capability_id)
        _preview_broker().revoke_capability(capability_id)
        response = _safe_core_capability(revoked)
        response.update({"object": "agent.capability", "schema_version": "1"})
        return jsonify(response)
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/creative/projects/<project_id>/exports/presentation",
    methods=["POST"],
)
def build_main_canonical_export_presentation(project_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        payload = strict_json_object(request)
        if payload:
            raise CanonicalExportServiceError(
                "invalid_canonical_export_request",
                "Canonical export presentation request must be an empty object.",
            )
        result = _runtime_extension("canonical_export_service").presentation(
            project_id
        )
        return jsonify(result)
    except Exception as exc:
        return _canonical_export_exception(exc)


@api_blueprint.route(
    "/v1/main/creative/projects/<project_id>/exports",
    methods=["POST"],
)
def start_main_canonical_export(project_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        payload = strict_json_object(request)
        result = _runtime_extension("canonical_export_service").start(
            project_id,
            payload,
            runtime_epoch=_runtime_authority_epoch(),
            idempotency_key=_main_idempotency_key(),
        )
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except Exception as exc:
        return _canonical_export_exception(exc)


@api_blueprint.route(
    "/v1/main/creative/projects/<project_id>/blueprint/authority/presentation",
    methods=["POST"],
)
def build_main_blueprint_authority_presentation(project_id: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        project_id = _protocol_identifier(
            project_id,
            code="invalid_blueprint_decision_units",
        )
        payload = strict_json_object(request)
        if (
            set(payload) != {"operation", "decision_units"}
            or payload.get("operation") not in {"confirm", "revoke"}
            or type(payload.get("decision_units")) is not list
        ):
            raise AgentProtocolError(
                "invalid_blueprint_decision_units",
                "Authority presentation request has an unsupported shape.",
                status=422,
            )
        envelope = _media_repository().build_blueprint_authority_presentation(
            project_id,
            authority_operation=payload["operation"],
            decision_units=payload["decision_units"],
            runtime_authority_epoch=_runtime_authority_epoch(),
        )
        return jsonify(
            {
                "object": "creative_blueprint.authority_presentation_envelope",
                "schema_version": "1",
                **envelope,
            }
        )
    except Exception as exc:
        return _b1_exception(exc)


def _execute_main_blueprint_authority(project_id: str, *, operation: str):
    denied = _require_main_authority()
    if denied:
        return denied
    try:
        _agent_query_must_be_empty()
        project_id = _protocol_identifier(
            project_id,
            code="invalid_blueprint_decision_units",
        )
        payload = strict_json_object(request)
        if set(payload) != {"presentation", "presentation_sha256", "native_gesture_nonce"}:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Authority command has an unsupported shape.",
                status=403,
            )
        if type(payload["presentation"]) is not dict:
            raise AgentProtocolError(
                "native_confirmation_required",
                "Authority command presentation is invalid.",
                status=403,
            )
        presentation_sha256 = _sha256_digest(
            payload["presentation_sha256"],
            code="blueprint_confirmation_stale",
        )
        native_nonce = _native_gesture_nonce(payload["native_gesture_nonce"])
        arguments = {
            "runtime_authority_epoch": _runtime_authority_epoch(),
            "presentation": payload["presentation"],
            "presentation_sha256": presentation_sha256,
            "native_gesture_nonce": native_nonce,
            "idempotency_key": _main_idempotency_key(),
        }
        repository = _media_repository()
        if operation == "confirm":
            result = repository.confirm_blueprint_decision_units(project_id, **arguments)
        else:
            result = repository.revoke_blueprint_decision_units(project_id, **arguments)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = "true" if result.replayed else "false"
        return response
    except Exception as exc:
        return _b1_exception(exc)


@api_blueprint.route(
    "/v1/main/creative/projects/<project_id>/blueprint/authority/confirm",
    methods=["POST"],
)
def confirm_main_blueprint_authority(project_id: str):
    return _execute_main_blueprint_authority(project_id, operation="confirm")


@api_blueprint.route(
    "/v1/main/creative/projects/<project_id>/blueprint/authority/revoke",
    methods=["POST"],
)
def revoke_main_blueprint_authority(project_id: str):
    return _execute_main_blueprint_authority(project_id, operation="revoke")


@api_blueprint.route("/v1/creative/projects/<project_id>", methods=["GET"])
def get_creative_project(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        limit = int(request.args.get("history_limit", "50"))
        result = _runtime_extension("blueprint_service").project_workspace(
            project_id,
            limit=limit,
        )
        return jsonify(result.response), result.response_status
    except (BlueprintServiceError, BlueprintIntegrityError) as exc:
        return _blueprint_exception(exc)
    except CoverageIntegrityError as exc:
        return _coverage_exception(exc)
    except CanonicalTimelineIntegrityError as exc:
        return _timeline_lowering_exception(exc)
    except CanonicalExportIntegrityError as exc:
        return _canonical_export_exception(exc)
    except ValueError:
        return _media_error(
            "invalid_limit",
            "Workspace history limit must be between 1 and 100.",
            400,
        )


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/exports",
    methods=["GET"],
)
def list_project_canonical_exports(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        raw_limit = request.args.get("limit", "50")
        if set(request.args) - {"limit"}:
            raise ValueError("unsupported_query")
        result = _runtime_extension("canonical_export_service").list_jobs(
            project_id,
            limit=int(raw_limit),
        )
        return jsonify(result)
    except Exception as exc:
        return _canonical_export_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/exports/<job_id>",
    methods=["GET"],
)
def get_project_canonical_export(project_id: str, job_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        if request.args:
            raise ValueError("unsupported_query")
        result = _runtime_extension("canonical_export_service").read_job(
            project_id,
            job_id,
        )
        return jsonify(result)
    except Exception as exc:
        return _canonical_export_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/exports/revisions/<int:revision>",
    methods=["GET"],
)
def get_project_canonical_export_revision(project_id: str, revision: int):
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        if request.args:
            raise ValueError("unsupported_query")
        result = _runtime_extension("canonical_export_service").read_revision(
            project_id,
            revision,
        )
        return jsonify(result)
    except Exception as exc:
        return _canonical_export_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/blueprint",
    methods=["GET"],
)
def get_creative_blueprint(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    revision_raw = request.args.get("revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else None
        result = _runtime_extension("blueprint_service").get(project_id, revision=revision)
        return jsonify(result.response), result.response_status
    except (BlueprintServiceError, BlueprintIntegrityError) as exc:
        return _blueprint_exception(exc)
    except ValueError:
        return _media_error("invalid_revision", "Blueprint revision is invalid.", 400)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/blueprint/operations",
    methods=["GET"],
)
def get_creative_blueprint_operations(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    limit_raw = request.args.get("limit", "50")
    try:
        limit = int(limit_raw)
        result = _runtime_extension("blueprint_service").history(project_id, limit=limit)
        return jsonify(result.response), result.response_status
    except (BlueprintServiceError, BlueprintIntegrityError) as exc:
        return _blueprint_exception(exc)
    except ValueError:
        return _media_error("invalid_limit", "History limit must be between 1 and 100.", 400)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/blueprint/commit",
    methods=["POST"],
)
def commit_blueprint_proposal(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        payload = strict_json_object(request)
        result = _runtime_extension("blueprint_service").commit_proposal(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
        )
        return _blueprint_command_response(result)
    except (
        StrictJsonRequestError,
        BlueprintServiceError,
        BlueprintHeadConflictError,
        BlueprintReceiptConflictError,
        BlueprintIntegrityError,
    ) as exc:
        return _blueprint_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/blueprint/restore",
    methods=["POST"],
)
def restore_blueprint_revision(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise BlueprintServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Blueprint restore.",
            )
        payload = strict_json_object(request)
        result = _runtime_extension("blueprint_service").restore_revision(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        return _blueprint_command_response(result)
    except (
        StrictJsonRequestError,
        BlueprintServiceError,
        BlueprintHeadConflictError,
        BlueprintReceiptConflictError,
        BlueprintIntegrityError,
    ) as exc:
        return _blueprint_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/coverage",
    methods=["GET"],
)
def get_project_coverage(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    revision_raw = request.args.get("revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else None
        result = _runtime_extension("coverage_service").read(
            project_id,
            revision=revision,
        )
        return jsonify(result)
    except (
        CoverageServiceError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _coverage_exception(exc)
    except ValueError:
        return _media_error("invalid_revision", "Coverage revision is invalid.", 400)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/coverage/materialize",
    methods=["POST"],
)
def materialize_coverage_baseline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise CoverageServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Coverage materialization.",
            )
        payload = strict_json_object(request)
        result = _runtime_extension("coverage_service").materialize_baseline(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        CoverageServiceError,
        CoverageHeadConflictError,
        CoverageReceiptConflictError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _coverage_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline",
    methods=["GET"],
)
def get_project_timeline(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    revision_raw = request.args.get("revision")
    try:
        revision = int(revision_raw) if revision_raw is not None else None
        result = _runtime_extension("timeline_lowering_service").read(
            project_id,
            revision=revision,
        )
        return jsonify(result)
    except (
        TimelineLoweringServiceError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)
    except ValueError:
        return _media_error(
            "invalid_revision",
            "Canonical Timeline revision is invalid.",
            400,
        )


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline/materialize",
    methods=["POST"],
)
def materialize_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise TimelineLoweringServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Timeline materialization.",
            )
        payload = strict_json_object(request)
        _drop_preview_project_leases(project_id)
        result = _runtime_extension(
            "timeline_lowering_service"
        ).materialize_first_cut(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        _drop_preview_project_leases(project_id)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        TimelineLoweringServiceError,
        CanonicalTimelineHeadConflictError,
        CanonicalTimelineReceiptConflictError,
        CanonicalTimelineSourceUnavailableError,
        TimelineLoweringContractError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline/reconcile",
    methods=["POST"],
)
def reconcile_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise TimelineLoweringServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Timeline reconciliation.",
            )
        payload = strict_json_object(request)
        _drop_preview_project_leases(project_id)
        result = _runtime_extension(
            "timeline_lowering_service"
        ).reconcile_from_coverage(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        _drop_preview_project_leases(project_id)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        TimelineLoweringServiceError,
        CanonicalTimelineHeadConflictError,
        CanonicalTimelineReceiptConflictError,
        CanonicalTimelineSourceUnavailableError,
        TimelineLoweringContractError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline/edit",
    methods=["POST"],
)
def edit_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise TimelineLoweringServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Timeline editing.",
            )
        payload = strict_json_object(request)
        _drop_preview_project_leases(project_id)
        result = _runtime_extension("timeline_lowering_service").apply_edit(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        _drop_preview_project_leases(project_id)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        TimelineLoweringServiceError,
        CanonicalTimelineHeadConflictError,
        CanonicalTimelineReceiptConflictError,
        CanonicalTimelineSourceUnavailableError,
        TimelineLoweringContractError,
        TimelineEditContractError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline/structural-edit",
    methods=["POST"],
)
def structurally_edit_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise TimelineLoweringServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Timeline structural editing.",
            )
        payload = strict_json_object(request)
        _drop_preview_project_leases(project_id)
        result = _runtime_extension(
            "timeline_lowering_service"
        ).apply_structural_edit(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        _drop_preview_project_leases(project_id)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        TimelineLoweringServiceError,
        CanonicalTimelineHeadConflictError,
        CanonicalTimelineReceiptConflictError,
        CanonicalTimelineSourceUnavailableError,
        TimelineLoweringContractError,
        TimelineStructuralEditContractError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)


@api_blueprint.route(
    "/v1/creative/projects/<project_id>/timeline/restore",
    methods=["POST"],
)
def restore_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        expected_database_uuid = request.args.get("expected_database_uuid")
        if expected_database_uuid is None:
            raise TimelineLoweringServiceError(
                "database_identity_required",
                "The exact MemoLens database identity is required for Timeline restore.",
            )
        payload = strict_json_object(request)
        _drop_preview_project_leases(project_id)
        result = _runtime_extension("timeline_lowering_service").restore_revision(
            project_id,
            payload,
            idempotency_key=request.headers.get("Idempotency-Key", ""),
            expected_database_uuid=expected_database_uuid,
        )
        _drop_preview_project_leases(project_id)
        response = jsonify(result.response)
        response.status_code = result.response_status
        response.headers["Idempotency-Replayed"] = (
            "true" if result.replayed else "false"
        )
        return response
    except (
        StrictJsonRequestError,
        TimelineLoweringServiceError,
        CanonicalTimelineHeadConflictError,
        CanonicalTimelineReceiptConflictError,
        CanonicalTimelineSourceUnavailableError,
        TimelineLoweringContractError,
        TimelineRestoreContractError,
        CanonicalTimelineIntegrityError,
        CoverageIntegrityError,
        BlueprintIntegrityError,
    ) as exc:
        return _timeline_lowering_exception(exc)


@api_blueprint.route("/v1/creative/projects/<project_id>/timelines", methods=["POST"])
def create_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/creative/projects/{project_id}/timelines"
    try:
        payload = _json_object()
        unknown_fields = sorted(set(payload) - {"brief_revision", "db_path"})
        if unknown_fields:
            raise ValueError(
                "Unsupported Timeline creation fields: "
                + ", ".join(unknown_fields)
                + "."
            )
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        brief_revision = payload.get("brief_revision")
        if (
            not isinstance(brief_revision, int)
            or isinstance(brief_revision, bool)
            or brief_revision <= 0
        ):
            raise ValueError("`brief_revision` must be a positive integer.")
        result = _runtime_extension("timeline_service").create_from_project_idempotent(
            project_id,
            brief_revision=brief_revision,
            idempotency_scope=idempotency_scope,
            idempotency_key=key,
            request_sha256=request_hash,
        )
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except LookupError as exc:
        return _media_error("project_not_found", str(exc), 404)
    except TimelineValidationError as exc:
        return _media_error("invalid_timeline", str(exc), 422, details=exc.errors)
    except BlueprintTimelineCompilerUnavailable as exc:
        return _media_error(exc.code, str(exc), 409)
    except ResidualParentResolutionError as exc:
        return _media_error(exc.code, str(exc), 409)
    except BlueprintIntegrityError as exc:
        return _blueprint_exception(exc)
    except ValueError as exc:
        return _media_error("invalid_timeline_request", str(exc), 400)


@api_blueprint.route("/v1/timelines/<timeline_id>", methods=["GET"])
def get_timeline_revision(timeline_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    revision_raw = request.args.get("revision")
    try:
        revision = int(revision_raw) if revision_raw else None
    except ValueError:
        return _media_error("invalid_revision", "`revision` must be an integer.", 400)
    row = _media_repository().get_timeline(timeline_id, revision)
    if not row:
        return _media_error("timeline_not_found", "Timeline revision does not exist.", 404)
    return jsonify(
        {"object": "timeline.revision", "schema_version": "1", "id": timeline_id, **row, "timeline": row["timeline"]}
    )


@api_blueprint.route("/v1/timelines/<timeline_id>/revisions", methods=["GET"])
def get_timeline_revisions(timeline_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    revisions = _media_repository().timeline_revisions(timeline_id)
    if not revisions:
        return _media_error("timeline_not_found", "Timeline does not exist.", 404)
    return jsonify(
        {
            "object": "timeline.revision.list",
            "schema_version": "1",
            "id": timeline_id,
            "data": revisions,
            "revisions": revisions,
        }
    )


@api_blueprint.route("/v1/timelines/<timeline_id>/validate", methods=["POST"])
def validate_timeline(timeline_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    try:
        payload = _json_object()
        if isinstance(payload.get("timeline"), dict):
            timeline = payload["timeline"]
        else:
            revision = payload.get("revision")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise ValueError("`revision` or `timeline` is required.")
            row = _media_repository().get_timeline(timeline_id, revision)
            if not row:
                return _media_error("timeline_not_found", "Timeline revision does not exist.", 404)
            timeline = row["timeline"]
        result = _runtime_extension("timeline_service").validate(timeline)
        return jsonify(result), 200 if result["valid"] else 422
    except ValueError as exc:
        return _media_error("invalid_validation_request", str(exc), 400)


@api_blueprint.route("/v1/timelines/<timeline_id>/revise", methods=["POST"])
def revise_timeline(timeline_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/timelines/{timeline_id}/revise"
    try:
        payload = _json_object()
        base_revision = payload.get("base_revision")
        if not isinstance(base_revision, int) or isinstance(base_revision, bool):
            raise ValueError("`base_revision` must be an integer.")
        instruction = payload.get("instruction")
        raw_operations = payload.get("operations")
        if bool(isinstance(instruction, str) and instruction.strip()) == bool(
            isinstance(raw_operations, list) and raw_operations
        ):
            raise ValueError("Provide exactly one of `instruction` or non-empty `operations`.")
        service = _runtime_extension("timeline_service")
        operations = (
            service.instruction_operations(timeline_id, base_revision, instruction.strip())
            if isinstance(instruction, str)
            else service.normalize_operations(timeline_id, base_revision, raw_operations)
        )
        apply_revision = payload.get("apply", True)
        if not isinstance(apply_revision, bool):
            raise ValueError("`apply` must be a boolean.")
        if not apply_revision:
            preview = service.preview_revision(
                timeline_id,
                base_revision=base_revision,
                operations=operations,
            )
            return jsonify(
                {
                    "object": "timeline.revision_preview",
                    "schema_version": "1",
                    "id": f"{timeline_id}:{base_revision}:preview",
                    "preview": preview,
                    "operations": preview["operations"],
                    "diff": preview["diff"],
                }
            )
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        result = service.revise_idempotent(
            timeline_id,
            base_revision=base_revision,
            operations=operations,
            idempotency_scope=idempotency_scope,
            idempotency_key=key,
            request_sha256=request_hash,
        )
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except RuntimeError as exc:
        if str(exc).startswith("revision_conflict:"):
            return _media_error("revision_conflict", str(exc), 409)
        raise
    except LookupError as exc:
        return _media_error("timeline_not_found", str(exc), 404)
    except TimelineValidationError as exc:
        return _media_error("invalid_timeline", str(exc), 422, details=exc.errors)
    except BlueprintLegacyTimelineWriteUnavailable as exc:
        return _media_error(exc.code, str(exc), 409)
    except BlueprintIntegrityError as exc:
        return _blueprint_exception(exc)
    except ValueError as exc:
        return _media_error("invalid_revision_request", str(exc), 400)


def _verify_render_source_set_before_enqueue(
    repository,
    timeline: dict[str, object],
    profile: str,
) -> None:
    """Prove every immutable source binding without mutating source state."""

    plan = build_render_plan(timeline, profile)
    with SourceVerifier(repository, mark_changed=False) as verifier:
        for clip in plan.clips:
            verifier.verify(clip)
        verifier.verify_all()


@api_blueprint.route("/v1/renders", methods=["POST"])
def start_timeline_render():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/renders"
    try:
        payload = _json_object()
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        repository = _media_repository()
        replay = repository.replay_idempotent_write(
            scope=idempotency_scope,
            key=key,
            request_sha256=request_hash,
        )
        if replay is not None:
            job_id = str(replay.resource_id or replay.response.get("id") or "")
            current = repository.get_render_job(job_id)
            if current and current.get("status") in {"queued", "interrupted"} and not current.get("cancel_requested"):
                _runtime_extension("render_job_runner").submit(job_id)
            return jsonify(replay.response), replay.response_status
        timeline_id = str(payload.get("timeline_id") or "")
        revision = payload.get("timeline_revision")
        expected_hash = payload.get("expected_timeline_sha256")
        profile = str(payload.get("profile") or "preview-low")
        if not timeline_id or not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("`timeline_id` and integer `timeline_revision` are required.")
        row = repository.get_timeline(timeline_id, revision)
        if not row:
            return _media_error("timeline_not_found", "Timeline revision does not exist.", 404)
        if not isinstance(expected_hash, str) or expected_hash != row["content_sha256"]:
            return _media_error(
                "timeline_hash_mismatch",
                "Expected timeline SHA-256 does not match.",
                409,
            )
        if not isinstance(payload.get("output"), dict):
            raise ValueError("`output` with the app preview `root_id` is required.")
        output = payload["output"]
        root_id = output.get("root_id")
        if root_id == "app-preview-root":
            root_id = _runtime_extension("app_preview_root_id")
        if root_id != _runtime_extension("app_preview_root_id"):
            return _media_error(
                "export_grant_required",
                "Only app-managed render artifacts are supported.",
                403,
            )
        if profile not in {"preview-low", "export-1080p"}:
            raise ValueError("Unsupported render profile.")
        if profile == "export-1080p":
            return _media_error(
                "export_grant_required",
                "Final 1080p export requires an Electron-issued export grant and is not exposed in this release.",
                403,
            )
        _verify_render_source_set_before_enqueue(
            repository,
            row["timeline"],
            profile,
        )

        def mutation(connection):
            if (
                repository.get_blueprint_head_in_transaction(
                    connection,
                    str(row["project_id"]),
                )
                is not None
            ):
                raise BlueprintLegacyTimelineWriteUnavailable()
            job = repository.create_render_job(
                timeline_id=timeline_id,
                timeline_revision=revision,
                profile=profile,
                output_root_id=str(root_id),
                output_relative_path=None,
                timeline_content_sha256=str(expected_hash),
                reuse_active=True,
                connection=connection,
            )
            public = _public_job(job, render=True)
            return {"job": public, **public}, 202, str(job["id"])

        result = repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=key,
            request_sha256=request_hash,
            resource_type="render_job",
            mutation=mutation,
        )
        job_id = str(result.resource_id or result.response.get("id") or "")
        current = repository.get_render_job(job_id)
        if current and current.get("status") in {"queued", "interrupted"} and not current.get("cancel_requested"):
            _runtime_extension("render_job_runner").submit(job_id)
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except BlueprintLegacyTimelineWriteUnavailable as exc:
        return _media_error(exc.code, str(exc), 409)
    except BlueprintIntegrityError as exc:
        return _blueprint_exception(exc)
    except MediaCapabilityError as exc:
        return _media_error(exc.code, str(exc), 409)
    except ValueError as exc:
        return _media_error("invalid_render_request", str(exc), 400)


@api_blueprint.route("/v1/renders/<job_id>", methods=["GET"])
def get_timeline_render(job_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    job = _media_repository().get_render_job(job_id)
    if not job:
        return _media_error("render_not_found", "Render job does not exist.", 404)
    public = _public_job(job, render=True)
    return jsonify({"job": public, **public})


@api_blueprint.route("/v1/renders", methods=["GET"])
def list_timeline_renders():
    denied = _require_media_read_access()
    if denied:
        return denied
    active = request.args.get("active", "true").casefold() != "false"
    try:
        limit = max(1, min(int(request.args.get("limit", "50")), 100))
    except ValueError:
        return _media_error("invalid_limit", "`limit` must be an integer.", 400)
    jobs = [_public_job(job, render=True) for job in _media_repository().list_render_jobs(active=active, limit=limit)]
    return jsonify({"object": "render.job.list", "schema_version": "1", "data": jobs, "jobs": jobs})


@api_blueprint.route("/v1/renders/<job_id>/cancel", methods=["POST"])
def cancel_timeline_render(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/renders/{job_id}/cancel"
    try:
        payload = request.get_json(silent=True) or {}
        idempotency_scope, key, request_hash = _atomic_idempotency_context(scope, payload)
        repository = _media_repository()

        def mutation(connection):
            if not repository.request_render_cancel(job_id, connection=connection):
                raise ValueError("Render cannot be cancelled.")
            job = repository.get_render_job_in_transaction(connection, job_id)
            public = _public_job(job, render=True)
            return {"job": public, **public}, 202, job_id

        result = repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=key,
            request_sha256=request_hash,
            resource_type="render_job_cancel",
            mutation=mutation,
        )
        return jsonify(result.response), result.response_status
    except IdempotencyConflictError as exc:
        return _idempotency_exception(exc)
    except InvalidIdempotencyKeyError as exc:
        return _media_error("invalid_idempotency_key", str(exc), 400)
    except ValueError as exc:
        return _media_error("render_not_cancellable", str(exc), 409)


@api_blueprint.route("/v1/renders/<job_id>/download", methods=["GET"])
def download_timeline_render(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    resolved = _media_repository().open_render_artifact(job_id)
    if not resolved:
        return _media_error("artifact_unavailable", "A verified render artifact is unavailable.", 404)
    job, handle, size = resolved
    return _stream_verified_handle(
        handle,
        size,
        mimetype="video/mp4",
        etag=str(job["output_sha256"]),
        filename=f"MemoLens-{job_id}.mp4",
    )
