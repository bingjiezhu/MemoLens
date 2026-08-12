from __future__ import annotations

import hashlib
import hmac
import mimetypes
import os
import re
import sqlite3
import stat
import uuid
from io import BytesIO
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, jsonify, request, send_file
from PIL import Image, ImageOps, UnidentifiedImageError

from core.app_settings import load_persisted_app_settings, save_persisted_app_settings
from core.db import ImageIndexRepository
from core.media_db import IdempotencyConflictError, canonical_json
from core.local_model_runtime import detect_local_model_runtime
from core.photo_atlas import (
    AtlasFilters,
    PhotoAtlasService,
    normalize_lens,
    normalize_mode,
    parse_bool,
    parse_float,
    parse_limit,
)
from core.schemas import RetrievedImageSummary, parse_indexing_request, parse_retrieval_request
from backend.src import (
    MEMOLENS_API_VERSION,
    MEMOLENS_SERVICE_ID,
    reload_runtime,
)
from indexing.files import ensure_heif_support
from backend.src.media.importing import MediaImportService
from backend.src.media.render import ffmpeg_encode_capability
from backend.src.media.director import CreativeBriefError
from backend.src.media.timeline import TimelineValidationError
from backend.src.media.video import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    binary_capability,
    sha256_file,
)
from backend.src.security import is_local_remote_addr as _is_local_remote_addr


ensure_heif_support()

api_blueprint = Blueprint("api", __name__)


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
    return current_app.extensions["media_repository"]


MEDIA_ROUTE_PREFIXES = (
    "/v1/media/",
    "/v1/assets/",
    "/v1/index/jobs",
    "/v1/search/mixed",
    "/v1/video-segments/",
    "/v1/keyframes/",
    "/v1/creative/",
    "/v1/timelines/",
    "/v1/renders",
)


@api_blueprint.before_request
def _verify_media_database_binding():
    if not request.path.startswith(MEDIA_ROUTE_PREFIXES):
        return None
    raw_path = request.args.get("db_path")
    if raw_path is None and request.is_json:
        payload = request.get_json(silent=True)
        if isinstance(payload, dict):
            raw_path = payload.get("db_path")
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


def _idempotency_begin(scope: str, payload: dict[str, object]):
    key = request.headers.get("Idempotency-Key", "").strip()
    if not key or len(key) > 200:
        raise ValueError("A valid `Idempotency-Key` header is required.")
    request_hash = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    existing = _media_repository().claim_idempotency(
        scope=f"desktop:{scope}",
        key=key,
        request_sha256=request_hash,
        resource_type=scope,
        resource_id=None,
    )
    if existing and existing.get("state") in {"completed", "failed"}:
        return key, existing
    if existing and existing.get("state") == "in_progress":
        return key, existing
    return key, None


def _idempotency_finish(scope: str, key: str, body: dict[str, object], status: int) -> None:
    _media_repository().complete_idempotency(
        scope=f"desktop:{scope}",
        key=key,
        response_status=status,
        response=body,
        failed=status >= 400,
    )


def _idempotency_failure(
    scope: str | None,
    key: str | None,
    code: str,
    message: str,
    status: int,
    *,
    details: object | None = None,
):
    body = _media_error_payload(code, message, status, details=details)
    if scope and key:
        _idempotency_finish(scope, key, body, status)
    return jsonify(body), status


def _replay_idempotency(existing: dict[str, object]):
    response = existing.get("response")
    if isinstance(response, dict):
        return jsonify(response), int(existing.get("response_status") or 200)
    body = _media_error_payload(
        "request_in_progress",
        "An identical request is still in progress.",
        409,
        details={"retry_after_seconds": 1},
    )
    body["error"]["retryable"] = True
    return jsonify(body), 409


def _public_job(job: dict[str, object], *, render: bool = False) -> dict[str, object]:
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


