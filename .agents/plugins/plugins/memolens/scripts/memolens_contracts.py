"""Shared, side-effect-free contracts for the MemoLens Codex plugin."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.5.0"


class MemoLensError(RuntimeError):
    """Expected, user-actionable plugin failure."""

    def __init__(self, message: str, *, code: str = "memolens_error") -> None:
        super().__init__(message)
        self.code = code


def safe_absolute_path(
    relative_path: Any, library_dir: Path | None
) -> dict[str, Any]:
    """Resolve a library-relative path without allowing traversal."""

    if not isinstance(relative_path, str) or not relative_path.strip():
        return {"path_status": "missing_relative_path"}
    if library_dir is None:
        return {"path_status": "library_root_unavailable"}
    root = library_dir.expanduser().resolve()
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return {"path_status": "rejected_outside_library"}
    return {
        "absolute_path": str(candidate),
        "path_status": "ok" if candidate.is_file() else "missing_file",
    }


def parse_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(item) for item in raw_tags if str(item).strip()]
    if not isinstance(raw_tags, str):
        return []
    try:
        parsed = json.loads(raw_tags)
    except json.JSONDecodeError:
        return [item.strip() for item in raw_tags.split(",") if item.strip()]
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def compact_asset(raw: Any, library_dir: Path | None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    asset = {
        key: raw.get(key)
        for key in (
            "id",
            "object",
            "filename",
            "relative_path",
            "taken_at",
            "place_name",
            "country",
            "description",
            "score",
            "quality_score",
            "technical_quality_score",
            "matched_terms",
            "asset_id",
            "asset_source_id",
            "asset_sha256",
            "source_availability",
            "review_revision",
            "inbox_state",
            "favorite",
            "project_ready",
        )
        if raw.get(key) is not None
    }
    asset["tags"] = parse_tags(raw.get("tags", raw.get("tags_json")))
    if asset.get("asset_id") and asset.get("asset_source_id") and asset.get(
        "asset_sha256"
    ):
        asset["provenance"] = {
            "asset_id": asset["asset_id"],
            "asset_source_id": asset["asset_source_id"],
            "asset_sha256": asset["asset_sha256"],
            "source_availability": asset.get("source_availability"),
        }
    if asset.get("review_revision") is not None:
        asset["review"] = {
            "revision": int(asset.pop("review_revision")),
            "inbox_state": asset.pop("inbox_state", "inbox"),
            "favorite": bool(asset.pop("favorite", False)),
            "project_ready": bool(asset.pop("project_ready", False)),
        }
    asset.update(safe_absolute_path(raw.get("relative_path"), library_dir))
    return asset


def parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def compact_media_asset(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    asset = {
        key: raw.get(key)
        for key in (
            "id",
            "kind",
            "sha256",
            "asset_source_id",
            "filename",
            "relative_path",
            "source_availability",
            "mime_type",
            "file_size",
            "duration_ms",
            "width",
            "height",
            "rotation_degrees",
            "captured_at",
            "probe_status",
            "analysis_run_id",
            "analysis_revision",
            "error_code",
            "review_revision",
            "inbox_state",
            "favorite",
            "project_ready",
        )
        if raw.get(key) is not None
    }
    asset["object"] = "memolens.media_asset"
    asset["schema_version"] = "1"
    asset["codec"] = parse_json_object(raw.get("codec_json"))
    asset["review"] = {
        "revision": int(asset.pop("review_revision", 0) or 0),
        "inbox_state": asset.pop("inbox_state", "inbox"),
        "favorite": bool(asset.pop("favorite", False)),
        "project_ready": bool(asset.pop("project_ready", False)),
    }
    return asset


def encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(value: Any, *, field: str = "cursor") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument") from exc
    if not decoded or len(decoded) > 300:
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    return decoded


def query_terms(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "find",
        "for",
        "from",
        "in",
        "me",
        "my",
        "of",
        "photo",
        "photos",
        "picture",
        "pictures",
        "show",
        "the",
        "to",
        "with",
    }
    tokens = re.findall(r"[\w\u3400-\u9fff]+", query.casefold(), flags=re.UNICODE)
    return list(dict.fromkeys(token for token in tokens if token not in stopwords))


def media_kinds(value: Any) -> list[str]:
    if value is None:
        return ["image", "video", "audio"]
    if not isinstance(value, list) or not value:
        raise MemoLensError(
            "kinds must be a non-empty array.", code="invalid_argument"
        )
    kinds: list[str] = []
    for item in value:
        if item not in {"image", "video", "audio"}:
            raise MemoLensError(
                "kinds may contain only image, video, or audio.",
                code="invalid_argument",
            )
        if item not in kinds:
            kinds.append(item)
    return kinds


def quality_value(*values: Any) -> float:
    numeric: list[float] = []
    for value in values:
        try:
            if value is not None:
                numeric.append(max(0.0, min(float(value), 1.0)))
        except (TypeError, ValueError):
            continue
    return sum(numeric) / len(numeric) if numeric else 0.0


def bounded_int(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool):
        raise MemoLensError(f"{field} must be an integer.", code="invalid_argument")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoLensError(
            f"{field} must be an integer.", code="invalid_argument"
        ) from exc
    if parsed < minimum or parsed > maximum:
        raise MemoLensError(
            f"{field} must be between {minimum} and {maximum}.",
            code="invalid_argument",
        )
    return parsed


def safety_summary() -> dict[str, Any]:
    return {
        "read_only": True,
        "photos_opened_by_plugin": False,
        "photos_modified": False,
        "media_modified": False,
        "timeline_persisted": False,
        "rendered": False,
        "exported": False,
        "remote_network_allowed": False,
    }


def timeline_safety_summary() -> dict[str, Any]:
    return {
        **safety_summary(),
        "in_memory_only": True,
        "requires_desktop_confirmation_to_persist": True,
    }


def capabilities(
    database: dict[str, Any] | None,
    *,
    legacy_search: bool,
    local_api_reads: bool,
) -> dict[str, bool]:
    database = database or {}
    media = bool(database.get("media_schema_available"))
    video = bool(database.get("video_search_available"))
    timelines = bool(database.get("timeline_read_available"))
    creator_context = bool(database.get("creator_context_available"))
    inbox = bool(database.get("inbox_available"))
    return {
        "status": True,
        "search": legacy_search,
        "search_assets": media,
        "video_search": video,
        "list_media": media,
        "get_media": media,
        "memories": local_api_reads,
        "cleanup": local_api_reads,
        "draft_timeline": True,
        "revise_timeline_draft": True,
        "validate_timeline": True,
        "read_timeline": timelines,
        "creator_context": creator_context,
        "list_inbox": inbox,
        "write_inbox_review": False,
        "write_creator_profile": False,
        "create_timeline": False,
        "save_timeline": False,
        "render_preview": False,
        "export_video": False,
    }


def json_ready(value: Any) -> Any:
    """Convert defensive edge types before emitting JSON."""

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    return value
