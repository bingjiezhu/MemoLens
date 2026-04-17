from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from core.app_settings import load_persisted_app_settings, save_persisted_app_settings
from core.db import ImageIndexRepository
from core.schemas import RetrievedImageSummary, parse_indexing_request, parse_retrieval_request
from backend.src import reload_runtime


api_blueprint = Blueprint("api", __name__)
LOCAL_CLIENT_ADDRESSES = {"127.0.0.1", "::1"}


def _is_local_remote_addr(remote_addr: str | None) -> bool:
    normalized = str(remote_addr or "").strip()
    if not normalized:
        return True
    if normalized in LOCAL_CLIENT_ADDRESSES:
        return True
    return normalized.startswith("::ffff:127.0.0.1")


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
    if raw_value is not None and (
        not isinstance(raw_value, str) or not raw_value.strip()
    ):
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
            raise ValueError(
                "`images` entries must include `filename`, `relative_path`, and `description`."
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
                matched_terms=[
                    str(term).strip()
                    for term in item.get("matched_terms", [])
                    if str(term).strip()
                ]
                if isinstance(item.get("matched_terms"), list)
                else [],
            )
        )

    return parsed


@api_blueprint.route("/healthz", methods=["GET"])
def healthz():
    settings = current_app.config["SETTINGS"]
    repository = current_app.extensions["image_index_repository"]
    return jsonify(
        {
            "status": "ok",
            "object": "health.check",
            "image_library_dir": str(settings.image_library_dir),
            "db_path": str(settings.db_path),
            "app_state_dir": str(settings.app_state_dir),
            "settings_path": str(settings.persisted_settings_path),
            "vision_profile": settings.vision_profile_name,
            "query_profile": settings.query_profile_name,
            "embedding_backend": settings.embedding_backend,
            "available_vlm_profiles": list(settings.available_vlm_profiles),
            "index_stats": repository.summarize_index_health(),
        }
    )


@api_blueprint.route("/v1/settings", methods=["GET"])
def get_settings():
    settings = current_app.config["SETTINGS"]
    persisted = load_persisted_app_settings(settings.app_state_dir)
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
        }
    )


@api_blueprint.route("/v1/settings", methods=["PUT"])
def update_settings():
    if not _request_is_local():
        return _local_only_error()

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
            return _error_response(
                f"`{profile_key}` must be one of: " + ", ".join(settings.available_vlm_profiles)
            )
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
        }
    )


@api_blueprint.route("/v1/indexing/jobs", methods=["POST"])
def create_indexing_job():
    if not _request_is_local():
        return _local_only_error()

    payload = request.get_json(silent=True) or {}
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
            indexing_service = indexing_service.__class__(
                settings=settings,
                repository=repository,
                vision_client=current_app.extensions["vision_client"],
                embedding_service=current_app.extensions["embedding_service"],
                text_embedding_service=current_app.extensions["text_embedding_service"],
                geocoder=current_app.extensions["geocoder"],
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

    return jsonify(result.to_response(include_records=include_records))


@api_blueprint.route("/v1/retrieval/query", methods=["POST"])
def create_retrieval_query():
    payload = request.get_json(silent=True) or {}
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

    if db_path_override is not None and (
        not isinstance(db_path_override, str) or not db_path_override.strip()
    ):
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
                image_limit=min(6, len(result.data)),
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
            image_limit=min(6, len(retrieved_images)),
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


@api_blueprint.route("/v1/library/files/<path:relative_path>", methods=["GET"])
def get_library_file(relative_path: str):
    if not _request_is_local():
        return _local_only_error()

    if Path(relative_path).suffix.lower() not in SUPPORTED_LIBRARY_FILE_EXTENSIONS:
        abort(404)

    settings = current_app.config["SETTINGS"]
    root_path_override = request.args.get("root_path")
    library_root = (
        Path(root_path_override).expanduser().resolve()
        if isinstance(root_path_override, str) and root_path_override.strip()
        else settings.image_library_dir.resolve()
    )
    if not library_root.exists() or not library_root.is_dir():
        abort(404)
    file_path = (library_root / relative_path).resolve()

    try:
        file_path.relative_to(library_root)
    except ValueError:
        abort(404)

    if not file_path.exists() or not file_path.is_file():
        abort(404)

    return send_file(Path(file_path), conditional=True)