def _resolve_existing_db_path(raw_value: str) -> Path:
    path = Path(raw_value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    if not path.is_file():
        raise ValueError("`db_path` must point to a SQLite file, not a directory.")
    return path


def _resolve_index_db_path(raw_value: str) -> Path:
    path = Path(raw_value).expanduser().resolve()
    if path.exists() and not path.is_file():
        raise ValueError("`db_path` must point to a SQLite file, not a directory.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


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


def _open_library_file(relative_path: str, *, root_path_override: str | None):
    if Path(relative_path).suffix.lower() not in SUPPORTED_LIBRARY_FILE_EXTENSIONS:
        abort(404)

    settings = current_app.config["SETTINGS"]
    library_root = settings.image_library_dir.resolve(strict=True)
    if root_path_override is not None:
        if not isinstance(root_path_override, str) or not root_path_override.strip():
            abort(404)
        try:
            requested_root = Path(root_path_override).expanduser().resolve(strict=True)
        except OSError:
            abort(404)
        if requested_root != library_root:
            abort(403)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None
    try:
        root_fd = os.open(library_root, flags)
        file_fd = _media_repository()._open_relative_regular(root_fd, relative_path)
    except (OSError, ValueError):
        if root_fd is not None:
            os.close(root_fd)
        abort(404)
    try:
        metadata = os.fstat(file_fd)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(file_fd)
            abort(404)
        digest = hashlib.sha256()
        with os.fdopen(os.dup(file_fd), "rb") as verification:
            for chunk in iter(lambda: verification.read(1024 * 1024), b""):
                digest.update(chunk)
        return os.fdopen(file_fd, "rb"), metadata.st_size, digest.hexdigest()
    finally:
        assert root_fd is not None
        os.close(root_fd)


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
        if not relative_path or not filename or not description:
            raise ValueError("`images` entries must include `filename`, `relative_path`, and `description`.")

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
        return current_app.extensions["photo_atlas_service"]
    if not isinstance(raw_db_path, str) or not raw_db_path.strip():
        raise ValueError("`db_path` must be a non-empty string when set.")
    repository = ImageIndexRepository(_resolve_existing_db_path(raw_db_path))
    repository.ensure_schema()
    return PhotoAtlasService(repository)


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


@api_blueprint.route("/healthz", methods=["GET"])
def healthz():
    expected_token = str(current_app.config.get("DESKTOP_SESSION_TOKEN") or "")
    challenge = request.args.get("challenge", "")
    proof = None
    if expected_token and re.fullmatch(r"[0-9a-f]{64}", challenge):
        proof = hmac.new(
            expected_token.encode("utf-8"),
            challenge.encode("ascii"),
            "sha256",
        ).hexdigest()
    return jsonify(
        {
            "status": "ok",
            "object": "health.check",
            "service": MEMOLENS_SERVICE_ID,
            "api_version": MEMOLENS_API_VERSION,
            "challenge_proof": proof,
        }
    )


@api_blueprint.route("/v1/index/status", methods=["GET"])
def get_index_status():
    raw_db_path = request.args.get("db_path")
    if raw_db_path is None:
        repository = current_app.extensions["image_index_repository"]
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
        index_stats = repository.summarize_index_health()
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
    settings = current_app.config["SETTINGS"]
    persisted = load_persisted_app_settings(settings.app_state_dir)
    local_model_runtime = detect_local_model_runtime(settings.vlm_profile_catalog)
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
            "index_stats": current_app.extensions["image_index_repository"].summarize_index_health(),
        }
    )


