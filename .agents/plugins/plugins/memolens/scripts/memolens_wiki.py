"""Agent-neutral live Wiki projection over read-only MemoLens facts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from memolens_contracts import MemoLensError, safety_summary
from memolens_text_safety import contains_private_locator


WIKI_SCHEMA_VERSION = "1"
WIKI_ROOT_PAGE_ID = "memolens://library/current"
_MAX_REFERENCE_ITEMS = 50
_MAX_URI_LENGTH = 260
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_ABSOLUTE_PATH = re.compile(
    r"^(?:/|[A-Za-z]:[\\/]|file://|data:)", re.IGNORECASE
)
_SEMANTIC_KEYS = {
    "actions",
    "angle",
    "camera",
    "colors",
    "composition",
    "description",
    "emotion",
    "keywords",
    "lighting",
    "location",
    "mood",
    "motion",
    "movement",
    "objects",
    "quality",
    "scene",
    "setting",
    "shot_type",
    "style",
    "subjects",
    "summary",
    "tags",
    "text",
    "time_of_day",
    "topic",
    "visual_style",
    "weather",
}


@dataclass(frozen=True)
class WikiReference:
    """One validated MemoLens Wiki URI."""

    kind: str
    identifier: str


def asset_page_id(asset_id: Any) -> str:
    return f"memolens://asset/{_identifier_value(asset_id)}"


def span_page_id(segment_id: Any) -> str:
    return f"memolens://span/{_identifier_value(segment_id)}"


def asset_evidence_id(asset_id: Any) -> str:
    return f"memolens://evidence/asset/{_identifier_value(asset_id)}"


def span_evidence_id(segment_id: Any) -> str:
    return f"memolens://evidence/span/{_identifier_value(segment_id)}"


def _identifier_value(value: Any) -> str:
    identifier = str(value or "")
    if not _IDENTIFIER.fullmatch(identifier):
        raise MemoLensError("MemoLens Wiki identifier is invalid.", code="invalid_argument")
    return identifier


def parse_page_id(value: Any) -> WikiReference:
    raw = _validated_uri(value, field="page_id")
    if raw == WIKI_ROOT_PAGE_ID:
        return WikiReference("library", "current")
    parsed = urlsplit(raw)
    if parsed.netloc not in {"asset", "span"}:
        raise MemoLensError("page_id is not a supported MemoLens Wiki page.", code="invalid_argument")
    identifier = parsed.path.removeprefix("/")
    if "/" in identifier:
        raise MemoLensError("page_id is invalid.", code="invalid_argument")
    return WikiReference(parsed.netloc, _identifier_value(identifier))


def parse_evidence_id(value: Any) -> WikiReference:
    raw = _validated_uri(value, field="evidence_id")
    parsed = urlsplit(raw)
    if parsed.netloc != "evidence":
        raise MemoLensError(
            "evidence_id is not a supported MemoLens Wiki evidence reference.",
            code="invalid_argument",
        )
    parts = parsed.path.removeprefix("/").split("/")
    if len(parts) != 2 or parts[0] not in {"asset", "span"}:
        raise MemoLensError("evidence_id is invalid.", code="invalid_argument")
    return WikiReference(parts[0], _identifier_value(parts[1]))


def _validated_uri(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_URI_LENGTH:
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    if (
        value != value.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    try:
        parsed = urlsplit(value)
        has_credentials_or_port = bool(
            parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
        )
    except ValueError as exc:
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument") from exc
    if (
        parsed.scheme != "memolens"
        or has_credentials_or_port
        or parsed.query
        or parsed.fragment
    ):
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    return value


def _projection() -> dict[str, Any]:
    return {
        "mode": "live_read_model_v0",
        "generation": None,
        "generation_pinned": False,
        "snapshot_policy": "private_sqlite_snapshot_per_store_read",
        "cross_request_consistency": "not_pinned",
    }


def _gap(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _standard_gaps(*extra: dict[str, str]) -> list[dict[str, str]]:
    return [
        _gap(
            "materialized_generation_unavailable",
            "This live view is not a pinned or exportable Wiki generation.",
        ),
        _gap(
            "typed_relations_unavailable",
            "Event, theme, usage, project, and creative relations are not materialized.",
        ),
        _gap(
            "usage_intervals_unavailable",
            "Published and exported source-usage intervals are not available in this slice.",
        ),
        _gap(
            "project_generation_pinning_unavailable",
            "Projects cannot pin this live projection to a stable Wiki generation.",
        ),
        _gap(
            "scoped_refinement_unavailable",
            "Agents cannot request a bounded media re-analysis through this read surface.",
        ),
        _gap(
            "portable_wiki_bundle_unavailable",
            "A portable Wiki bundle cannot be exported from this live projection.",
        ),
        *extra,
    ]


def _base(object_name: str, *, status: str = "completed") -> dict[str, Any]:
    return {
        "object": object_name,
        "schema_version": WIKI_SCHEMA_VERSION,
        "status": status,
        "source": "sqlite_read_only",
        "mode": "safe_default_read_only",
        "projection": _projection(),
    }


def _content_sha256(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _safe_untrusted_text(value: Any) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    # Fail closed for locator-shaped text. Replacing the whole display field avoids
    # leaking the tail of a path containing spaces or an unusual token boundary.
    if contains_private_locator(normalized):
        return "[redacted-path]"
    return normalized[:4000]


def _finite_number(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_filename(value: Any) -> str | None:
    normalized = _safe_untrusted_text(value)
    if normalized is None:
        return None
    # A display filename is data, never a locator. Collapse hostile path-shaped values.
    return normalized.replace("\\", "/").rsplit("/", 1)[-1] or None


def _safe_image_observation(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    status = value.get("status")
    if (
        value.get("object") != "memolens.canonical_image_observation"
        or value.get("schema_version") != "1"
        or status not in {"current", "pending", "unavailable"}
        or value.get("authority") != "canonical_image_analysis"
    ):
        return None
    raw_binding = value.get("analysis_binding")
    binding = (
        {
            "analysis_run_id": raw_binding.get("analysis_run_id"),
            "revision": _finite_number(raw_binding.get("revision")),
            "content_sha256": raw_binding.get("content_sha256"),
        }
        if isinstance(raw_binding, dict)
        else None
    )
    raw_projection = value.get("projection")
    processing_generation_id = (
        raw_projection.get("processing_generation_id")
        if isinstance(raw_projection, dict)
        else None
    )
    if processing_generation_id is not None and (
        not isinstance(processing_generation_id, str)
        or not _IDENTIFIER.fullmatch(processing_generation_id)
    ):
        return None
    projection = (
        {
            "status": raw_projection.get("status"),
            "generation_id": raw_projection.get("generation_id"),
            "processing_generation_id": processing_generation_id,
            "receipt_sha256": raw_projection.get("receipt_sha256"),
            "row_sha256": raw_projection.get("row_sha256"),
            "reason_code": raw_projection.get("reason_code"),
        }
        if isinstance(raw_projection, dict)
        else None
    )
    raw_stages = value.get("stages")
    stages = (
        {
            stage_name: _safe_image_stage(stage_name, raw_stages.get(stage_name))
            for stage_name in ("metadata", "geocode", "vision", "embedding", "quality")
        }
        if status == "current" and isinstance(raw_stages, dict)
        else None
    )
    if isinstance(stages, dict) and any(
        stage is None for stage in stages.values()
    ):
        return None
    return {
        "object": "memolens.canonical_image_observation",
        "schema_version": "1",
        "status": status,
        "authority": "canonical_image_analysis",
        "provenance_status": value.get("provenance_status"),
        "asset_id": value.get("asset_id"),
        "analysis_binding": binding,
        "source_binding_sha256": value.get("source_binding_sha256"),
        "projection": projection,
        "stages": stages,
        "reason_code": value.get("reason_code"),
    }


def _safe_image_provenance(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "producer_id": _safe_untrusted_text(value.get("producer_id")),
        "producer_version": _safe_untrusted_text(value.get("producer_version")),
        "model_id": _safe_untrusted_text(value.get("model_id")),
        "model_version": _safe_untrusted_text(value.get("model_version")),
        "rule_id": _safe_untrusted_text(value.get("rule_id")),
        "rule_version": _safe_untrusted_text(value.get("rule_version")),
    }


def _safe_image_stage(stage_name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    raw_output = value.get("output")
    output: dict[str, Any] | None = None
    if isinstance(raw_output, dict):
        if stage_name == "metadata":
            output = {
                "mime_type": _safe_untrusted_text(raw_output.get("mime_type")),
                "file_size": _finite_number(raw_output.get("file_size")),
                "width": _finite_number(raw_output.get("width")),
                "height": _finite_number(raw_output.get("height")),
                "taken_at": _safe_untrusted_text(raw_output.get("taken_at")),
                "latitude": _finite_number(raw_output.get("latitude")),
                "longitude": _finite_number(raw_output.get("longitude")),
                "altitude": _finite_number(raw_output.get("altitude")),
            }
        elif stage_name == "geocode":
            output = {
                "place_name": _safe_untrusted_text(raw_output.get("place_name")),
                "country": _safe_untrusted_text(raw_output.get("country")),
            }
        elif stage_name == "vision":
            raw_tags = raw_output.get("tags")
            output = {
                "description": _safe_untrusted_text(raw_output.get("description")),
                "tags": (
                    [
                        item
                        for item in (
                            _safe_untrusted_text(tag) for tag in raw_tags[:50]
                        )
                        if item
                    ]
                    if isinstance(raw_tags, list)
                    else []
                ),
                "location_hint": _safe_untrusted_text(
                    raw_output.get("location_hint")
                ),
            }
        elif stage_name == "embedding":
            raw_vectors = raw_output.get("vectors")
            vectors = raw_vectors if isinstance(raw_vectors, list) else []
            output = {
                "combined_text_sha256": raw_output.get("combined_text_sha256"),
                "vectors": [
                    {
                        "purpose": vector.get("purpose"),
                        "signal": vector.get("signal"),
                        "model_id": _safe_untrusted_text(vector.get("model_id")),
                        "dimensions": _finite_number(vector.get("dimensions")),
                    }
                    for vector in vectors[:4]
                    if isinstance(vector, dict)
                ],
            }
        elif stage_name == "quality":
            output = {
                "aesthetic_score": _finite_number(
                    raw_output.get("aesthetic_score")
                ),
                "technical_quality_score": _finite_number(
                    raw_output.get("technical_quality_score")
                ),
            }
    return {
        "status": value.get("status"),
        "provenance": _safe_image_provenance(value.get("provenance")),
        "output": output,
        "reason_code": value.get("reason_code"),
    }


def _safe_semantic(value: Any, *, depth: int = 0) -> Any:
    """Keep bounded domain semantics while dropping provider and path payloads."""

    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized or _ABSOLUTE_PATH.match(normalized):
            return None
        return _safe_untrusted_text(normalized)
    if isinstance(value, list):
        items = [_safe_semantic(item, depth=depth + 1) for item in value[:50]]
        return [item for item in items if item is not None]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:100]:
            key = str(raw_key).casefold()
            if key not in _SEMANTIC_KEYS:
                continue
            sanitized = _safe_semantic(raw_value, depth=depth + 1)
            if sanitized is not None:
                result[key] = sanitized
        return result
    return None


def _review(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    return {
        "revision": int(raw.get("revision") or 0),
        "inbox_state": str(raw.get("inbox_state") or "inbox"),
        "favorite": bool(raw.get("favorite")),
        "project_ready": bool(raw.get("project_ready")),
    }


def _asset_facts(raw: dict[str, Any]) -> dict[str, Any]:
    dimensions = {
        "width": _finite_number(raw.get("width")),
        "height": _finite_number(raw.get("height")),
        "rotation_degrees": _finite_number(raw.get("rotation_degrees")),
    }
    return {
        "asset_id": raw.get("id") or raw.get("asset_id"),
        "asset_source_id": raw.get("asset_source_id"),
        "asset_sha256": raw.get("sha256") or raw.get("asset_sha256"),
        "media_kind": raw.get("kind") or raw.get("media_kind"),
        "mime_type": raw.get("mime_type"),
        "file_size": _finite_number(raw.get("file_size")),
        "duration_ms": _finite_number(raw.get("duration_ms")),
        "dimensions": dimensions,
        "captured_at": raw.get("captured_at") or raw.get("taken_at"),
        "probe_status": raw.get("probe_status"),
        "source_availability": raw.get("source_availability"),
        "review": _review(raw.get("review")),
    }


def _asset_title(raw: dict[str, Any]) -> str:
    return _safe_filename(raw.get("filename")) or str(
        raw.get("id") or "Untitled asset"
    )


def _asset_summary(raw: dict[str, Any]) -> str:
    kind = str(raw.get("kind") or "media").capitalize()
    duration = _finite_number(raw.get("duration_ms"))
    dimensions = ""
    width = _finite_number(raw.get("width"))
    height = _finite_number(raw.get("height"))
    if width and height:
        dimensions = f" · {width}×{height}"
    timing = (
        f" · {int(duration)} ms"
        if isinstance(duration, (int, float)) and not isinstance(duration, bool)
        else ""
    )
    return f"{kind}{timing}{dimensions}"


def _finalize_page(page: dict[str, Any]) -> dict[str, Any]:
    page["content_sha256"] = _content_sha256(page)
    return page


class LiveMediaWiki:
    """Pure presenter for the non-materialized ML-014-A0 read surface."""

    @staticmethod
    def status(database: dict[str, Any]) -> dict[str, Any]:
        available = bool(database.get("media_schema_available"))
        status = "completed" if available else "capability_unavailable"
        kind_counts = database.get("asset_kind_counts")
        if not isinstance(kind_counts, dict):
            kind_counts = {}
        gaps = _standard_gaps(
            _gap(
                "full_library_scan_watermark_unavailable",
                "MemoLens can report indexed assets, not unseen files outside the ledger.",
            ),
        )
        if not available:
            gaps.append(
                _gap(
                    "mixed_media_schema_unavailable",
                    "The installed index does not expose canonical media assets.",
                )
            )
        if database.get("wiki_read_error_code"):
            gaps.append(
                _gap(
                    "media_index_read_unavailable",
                    "The configured MemoLens media index could not be read.",
                )
            )
        if int(database.get("legacy_image_count") or 0):
            gaps.append(
                _gap(
                    "image_analysis_provenance_incomplete",
                    "Legacy image descriptions do not carry complete model and prompt provenance.",
                )
            )
        video_mode = str(database.get("video_schema_mode") or "")
        verified_video_heads = video_mode.startswith("analysis_heads")
        if verified_video_heads and int(
            database.get("readable_current_video_segment_count") or 0
        ):
            gaps.append(
                _gap(
                    "video_visual_semantics_unverified",
                    "Current video spans may contain local fallback text rather than full visual semantics.",
                )
            )
        if video_mode and not verified_video_heads:
            gaps.append(
                _gap(
                    "verified_successful_video_head_unavailable",
                    "Legacy revision selectors cannot prove that a video analysis head succeeded.",
                )
            )
        search_available = bool(
            available
            and (
                database.get("legacy_search_available")
                or verified_video_heads
            )
        )
        current_video_segment_count = (
            int(database.get("readable_current_video_segment_count") or 0)
            if verified_video_heads
            else 0
        )
        current_video_asset_count = (
            int(database.get("readable_current_video_asset_count") or 0)
            if verified_video_heads
            else 0
        )
        default_video_segment_count = (
            int(database.get("video_segment_count") or 0)
            if verified_video_heads
            else 0
        )
        default_video_asset_count = (
            int(database.get("current_video_asset_count") or 0)
            if verified_video_heads
            else 0
        )
        successful_video_head_asset_count = (
            int(database.get("successful_video_head_asset_count") or 0)
            if verified_video_heads
            else 0
        )
        indexed_video_asset_count = int(kind_counts.get("video") or 0)
        payload = {
            **_base("memolens.wiki_status", status=status),
            "capability_available": available,
            "root_page_id": WIKI_ROOT_PAGE_ID,
            "coverage": {
                "indexed_asset_count": int(database.get("asset_count") or 0),
                "asset_kind_counts": {
                    str(key): int(value) for key, value in kind_counts.items()
                },
                "legacy_described_image_count": int(
                    database.get("legacy_image_count") or 0
                ),
                "current_video_segment_count": current_video_segment_count,
                "default_discoverable_video_segment_count": default_video_segment_count,
                "indexed_video_asset_count": indexed_video_asset_count,
                "current_video_asset_count": current_video_asset_count,
                "default_discoverable_video_asset_count": default_video_asset_count,
                "video_assets_with_readable_current_spans": current_video_asset_count,
                "video_assets_without_readable_current_spans": max(
                    indexed_video_asset_count - current_video_asset_count,
                    0,
                ),
                "video_assets_with_verified_successful_head": successful_video_head_asset_count,
                "video_assets_without_verified_successful_head": max(
                    indexed_video_asset_count - successful_video_head_asset_count,
                    0,
                ),
                "video_assets_with_head_but_no_readable_spans": max(
                    successful_video_head_asset_count - current_video_asset_count,
                    0,
                ),
                "video_analysis_coverage_basis": (
                    "explicit_head_plus_successful_run"
                    if verified_video_heads
                    else "unavailable"
                ),
                "physical_library_file_count": None,
                "unseen_library_files": "unknown",
            },
            "capabilities": {
                "list_asset_pages": available,
                "search_pages": search_available,
                "open_asset_pages": available,
                "open_current_span_pages": bool(
                    available and verified_video_heads
                ),
                "read_asset_evidence": available,
                "read_current_span_evidence": bool(
                    available and verified_video_heads
                ),
                "write_wiki": False,
                "refine_media": False,
            },
            "gaps": gaps,
            "safety": safety_summary(),
        }
        return payload

    @staticmethod
    def unavailable_page_list(
        *, kinds: list[str], reason_code: str = "mixed_media_schema_unavailable"
    ) -> dict[str, Any]:
        return {
            **_base(
                "memolens.wiki_page_list", status="capability_unavailable"
            ),
            "capability_available": False,
            "kinds": kinds,
            "result_count": 0,
            "pages": [],
            "next_cursor": None,
            "gaps": _standard_gaps(
                _gap(
                    reason_code,
                    "The installed index does not expose canonical media assets.",
                )
            ),
            "safety": safety_summary(),
        }

    @staticmethod
    def list_pages(media_list: dict[str, Any]) -> dict[str, Any]:
        raw_assets = media_list.get("assets")
        assets = raw_assets if isinstance(raw_assets, list) else []
        pages = [
            LiveMediaWiki._asset_page_summary(raw)
            for raw in assets
            if isinstance(raw, dict)
        ]
        return {
            **_base("memolens.wiki_page_list"),
            "capability_available": True,
            "kinds": list(media_list.get("kinds") or []),
            "result_count": len(pages),
            "pages": pages,
            "next_cursor": media_list.get("next_cursor"),
            "gaps": _standard_gaps(
                _gap(
                    "live_cursor_not_generation_pinned",
                    "Pagination is keyset-based over a live projection and may change after ledger updates.",
                )
            ),
            "safety": safety_summary(),
        }

    @staticmethod
    def _asset_page_summary(raw: dict[str, Any]) -> dict[str, Any]:
        identifier = _identifier_value(raw.get("id"))
        page = {
            "page_id": asset_page_id(identifier),
            "page_type": "asset",
            "title": _asset_title(raw),
            "summary": _asset_summary(raw),
            "authority_summary": ["deterministic"],
            "status": "current_live_projection",
            "freshness": "live",
            "media_kind": raw.get("kind"),
            "evidence_refs": [asset_evidence_id(identifier)],
            "untrusted_data_fields": ["title"],
        }
        return _finalize_page(page)

    @staticmethod
    def search(mixed_search: dict[str, Any]) -> dict[str, Any]:
        raw_results = mixed_search.get("results")
        raw_items = raw_results if isinstance(raw_results, list) else []
        results: list[dict[str, Any]] = []
        excluded_unverified_video_heads = 0
        for raw in raw_items:
            if not isinstance(raw, dict):
                continue
            result_type = raw.get("result_type")
            if result_type == "video_segment":
                if not raw.get("analysis_run_id"):
                    excluded_unverified_video_heads += 1
                    continue
                identifier = _identifier_value(raw.get("segment_id") or raw.get("id"))
                page_id = span_page_id(identifier)
                evidence_id = span_evidence_id(identifier)
            elif result_type == "image":
                identifier = _identifier_value(raw.get("asset_id") or raw.get("id"))
                page_id = asset_page_id(identifier)
                evidence_id = asset_evidence_id(identifier)
            else:
                continue
            results.append(
                {
                    "page_id": page_id,
                    "evidence_id": evidence_id,
                    "result_type": result_type,
                    "media_kind": raw.get("media_kind"),
                    "asset_id": raw.get("asset_id"),
                    "segment_id": raw.get("segment_id"),
                    "title": _safe_filename(raw.get("filename")) or identifier,
                    "summary": _safe_untrusted_text(
                        raw.get("summary") or raw.get("description")
                    ),
                    "start_ms": _finite_number(raw.get("start_ms")),
                    "end_ms": _finite_number(raw.get("end_ms")),
                    "analysis_run_id": raw.get("analysis_run_id"),
                    "analysis_revision": _finite_number(raw.get("analysis_revision")),
                    "matched_terms": list(raw.get("matched_terms") or []),
                    "rank_score": _finite_number(raw.get("rank_score")),
                    "source_availability": raw.get("source_availability"),
                    "authority_summary": [
                        "deterministic",
                        "model_hypothesis",
                    ],
                    "untrusted_data_fields": ["title", "summary", "matched_terms"],
                }
            )
        extra_gaps: list[dict[str, str]] = []
        if excluded_unverified_video_heads:
            extra_gaps.append(
                _gap(
                    "unverified_successful_video_heads_excluded",
                    "Video matches without an explicit successful analysis head were excluded.",
                )
            )
        return {
            **_base("memolens.wiki_search"),
            "query": mixed_search.get("query"),
            "ranking": mixed_search.get("ranking"),
            "retrieval_claim": "existing_mixed_search_projection",
            "result_count": len(results),
            "results": results,
            "searched_result_types": list(
                mixed_search.get("searched_result_types") or []
            ),
            "branch_errors": list(mixed_search.get("branch_errors") or []),
            "gaps": _standard_gaps(
                _gap(
                    "semantic_ranking_not_added",
                    "This Wiki slice projects the existing retrieval result; it does not add a new semantic ranker.",
                ),
                *extra_gaps,
            ),
            "safety": safety_summary(),
        }

    @staticmethod
    def library_page(wiki_status: dict[str, Any]) -> dict[str, Any]:
        page = {
            "page_id": WIKI_ROOT_PAGE_ID,
            "page_type": "library",
            "title": "MemoLens Library",
            "summary": "Live map of media already present in the canonical ledger.",
            "authority_summary": ["deterministic"],
            "status": "current_live_projection",
            "freshness": "live",
            "content": {
                "coverage": wiki_status.get("coverage", {}),
                "capabilities": wiki_status.get("capabilities", {}),
                "gaps": wiki_status.get("gaps", []),
            },
            "links": {
                "items": [],
                "returned_count": 0,
                "known_count": None,
                "truncated": False,
                "discovery_operation": "wiki-list",
            },
            "evidence_refs": [],
            "untrusted_data_fields": [],
        }
        return LiveMediaWiki._page_response(_finalize_page(page))

    @staticmethod
    def asset_page(media_detail: dict[str, Any]) -> dict[str, Any]:
        raw_asset = media_detail.get("asset")
        if not isinstance(raw_asset, dict):
            raise MemoLensError(
                "Wiki asset page could not be projected.", code="database_identity_mismatch"
            )
        identifier = _identifier_value(raw_asset.get("id"))
        raw_segments = media_detail.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        links: list[dict[str, Any]] = []
        evidence_refs = [asset_evidence_id(identifier)]
        for raw in segments[:_MAX_REFERENCE_ITEMS]:
            if not isinstance(raw, dict):
                continue
            segment_id = _identifier_value(raw.get("id"))
            links.append(
                {
                    "relation": "contains",
                    "target_page_id": span_page_id(segment_id),
                    "target_page_type": "temporal_span",
                    "start_ms": raw.get("start_ms"),
                    "end_ms": raw.get("end_ms"),
                }
            )
            evidence_refs.append(span_evidence_id(segment_id))
        observation = _safe_image_observation(media_detail.get("image_observation"))
        observation_status = (
            observation.get("status") if isinstance(observation, dict) else None
        )
        has_observation = observation_status == "current"
        authorities = ["deterministic"] + (
            ["model_hypothesis"] if has_observation else []
        )
        page = {
            "page_id": asset_page_id(identifier),
            "page_type": "asset",
            "title": _asset_title(raw_asset),
            "summary": _asset_summary(raw_asset),
            "authority_summary": authorities,
            "status": "current_live_projection",
            "freshness": "live",
            "content": {
                "facts": _asset_facts(raw_asset),
                "observation": observation,
                "video_index_status": media_detail.get("video_index_status"),
            },
            "links": {
                "items": links,
                "returned_count": len(links),
                "known_count": len(segments) if len(segments) < 500 else None,
                "truncated": len(segments) > len(links) or len(segments) >= 500,
            },
            "evidence_refs": evidence_refs,
            "untrusted_data_fields": [
                "title",
                "content.observation",
            ],
        }
        page_gaps: list[dict[str, str]] = []
        if raw_asset.get("kind") == "video" and not segments:
            if (
                not raw_asset.get("asset_source_id")
                or raw_asset.get("source_availability") != "available"
            ):
                page_gaps.append(
                    _gap(
                        "video_spans_unreadable_without_active_source",
                        "Current analysis may exist, but no active source binding can expose its spans.",
                    )
                )
            elif media_detail.get("video_index_status") == (
                "verified_successful_head_unavailable"
            ):
                page_gaps.append(
                    _gap(
                        "verified_successful_video_head_unavailable",
                        "The installed index cannot prove a successful current video-analysis head.",
                    )
                )
            else:
                page_gaps.append(
                    _gap(
                        "video_current_analysis_unavailable",
                        "This video has no readable spans from a current successful analysis head.",
                    )
                )
        if raw_asset.get("kind") == "image":
            if observation_status == "pending":
                page_gaps.append(
                    _gap(
                        "canonical_image_projection_pending",
                        "The canonical image result is bound, but its verified projection is not current yet.",
                    )
                )
            elif observation_status != "current":
                page_gaps.append(
                    _gap(
                        "canonical_image_observation_unavailable",
                        "No exact current canonical image observation passed the projection proof chain.",
                    )
                )
        if (
            not raw_asset.get("asset_source_id")
            or raw_asset.get("source_availability") != "available"
            or raw_asset.get("library_root_status") not in {None, "active"}
        ):
            page_gaps.append(
                _gap(
                    "asset_source_unavailable",
                    "No currently available source binding is present for this asset.",
                )
            )
        return LiveMediaWiki._page_response(
            _finalize_page(page),
            extra_gaps=page_gaps,
        )

    @staticmethod
    def span_page(segment_detail: dict[str, Any]) -> dict[str, Any]:
        raw_segment = segment_detail.get("segment")
        raw_asset = segment_detail.get("asset")
        if not isinstance(raw_segment, dict) or not isinstance(raw_asset, dict):
            raise MemoLensError(
                "Wiki span page could not be projected.", code="database_identity_mismatch"
            )
        segment_id = _identifier_value(raw_segment.get("id"))
        asset_id = _identifier_value(raw_segment.get("asset_id"))
        page = {
            "page_id": span_page_id(segment_id),
            "page_type": "temporal_span",
            "title": _safe_untrusted_text(raw_segment.get("summary"))
            or f"Video span {segment_id}",
            "summary": (
                f"Current video evidence [{raw_segment.get('start_ms')},"
                f"{raw_segment.get('end_ms')}) ms"
            ),
            "authority_summary": ["deterministic", "model_hypothesis"],
            "status": "current_live_projection",
            "freshness": "live",
            "content": {
                "facts": LiveMediaWiki._span_facts(raw_asset, raw_segment),
                "observation": LiveMediaWiki._span_observation(raw_segment),
            },
            "links": {
                "items": [
                    {
                        "relation": "span_of",
                        "target_page_id": asset_page_id(asset_id),
                        "target_page_type": "asset",
                    }
                ],
                "returned_count": 1,
                "known_count": 1,
                "truncated": False,
            },
            "evidence_refs": [span_evidence_id(segment_id)],
            "untrusted_data_fields": [
                "title",
                "content.observation.summary",
                "content.observation.visible_text",
                "content.observation.semantic",
            ],
        }
        extra_gaps = []
        if (
            not raw_asset.get("asset_source_id")
            or raw_asset.get("source_availability") != "available"
            or raw_asset.get("library_root_status") not in {None, "active"}
        ):
            extra_gaps.append(
                _gap(
                    "asset_source_unavailable",
                    "The current span is known, but its selected source is not available.",
                )
            )
        return LiveMediaWiki._page_response(
            _finalize_page(page), extra_gaps=extra_gaps
        )

    @staticmethod
    def asset_evidence(media_detail: dict[str, Any]) -> dict[str, Any]:
        raw_asset = media_detail.get("asset")
        if not isinstance(raw_asset, dict):
            raise MemoLensError(
                "Wiki asset evidence could not be projected.",
                code="database_identity_mismatch",
            )
        identifier = _identifier_value(raw_asset.get("id"))
        gaps: list[dict[str, str]] = []
        if (
            not raw_asset.get("asset_source_id")
            or raw_asset.get("source_availability") != "available"
            or raw_asset.get("library_root_status") not in {None, "active"}
        ):
            gaps.append(
                _gap(
                    "asset_source_unavailable",
                    "The asset identity is known, but no currently available source binding is present.",
                )
            )
        return {
            **_base("memolens.wiki_evidence"),
            "evidence": {
                "evidence_id": asset_evidence_id(identifier),
                "page_id": asset_page_id(identifier),
                "evidence_kind": "asset_identity",
                "authority": "deterministic",
                "facts": _asset_facts(raw_asset),
                "untrusted_data": {
                    "filename": _safe_filename(raw_asset.get("filename")),
                },
                "untrusted_data_fields": ["untrusted_data.filename"],
            },
            "gaps": _standard_gaps(*gaps),
            "safety": safety_summary(),
        }

    @staticmethod
    def span_evidence(segment_detail: dict[str, Any]) -> dict[str, Any]:
        raw_segment = segment_detail.get("segment")
        raw_asset = segment_detail.get("asset")
        if not isinstance(raw_segment, dict) or not isinstance(raw_asset, dict):
            raise MemoLensError(
                "Wiki span evidence could not be projected.",
                code="database_identity_mismatch",
            )
        segment_id = _identifier_value(raw_segment.get("id"))
        gaps: list[dict[str, str]] = []
        if (
            not raw_asset.get("asset_source_id")
            or raw_asset.get("source_availability") != "available"
            or raw_asset.get("library_root_status") not in {None, "active"}
        ):
            gaps.append(
                _gap(
                    "asset_source_unavailable",
                    "The current span is known, but its selected source is not available.",
                )
            )
        return {
            **_base("memolens.wiki_evidence"),
            "evidence": {
                "evidence_id": span_evidence_id(segment_id),
                "page_id": span_page_id(segment_id),
                "evidence_kind": "current_video_span",
                "authority": "deterministic",
                "facts": LiveMediaWiki._span_facts(raw_asset, raw_segment),
                "observations": {
                    "authority": "model_hypothesis",
                    **LiveMediaWiki._span_observation(raw_segment),
                },
                "untrusted_data_fields": [
                    "observations.summary",
                    "observations.visible_text",
                    "observations.semantic",
                ],
            },
            "gaps": _standard_gaps(*gaps),
            "safety": safety_summary(),
        }

    @staticmethod
    def _span_facts(
        raw_asset: dict[str, Any], raw_segment: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "asset_id": raw_segment.get("asset_id") or raw_asset.get("id"),
            "asset_source_id": raw_segment.get("asset_source_id")
            or raw_asset.get("asset_source_id"),
            "asset_sha256": raw_asset.get("sha256")
            or raw_segment.get("asset_sha256"),
            "source_availability": raw_asset.get("source_availability"),
            "library_root_status": raw_asset.get("library_root_status"),
            "segment_id": raw_segment.get("id"),
            "time_range": {
                "start_ms": _finite_number(raw_segment.get("start_ms")),
                "end_ms": _finite_number(raw_segment.get("end_ms")),
                "interval_semantics": "half_open",
            },
            "analysis_run_id": raw_segment.get("analysis_run_id"),
            "analysis_revision": _finite_number(raw_segment.get("analysis_revision")),
            "current_successful_head": True,
        }

    @staticmethod
    def _span_observation(raw_segment: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": _safe_untrusted_text(raw_segment.get("summary")),
            "visible_text": _safe_untrusted_text(raw_segment.get("visible_text")),
            "semantic": _safe_semantic(raw_segment.get("semantic")),
            "visual_status": raw_segment.get("visual_status"),
            "transcript_status": raw_segment.get("transcript_status"),
            "confidence": _finite_number(raw_segment.get("confidence")),
        }

    @staticmethod
    def _page_response(
        page: dict[str, Any],
        *,
        extra_gaps: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            **_base("memolens.wiki_page"),
            "page": page,
            "gaps": _standard_gaps(*(extra_gaps or [])),
            "safety": safety_summary(),
        }


__all__ = [
    "LiveMediaWiki",
    "WIKI_ROOT_PAGE_ID",
    "asset_evidence_id",
    "asset_page_id",
    "parse_evidence_id",
    "parse_page_id",
    "span_evidence_id",
    "span_page_id",
]
