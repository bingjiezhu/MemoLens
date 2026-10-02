"""Shared, side-effect-free contracts for the MemoLens Codex plugin."""

from __future__ import annotations

import base64
from copy import deepcopy
import json
import re
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.10.1"


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


def compact_atlas_asset(raw: Any, library_dir: Path | None) -> dict[str, Any]:
    """Preserve an already Core-verified Atlas observation without re-verifying it.

    The plugin presenter has no canonical-image cryptographic authority.  It
    only rejects a missing or internally contradictory envelope so the exact
    Core observation cannot be stripped between Memories and the user.
    """

    if not isinstance(raw, dict):
        raise MemoLensError(
            "MemoLens Atlas omitted a verified-current image observation.",
            code="canonical_image_observation_invalid",
        )
    asset_id = raw.get("id")
    observation = _atlas_current_observation(
        raw.get("canonical_image_observation"),
        expected_asset_id=asset_id,
    )
    if (
        raw.get("asset_id") != asset_id
        or raw.get("analysis_status") != "current"
        or raw.get("analysis_binding") != observation["analysis_binding"]
        or raw.get("projection") != observation["projection"]
    ):
        raise MemoLensError(
            "MemoLens Atlas returned contradictory current-image evidence.",
            code="canonical_image_observation_invalid",
        )
    asset = compact_asset(raw, library_dir)
    asset["asset_id"] = observation["asset_id"]
    asset["analysis_status"] = "current"
    asset["canonical_image_observation"] = observation
    return asset


def _atlas_current_observation(
    value: Any,
    *,
    expected_asset_id: Any,
) -> dict[str, Any]:
    invalid = MemoLensError(
        "MemoLens Atlas omitted a verified-current image observation.",
        code="canonical_image_observation_invalid",
    )
    if (
        not isinstance(expected_asset_id, str)
        or not expected_asset_id
        or len(expected_asset_id) > 200
        or not isinstance(value, dict)
        or set(value)
        != {
            "object",
            "schema_version",
            "status",
            "authority",
            "provenance_status",
            "asset_id",
            "analysis_binding",
            "source_binding_sha256",
            "projection",
            "stages",
            "reason_code",
        }
        or value.get("object") != "memolens.canonical_image_observation"
        or value.get("schema_version") != "1"
        or value.get("status") != "current"
        or value.get("authority") != "canonical_image_analysis"
        or value.get("provenance_status") != "verified_current"
        or value.get("asset_id") != expected_asset_id
        or not _is_sha256(value.get("source_binding_sha256"))
        or value.get("reason_code") is not None
    ):
        raise invalid
    binding = value.get("analysis_binding")
    projection = value.get("projection")
    if (
        not isinstance(binding, dict)
        or set(binding)
        != {"analysis_run_id", "revision", "content_sha256"}
        or not isinstance(binding.get("analysis_run_id"), str)
        or not 1 <= len(binding["analysis_run_id"]) <= 200
        or type(binding.get("revision")) is not int
        or binding["revision"] < 1
        or not _is_sha256(binding.get("content_sha256"))
        or not isinstance(projection, dict)
        or set(projection)
        != {
            "status",
            "generation_id",
            "processing_generation_id",
            "receipt_sha256",
            "row_sha256",
            "reason_code",
        }
        or projection.get("status") != "current"
        or not _bounded_text(projection.get("generation_id"))
        or not _bounded_text(projection.get("processing_generation_id"))
        or not _is_sha256(projection.get("receipt_sha256"))
        or not _is_sha256(projection.get("row_sha256"))
        or projection.get("reason_code") is not None
    ):
        raise invalid
    stages = _atlas_observation_stages(value.get("stages"), invalid=invalid)
    return {
        "object": "memolens.canonical_image_observation",
        "schema_version": "1",
        "status": "current",
        "authority": "canonical_image_analysis",
        "provenance_status": "verified_current",
        "asset_id": expected_asset_id,
        "analysis_binding": dict(binding),
        "source_binding_sha256": value["source_binding_sha256"],
        "projection": dict(projection),
        "stages": stages,
        "reason_code": None,
    }


def _atlas_observation_stages(
    value: Any,
    *,
    invalid: MemoLensError,
) -> dict[str, Any]:
    stage_names = {"metadata", "geocode", "vision", "embedding", "quality"}
    statuses = {
        "succeeded",
        "partial",
        "unsupported",
        "disabled",
        "failed",
        "unknown",
    }
    provenance_fields = {
        "producer_id",
        "producer_version",
        "model_id",
        "model_version",
        "rule_id",
        "rule_version",
    }
    if not isinstance(value, dict) or set(value) != stage_names:
        raise invalid
    for stage in value.values():
        if (
            not isinstance(stage, dict)
            or set(stage) != {"status", "provenance", "output", "reason_code"}
            or stage.get("status") not in statuses
            or not isinstance(stage.get("provenance"), dict)
            or set(stage["provenance"]) != provenance_fields
            or not _bounded_text(stage["provenance"].get("producer_id"))
            or not _bounded_text(stage["provenance"].get("producer_version"))
            or any(
                item is not None and not _bounded_text(item)
                for item in (
                    stage["provenance"].get("model_id"),
                    stage["provenance"].get("model_version"),
                    stage["provenance"].get("rule_id"),
                    stage["provenance"].get("rule_version"),
                )
            )
            or (stage.get("output") is not None and not isinstance(stage["output"], dict))
            or (
                stage.get("reason_code") is not None
                and not _bounded_text(stage["reason_code"])
            )
        ):
            raise invalid
    return deepcopy(value)


def _bounded_text(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 200


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


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
    blueprint_schema_available: bool,
) -> dict[str, bool]:
    database = database or {}
    media = bool(database.get("media_schema_available"))
    video = bool(database.get("video_search_available"))
    timelines = bool(database.get("timeline_read_available"))
    project_list = bool(database.get("project_list_available"))
    project_resume = bool(database.get("project_resume_available"))
    project_history = bool(database.get("project_history_available"))
    blueprint_read = bool(database.get("persisted_blueprint_read_available"))
    blueprint_ledger = bool(database.get("blueprint_operation_ledger_available"))
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
        "list_projects": project_list,
        "read_project_resume": project_resume,
        "read_project_history": project_history,
        "read_blueprint_shadow": bool(project_resume and blueprint_schema_available),
        "validate_blueprint_candidate": blueprint_schema_available,
        "read_persisted_blueprint": blueprint_read,
        "read_blueprint_history": blueprint_ledger,
        "read_blueprint_operation_ledger": blueprint_ledger,
        "complete_project_history": False,
        "request_agent_pairing_via_cli": True,
        "write_blueprint_via_paired_cli": True,
        "mcp_write_blueprint": False,
        "confirm_blueprint_decisions": False,
        "creator_context": creator_context,
        "list_inbox": inbox,
        "wiki_status": True,
        "list_wiki_pages": media,
        "search_wiki": bool(media and (legacy_search or video)),
        "read_wiki_page": media,
        "read_wiki_evidence": media,
        "write_wiki": False,
        "refine_wiki": False,
        "write_inbox_review": False,
        "write_creator_profile": False,
        "create_timeline": False,
        "write_project": False,
        # This key describes the safe-default/MCP gateway.  The separate CLI
        # pairing protocol is advertised explicitly above and cannot widen it.
        "write_blueprint": False,
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