@api_blueprint.route("/v1/settings", methods=["PUT"])
def update_settings():
    if not _request_is_local():
        return _local_only_error()

    media_repository = current_app.extensions.get("media_repository")
    if media_repository is not None and media_repository.active_job_count() > 0:
        return _media_error(
            "media_jobs_active",
            "Cancel or wait for active media jobs before changing MemoLens settings.",
            409,
        )

    settings = current_app.config["SETTINGS"]
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
        if not isinstance(process_image_width, int) or process_image_width <= 0:
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

    try:
        persisted = save_persisted_app_settings(settings.app_state_dir, normalized_payload)
        reloaded = reload_runtime(current_app)
    except ValueError as exc:
        return _error_response(str(exc))

    return jsonify(
        {
            "object": "memolens.settings",
            "effective": {
                "image_library_dir": str(reloaded.image_library_dir),
                "db_path": str(reloaded.db_path),
                "app_state_dir": str(reloaded.app_state_dir),
                "settings_path": str(reloaded.persisted_settings_path),
                "process_image_width": reloaded.process_image_width,
                "vision_profile_name": reloaded.vision_profile_name,
                "query_profile_name": reloaded.query_profile_name,
                "embedding_backend": reloaded.embedding_backend,
            },
            "persisted": persisted.to_dict(),
            "available_vlm_profiles": list(reloaded.available_vlm_profiles),
            "vlm_profile_catalog": [entry.to_dict() for entry in reloaded.vlm_profile_catalog],
            "local_model_runtime": detect_local_model_runtime(reloaded.vlm_profile_catalog).to_dict(),
        }
    )


@api_blueprint.route("/v1/indexing/jobs", methods=["POST"])
def create_indexing_job():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response("Indexing payload must be a JSON object.")
    settings = current_app.config["SETTINGS"]
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

    indexing_service = current_app.extensions["indexing_service"]
    db_path_override = indexing_request.db_path

    try:
        if isinstance(db_path_override, str) and db_path_override.strip():
            repository = ImageIndexRepository(_resolve_index_db_path(db_path_override))
            repository.ensure_schema()
            media_repository = None
            if repository.db_path.resolve() == current_app.extensions["media_repository"].db_path.resolve():
                media_repository = current_app.extensions["media_repository"]
            indexing_service = indexing_service.__class__(
                settings=settings,
                repository=repository,
                vision_client=current_app.extensions["vision_client"],
                embedding_service=current_app.extensions["embedding_service"],
                text_embedding_service=current_app.extensions["text_embedding_service"],
                geocoder=current_app.extensions["geocoder"],
                media_repository=media_repository,
            )
        result = indexing_service.run(indexing_request)
    except (FileNotFoundError, ValueError, OSError) as exc:
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

    response_body = result.to_response(include_records=include_records)
    if not result.indexed and not result.skipped:
        if result.failed:
            response_body["status"] = "failed"
            response_body["message"] = "All candidate images failed to index."
        else:
            response_body["status"] = "empty"
            response_body["message"] = "No supported images were found to index."
    elif result.failed:
        response_body["status"] = "partial"
        response_body["message"] = "Indexing completed with some failed images."

    return jsonify(response_body)


@api_blueprint.route("/v1/retrieval/query", methods=["POST"])
def create_retrieval_query():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return _error_response("Retrieval payload must be a JSON object.")
    settings = current_app.config["SETTINGS"]
    include_copy = payload.get("include_copy", True)

    if not isinstance(include_copy, bool):
        return _error_response("`include_copy` must be a boolean when set.")

    try:
        retrieval_request = parse_retrieval_request(payload)
    except ValueError as exc:
        return _error_response(str(exc))

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
            repository = ImageIndexRepository(_resolve_existing_db_path(db_path_override))
        except (FileNotFoundError, ValueError) as exc:
            return _error_response(str(exc))
        retrieval_service = current_app.extensions["retrieval_service"].__class__(
            settings=settings,
            repository=repository,
            planner=current_app.extensions["query_planner"],
            text_embedding_service=current_app.extensions["text_embedding_service"],
        )
    else:
        retrieval_service = current_app.extensions["retrieval_service"]

    copywriter = current_app.extensions["retrieval_copywriter"]
    result = retrieval_service.run(retrieval_request)
    body = result.to_response()
    body["candidate_count"] = len(result.data)

    if include_copy and result.status == "completed" and result.data:
        try:
            generated_copy = copywriter.generate(
                query_text=result.query_text,
                retrieved_images=result.data,
                image_library_dir=image_library_dir,
                # Compose from text already stored in the index. Retrieval
                # must never upload original photo bytes to a copy provider.
                image_limit=0,
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
    settings = current_app.config["SETTINGS"]
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

    copywriter = current_app.extensions["retrieval_copywriter"]
    try:
        generated_copy = copywriter.generate(
            query_text=query_text.strip(),
            retrieved_images=retrieved_images,
            image_library_dir=image_library_dir,
            # This endpoint is a text-only composition boundary. Photo bytes
            # may leave the device only during an explicit indexing request.
            image_limit=0,
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
        planner = current_app.extensions["query_planner"]
        suggestions = planner.generate_search_suggestions(
            library_summary=library_summary,
            memories=[memory for memory in memories if isinstance(memory, dict)],
            context_assets=context_assets,
            count=count,
        )
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

    raw_asset_ids = payload.get("asset_ids")
    asset_ids = (
        [str(item).strip() for item in raw_asset_ids if isinstance(item, str) and item.strip()]
        if isinstance(raw_asset_ids, list)
        else None
    )
    selected_memory_ids = _string_list_from_payload(payload, "selected_memory_ids")

    try:
        service = _atlas_service_for_db_path(payload.get("db_path"))
        if not asset_ids and selected_memory_ids:
            asset_ids = service.asset_ids_for_memories(selected_memory_ids)
        result = service.generate(
            text=text.strip(),
            top_k=top_k,
            mode=normalize_mode(payload.get("mode") if isinstance(payload.get("mode"), str) else None),
            cluster_id=payload.get("cluster_id") if isinstance(payload.get("cluster_id"), str) else None,
            asset_ids=asset_ids,
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
        settings = current_app.config["SETTINGS"]
        try:
            image_library_dir = _resolve_image_library_dir(
                settings=settings,
                raw_value=payload.get("image_library_dir"),
                allow_remote_override=True,
            )
            generated_copy = current_app.extensions["retrieval_copywriter"].generate(
                query_text=text.strip(),
                retrieved_images=_parse_copywriter_images(result.get("data")),
                image_library_dir=image_library_dir,
                image_limit=0,
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
            "preview_root_id": current_app.extensions["app_preview_root_id"],
            "write_requires_desktop_token": True,
        }
    )


def _import_root(payload: dict[str, object]) -> tuple[str, Path]:
    repository = _media_repository()
    root_id = payload.get("library_root_id")
    if isinstance(root_id, str) and root_id:
        root = repository.library_root(root_id)
        if not root or root.get("status") != "active":
            raise ValueError("Approved library root is unavailable.")
        return root_id, repository.validate_library_root(root_id)
    # Compatibility bridge: only the already persisted Settings root is accepted.
    raw_path = payload.get("root_path")
    configured = current_app.config["SETTINGS"].image_library_dir.resolve(strict=True)
    if raw_path is not None:
        if not isinstance(raw_path, str) or Path(raw_path).expanduser().resolve(strict=True) != configured:
            raise ValueError("`root_path` must equal the directory already approved in MemoLens Settings.")
    registered = repository.register_library_root(configured)
    return str(registered["id"]), configured


@api_blueprint.route("/v1/assets/import", methods=["POST"])
def import_media_assets():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/assets/import"
    idempotency_key: str | None = None
    try:
        payload = _json_object()
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        root_id, root = _import_root(payload)
        result = MediaImportService(
            _media_repository(),
            current_app.extensions["media_job_runner"],
        ).import_assets(root_id=root_id, root=root, payload=payload)
        jobs = [_public_job(job) for job in result.jobs]
        body: dict[str, object] = {
            "object": "asset.import",
            "schema_version": "1",
            "id": f"import_{uuid.uuid4().hex}",
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
        _idempotency_finish(scope, idempotency_key, body, 202 if jobs else 200)
        return jsonify(body), 202 if jobs else 200
    except IdempotencyConflictError as exc:
        return _media_error("idempotency_conflict", str(exc), 409)
    except (FileNotFoundError, OSError, ValueError) as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_import", str(exc), 400)


@api_blueprint.route("/v1/index/jobs/<job_id>", methods=["GET"])
def get_media_index_job(job_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    job = _media_repository().get_media_job(job_id)
    if not job:
        return _media_error("job_not_found", "Media job does not exist.", 404)
    return jsonify({"job": _public_job(job), **_public_job(job)})


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
    jobs = [_public_job(job) for job in _media_repository().list_media_jobs(active=active, limit=limit)]
    return jsonify({"object": "media.job.list", "schema_version": "1", "data": jobs, "jobs": jobs})


@api_blueprint.route("/v1/index/jobs/<job_id>/cancel", methods=["POST"])
def cancel_media_index_job(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/index/jobs/{job_id}/cancel"
    idempotency_key: str | None = None
    try:
        payload = request.get_json(silent=True) or {}
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        if not current_app.extensions["media_job_runner"].cancel(job_id):
            return _idempotency_failure(
                scope, idempotency_key, "job_not_cancellable", "Media job cannot be cancelled.", 409
            )
        job = _media_repository().get_media_job(job_id)
        body = {"job": _public_job(job)}
        _idempotency_finish(scope, idempotency_key, body, 202)
        return jsonify(body), 202
    except (IdempotencyConflictError, ValueError) as exc:
        code = "idempotency_conflict" if isinstance(exc, IdempotencyConflictError) else "invalid_cancel_request"
        return _idempotency_failure(scope, idempotency_key, code, str(exc), 409)


@api_blueprint.route("/v1/index/jobs/<job_id>/resume", methods=["POST"])
def resume_media_index_job(job_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/index/jobs/{job_id}/resume"
    idempotency_key: str | None = None
    try:
        payload = request.get_json(silent=True) or {}
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        if not current_app.extensions["media_job_runner"].resume(job_id):
            return _idempotency_failure(
                scope, idempotency_key, "job_not_resumable", "Media job cannot be resumed.", 409
            )
        job = _media_repository().get_media_job(job_id)
        body = {"job": _public_job(job)}
        _idempotency_finish(scope, idempotency_key, body, 202)
        return jsonify(body), 202
    except (IdempotencyConflictError, ValueError) as exc:
        code = "idempotency_conflict" if isinstance(exc, IdempotencyConflictError) else "invalid_resume_request"
        return _idempotency_failure(scope, idempotency_key, code, str(exc), 409)


@api_blueprint.route("/v1/search/mixed", methods=["POST"])
def mixed_media_search():
    denied = _require_media_read_access()
    if denied:
        return denied
    try:
        result = current_app.extensions["mixed_retrieval_service"].search(_json_object())
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


def _keyframe_file(keyframe_id: str) -> Path | None:
    repository = _media_repository()
    with repository._connect() as connection:
        row = connection.execute("SELECT cache_key,sha256 FROM keyframes WHERE id=?", (keyframe_id,)).fetchone()
    if not row:
        return None
    root = current_app.config["SETTINGS"].app_state_dir / "media-cache"
    path = root.joinpath(str(row["cache_key"])).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    if path.is_symlink() or not path.is_file() or sha256_file(path) != row["sha256"]:
        return None
    return path


@api_blueprint.route("/v1/keyframes/<keyframe_id>", methods=["GET"])
def get_keyframe(keyframe_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    path = _keyframe_file(keyframe_id)
    if path is None:
        return _media_error("keyframe_unavailable", "Keyframe is unavailable.", 404)
    return _private_media(send_file(path, mimetype="image/jpeg", conditional=True))


@api_blueprint.route("/v1/video-segments/<segment_id>/thumbnail", methods=["GET"])
def get_segment_thumbnail(segment_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    segment = _media_repository().get_segment(segment_id)
    if not segment or not segment.get("keyframes"):
        return _media_error("keyframe_unavailable", "Segment thumbnail is unavailable.", 404)
    path = _keyframe_file(str(segment["keyframes"][0]["id"]))
    if path is None:
        return _media_error("keyframe_unavailable", "Segment thumbnail is unavailable.", 404)
    return _private_media(send_file(path, mimetype="image/jpeg", conditional=True))


@api_blueprint.route("/v1/creative/briefs", methods=["POST"])
def create_creative_brief():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/creative/briefs"
    idempotency_key: str | None = None
    try:
        payload = _json_object()
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        project, search = current_app.extensions["creative_director"].create_brief(payload)
        body = {
            "object": "creative.project",
            "schema_version": "1",
            "project": project,
            "search": search,
            "id": project["id"],
        }
        _idempotency_finish(scope, idempotency_key, body, 201)
        return jsonify(body), 201
    except IdempotencyConflictError as exc:
        return _media_error("idempotency_conflict", str(exc), 409)
    except CreativeBriefError as exc:
        return _idempotency_failure(scope, idempotency_key, exc.code, str(exc), 400, details=exc.details)
    except ValueError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_brief", str(exc), 400)


@api_blueprint.route("/v1/creative/projects/<project_id>", methods=["GET"])
def get_creative_project(project_id: str):
    denied = _require_media_read_access()
    if denied:
        return denied
    project = _media_repository().get_project(project_id)
    if not project:
        return _media_error("project_not_found", "Creative project does not exist.", 404)
    project.update({"object": "creative.project", "schema_version": "1"})
    return jsonify({"project": project, **project})


@api_blueprint.route("/v1/creative/projects/<project_id>/timelines", methods=["POST"])
def create_project_timeline(project_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/creative/projects/{project_id}/timelines"
    idempotency_key: str | None = None
    try:
        payload = _json_object()
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        brief_revision = payload.get("brief_revision")
        if not isinstance(brief_revision, int) or isinstance(brief_revision, bool):
            raise ValueError("`brief_revision` must be a positive integer.")
        result = current_app.extensions["timeline_service"].create_from_project(
            project_id, brief_revision=brief_revision
        )
        body = {"object": "timeline.revision", "schema_version": "1", "id": result["timeline"]["id"], **result}
        _idempotency_finish(scope, idempotency_key, body, 201)
        return jsonify(body), 201
    except IdempotencyConflictError as exc:
        return _media_error("idempotency_conflict", str(exc), 409)
    except LookupError as exc:
        return _idempotency_failure(scope, idempotency_key, "project_not_found", str(exc), 404)
    except TimelineValidationError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_timeline", str(exc), 422, details=exc.errors)
    except ValueError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_timeline_request", str(exc), 400)


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
        result = current_app.extensions["timeline_service"].validate(timeline)
        return jsonify(result), 200 if result["valid"] else 422
    except ValueError as exc:
        return _media_error("invalid_validation_request", str(exc), 400)


@api_blueprint.route("/v1/timelines/<timeline_id>/revise", methods=["POST"])
def revise_timeline(timeline_id: str):
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = f"POST:/v1/timelines/{timeline_id}/revise"
    idempotency_key: str | None = None
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
        service = current_app.extensions["timeline_service"]
        operations = (
            service.instruction_operations(timeline_id, base_revision, instruction.strip())
            if isinstance(instruction, str)
            else service.normalize_operations(timeline_id, base_revision, raw_operations)
        )
        apply_revision = payload.get("apply", True)
        if not isinstance(apply_revision, bool):
            raise ValueError("`apply` must be a boolean.")
        preview = service.preview_revision(timeline_id, base_revision=base_revision, operations=operations)
        if not apply_revision:
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
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        result = service.revise(timeline_id, base_revision=base_revision, operations=operations)
        body = {"object": "timeline.revision", "schema_version": "1", "id": timeline_id, **result}
        _idempotency_finish(scope, idempotency_key, body, 201)
        return jsonify(body), 201
    except IdempotencyConflictError as exc:
        return _media_error("idempotency_conflict", str(exc), 409)
    except RuntimeError as exc:
        if str(exc).startswith("revision_conflict:"):
            return _idempotency_failure(scope, idempotency_key, "revision_conflict", str(exc), 409)
        raise
    except LookupError as exc:
        return _idempotency_failure(scope, idempotency_key, "timeline_not_found", str(exc), 404)
    except TimelineValidationError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_timeline", str(exc), 422, details=exc.errors)
    except ValueError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_revision_request", str(exc), 400)


@api_blueprint.route("/v1/renders", methods=["POST"])
def start_timeline_render():
    denied = _require_media_desktop_token()
    if denied:
        return denied
    scope = "POST:/v1/renders"
    idempotency_key: str | None = None
    try:
        payload = _json_object()
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        timeline_id = str(payload.get("timeline_id") or "")
        revision = payload.get("timeline_revision")
        expected_hash = payload.get("expected_timeline_sha256")
        profile = str(payload.get("profile") or "preview-low")
        if not timeline_id or not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("`timeline_id` and integer `timeline_revision` are required.")
        row = _media_repository().get_timeline(timeline_id, revision)
        if not row:
            return _idempotency_failure(
                scope, idempotency_key, "timeline_not_found", "Timeline revision does not exist.", 404
            )
        if not isinstance(expected_hash, str) or expected_hash != row["content_sha256"]:
            return _idempotency_failure(
                scope,
                idempotency_key,
                "timeline_hash_mismatch",
                "Expected timeline SHA-256 does not match.",
                409,
            )
        if not isinstance(payload.get("output"), dict):
            raise ValueError("`output` with the app preview `root_id` is required.")
        output = payload["output"]
        root_id = output.get("root_id")
        if root_id == "app-preview-root":
            root_id = current_app.extensions["app_preview_root_id"]
        if root_id != current_app.extensions["app_preview_root_id"]:
            return _idempotency_failure(
                scope,
                idempotency_key,
                "export_grant_required",
                "Only app-managed render artifacts are supported.",
                403,
            )
        if profile not in {"preview-low", "export-1080p"}:
            raise ValueError("Unsupported render profile.")
        if profile == "export-1080p":
            return _idempotency_failure(
                scope,
                idempotency_key,
                "export_grant_required",
                "Final 1080p export requires an Electron-issued export grant and is not exposed in this release.",
                403,
            )
        job_id = f"render_{uuid.uuid4().hex}"
        job = _media_repository().create_render_job(
            timeline_id=timeline_id,
            timeline_revision=revision,
            profile=profile,
            output_root_id=str(root_id),
            output_relative_path=f"{job_id}.mp4",
            timeline_content_sha256=str(expected_hash),
            reuse_active=True,
        )
        # Repository owns the real id; use it in the safe app-managed filename.
        if job["output_relative_path"] != f"{job['id']}.mp4":
            with _media_repository().transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE render_jobs SET output_relative_path=? WHERE id=?", (f"{job['id']}.mp4", job["id"])
                )
            job = _media_repository().get_render_job(str(job["id"]))
        current_app.extensions["render_job_runner"].submit(str(job["id"]))
        public = _public_job(job, render=True)
        body = {"job": public, **public}
        _idempotency_finish(scope, idempotency_key, body, 202)
        return jsonify(body), 202
    except IdempotencyConflictError as exc:
        return _media_error("idempotency_conflict", str(exc), 409)
    except ValueError as exc:
        return _idempotency_failure(scope, idempotency_key, "invalid_render_request", str(exc), 400)


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
    idempotency_key: str | None = None
    try:
        payload = request.get_json(silent=True) or {}
        idempotency_key, existing = _idempotency_begin(scope, payload)
        if existing:
            return _replay_idempotency(existing)
        if not current_app.extensions["render_job_runner"].cancel(job_id):
            return _idempotency_failure(
                scope, idempotency_key, "render_not_cancellable", "Render cannot be cancelled.", 409
            )
        job = _media_repository().get_render_job(job_id)
        public = _public_job(job, render=True)
        body = {"job": public, **public}
        _idempotency_finish(scope, idempotency_key, body, 202)
        return jsonify(body), 202
    except (IdempotencyConflictError, ValueError) as exc:
        code = "idempotency_conflict" if isinstance(exc, IdempotencyConflictError) else "invalid_cancel_request"
        return _idempotency_failure(scope, idempotency_key, code, str(exc), 409)


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
