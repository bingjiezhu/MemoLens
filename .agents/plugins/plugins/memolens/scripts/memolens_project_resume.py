"""Strict Agent-facing projections for creative project Resume facts."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from memolens_contracts import MemoLensError, safety_summary
from memolens_project_store import validate_project_identifier
from memolens_text_safety import contains_private_locator
from memolens_wiki import asset_evidence_id, span_evidence_id


PROJECT_RESUME_SCHEMA_VERSION = "1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROJECT_STATUSES = {"draft", "active", "archived"}
_OPERATION_TYPES = {
    "add_transition",
    "draft",
    "move_clip",
    "relink_source",
    "remove_clip",
    "remove_transition",
    "replace_clip",
    "set_crop",
    "set_duration",
    "set_fit",
    "set_format",
    "set_transitions",
    "set_volume",
    "trim_clip",
}
_SUBJECT_FIELDS = (
    ("clip_id", "clip"),
    ("transition_id", "transition"),
    ("asset_id", "asset"),
)
_MAX_EVIDENCE = 200
_STANDARD_GAPS = (
    (
        "creative_blueprint_unavailable",
        "The persisted legacy brief is not a versioned Creative Blueprint.",
    ),
    (
        "explicit_project_timeline_head_unavailable",
        "The current schema has no authoritative project-level Timeline head.",
    ),
    (
        "project_write_unavailable",
        "This Agent surface cannot create or modify creative projects.",
    ),
    (
        "complete_operation_ledger_unavailable",
        "Persisted provenance is not a complete replayable operation ledger.",
    ),
    (
        "wiki_generation_unpinned",
        "Evidence references resolve against the current live Wiki view, not a pinned generation.",
    ),
    (
        "current_wiki_resolution_not_verified",
        "Historical evidence identifiers must be resolved with wiki-evidence before current source availability is assumed.",
    ),
)


def _remove_gap(gaps: list[dict[str, str]], code: str) -> None:
    gaps[:] = [item for item in gaps if item.get("code") != code]


def _persisted_blueprint_head(
    raw: Any, gaps: list[dict[str, str]]
) -> dict[str, Any] | None:
    """Project an already fail-closed persisted-Blueprint read envelope."""

    if not isinstance(raw, dict) or raw.get("status") != "completed":
        return None
    blueprint = raw.get("blueprint")
    operation = raw.get("operation")
    selection = raw.get("selection")
    if not (
        isinstance(blueprint, dict)
        and isinstance(operation, dict)
        and isinstance(selection, dict)
        and selection.get("kind") == "current"
        and selection.get("is_current") is True
    ):
        raise MemoLensError(
            "Persisted Creative Blueprint projection is inconsistent.",
            code="blueprint_integrity_failed",
        )
    semantic = blueprint.get("semantic")
    open_decisions = semantic.get("open_decisions") if isinstance(semantic, dict) else None
    creation_authority = blueprint.get("creation_authority")
    authority_projection = blueprint.get("authority_projection")
    authority_summary = blueprint.get("authority_summary")
    if not (
        isinstance(open_decisions, list)
        and creation_authority == {"state": "unverified", "verified": False}
        and isinstance(authority_projection, dict)
        and authority_projection.get("state")
        in {"unverified", "partially_confirmed", "confirmed"}
        and type(authority_projection.get("confirmed_decision_unit_count")) is int
        and authority_projection.get("decision_unit_count") == 8
        and isinstance(authority_projection.get("decision_units"), dict)
        and len(authority_projection["decision_units"]) == 8
        and isinstance(authority_summary, dict)
    ):
        raise MemoLensError(
            "Persisted Creative Blueprint projection is inconsistent.",
            code="blueprint_integrity_failed",
        )
    _remove_gap(gaps, "creative_blueprint_unavailable")
    confirmed = authority_projection["confirmed_decision_unit_count"]
    if confirmed == 0:
        _add_gap(
            gaps,
            "blueprint_user_authority_unverified",
            "The canonical persisted Blueprint is an Agent proposal; no decision unit has verified user authority.",
        )
    elif confirmed < 8:
        _remove_gap(gaps, "blueprint_user_authority_unverified")
        _add_gap(
            gaps,
            "blueprint_user_authority_partial",
            f"The user has confirmed {confirmed} of 8 Blueprint decision units.",
        )
    else:
        _remove_gap(gaps, "blueprint_user_authority_unverified")
    _add_gap(
        gaps,
        "blueprint_operation_ledger_scope_only",
        "The operation ledger covers Creative Blueprint changes only, not the complete project history.",
    )
    _add_gap(
        gaps,
        "blueprint_timeline_compiler_unavailable",
        "The current Blueprint cannot yet be compiled into a Blueprint-bound Timeline in this slice.",
    )
    if open_decisions:
        _add_gap(
            gaps,
            "blueprint_open_decisions_unresolved",
            f"The Blueprint still has {len(open_decisions)} explicit open decision(s); unit confirmation does not resolve them.",
        )
    _add_gap(
        gaps,
        "blueprint_publish_authority_unavailable",
        "Decision-unit confirmation is not approval to render, export, publish, move, upload, or delete files.",
    )
    return {
        "canonical_persisted_head": True,
        "selection_policy": "explicit_blueprint_head_exact_revision_digest_operation_join",
        "revision": blueprint.get("revision"),
        "parent": blueprint.get("parent"),
        "status": blueprint.get("status"),
        "content_sha256": blueprint.get("content_sha256"),
        "semantic_sha256": blueprint.get("semantic_sha256"),
        "created_at": blueprint.get("created_at"),
        "creation_authority": creation_authority,
        "authority_projection": authority_projection,
        "authority": authority_summary,
        "outline": blueprint.get("outline"),
        "open_decisions": open_decisions,
        "data_trust": "untrusted_data",
        "untrusted_data_fields": ["open_decisions[].question"],
        "operation": {
            "operation_id": operation.get("operation_id"),
            "sequence": operation.get("sequence"),
            "command_type": operation.get("command_type"),
            "result_kind": operation.get("result_kind"),
            "created_at": operation.get("created_at"),
        },
    }


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number is not allowed: {value}")


def _gap(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _add_gap(gaps: list[dict[str, str]], code: str, message: str) -> None:
    if any(item["code"] == code for item in gaps):
        return
    gaps.append(_gap(code, message))


def _gaps() -> list[dict[str, str]]:
    return [_gap(code, message) for code, message in _STANDARD_GAPS]


def _safe_text(value: Any, *, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _CONTROL.sub(" ", value).strip()
    if not normalized:
        return None
    # Do not attempt partial replacement: locators containing spaces or embedded
    # next to other text otherwise leave a sensitive suffix behind.
    if contains_private_locator(normalized):
        return "[redacted-path]"
    return normalized[:limit]


def _identifier(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _positive_int(value: Any, *, maximum: int = 2_147_483_647) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= maximum else None


def _nonnegative_int(value: Any, *, maximum: int = 2_147_483_647) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= maximum else None


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _safe_timestamp(value: Any) -> str | None:
    # A timestamp remains display data.  Bound it without interpreting it as a locator.
    return _safe_text(value, limit=80)


def _integrity(
    raw_json: Any,
    stored_sha256: Any,
    *,
    expected_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    stored = (
        stored_sha256.casefold()
        if isinstance(stored_sha256, str) and _SHA256.fullmatch(stored_sha256)
        else None
    )
    computed: str | None = None
    parsed: dict[str, Any] | None = None
    if isinstance(raw_json, str):
        computed = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        try:
            candidate = json.loads(raw_json, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, RecursionError, ValueError):
            candidate = None
        if isinstance(candidate, dict):
            parsed = candidate
    digest_matches = bool(stored is not None and computed == stored)
    json_object = parsed is not None
    identity_matches = True
    if parsed is not None and expected_identity:
        for key, expected in expected_identity.items():
            observed = parsed.get(key)
            if isinstance(expected, int):
                matches = type(observed) is int and observed == expected
            elif isinstance(expected, str):
                matches = isinstance(observed, str) and observed == expected
            else:
                matches = observed == expected
            if not matches:
                identity_matches = False
                break
    verified = bool(digest_matches and json_object and identity_matches)
    if stored is None:
        status = "invalid_stored_digest"
    elif not digest_matches:
        status = "digest_mismatch"
    elif not json_object:
        status = "invalid_json"
    elif not identity_matches:
        status = "identity_mismatch"
    else:
        status = "verified"
    result = {
        "status": status,
        "stored_content_sha256": stored,
        "computed_content_sha256": computed,
        "digest_matches": digest_matches,
        "json_object": json_object,
        "identity_matches": identity_matches,
        "verified": verified,
    }
    return result, parsed if verified else None


def _integrity_gaps(
    integrity: dict[str, Any], gaps: list[dict[str, str]], *, prefix: str
) -> None:
    status = integrity["status"]
    if status == "invalid_stored_digest":
        _add_gap(
            gaps,
            f"{prefix}_stored_digest_invalid",
            f"The latest observed {prefix} has no valid stored SHA-256 digest.",
        )
    elif status == "digest_mismatch":
        _add_gap(
            gaps,
            f"{prefix}_digest_mismatch",
            f"The latest observed {prefix} content does not match its stored digest.",
        )
    elif status == "invalid_json":
        _add_gap(
            gaps,
            f"{prefix}_json_invalid",
            f"The latest observed {prefix} content is not a JSON object.",
        )
    elif status == "identity_mismatch":
        _add_gap(
            gaps,
            f"{prefix}_identity_mismatch",
            f"The latest observed {prefix} JSON identity conflicts with its row identity.",
        )
    if not integrity["json_object"] and status != "invalid_json":
        _add_gap(
            gaps,
            f"{prefix}_json_invalid",
            f"The latest observed {prefix} content is not a JSON object.",
        )


def _project_identity(raw: Any, gaps: list[dict[str, str]]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    project_id = _identifier(raw.get("project_id"))
    if project_id is None:
        _add_gap(
            gaps,
            "project_identity_invalid",
            "A persisted project has an invalid opaque identifier and was omitted.",
        )
        return None
    status = raw.get("status") if raw.get("status") in _PROJECT_STATUSES else "unknown"
    if status == "unknown":
        _add_gap(
            gaps,
            "project_status_invalid",
            "The persisted project status is outside the supported enum.",
        )
    return {
        "project_id": project_id,
        "title": _safe_text(raw.get("title"), limit=300),
        "title_trust": "untrusted_data",
        "untrusted_data_fields": ["title"],
        "status": status,
        "created_at": _safe_timestamp(raw.get("created_at")),
        "updated_at": _safe_timestamp(raw.get("updated_at")),
    }


def _safe_string_list(
    value: Any, *, limit: int, item_limit: int
) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value[:limit]:
        safe = _safe_text(item, limit=item_limit)
        if safe is not None and safe not in result:
            result.append(safe)
    return result


def _missing_asset_list(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    result: list[str] = []
    for item in value[:50]:
        candidate: Any = item
        if isinstance(item, dict):
            candidate = item.get("description") or item.get("why_needed")
        safe = _safe_text(candidate, limit=1_000)
        if safe is not None and safe not in result:
            result.append(safe)
    return result


def _brief_fields(brief: dict[str, Any]) -> dict[str, Any]:
    """Project only the documented legacy brief vocabulary."""

    result: dict[str, Any] = {}
    schema_version = _safe_text(brief.get("schema_version"), limit=40)
    if schema_version is not None:
        result["schema_version"] = schema_version
    for key, limit in (
        ("goal", 4_000),
        ("aspect_ratio", 40),
        ("audience", 1_000),
        ("platform", 200),
        ("tone", 1_000),
        ("pace", 1_000),
        ("narrative_arc", 4_000),
    ):
        safe = _safe_text(brief.get(key), limit=limit)
        if safe is not None:
            result[key] = safe
    duration = _positive_int(brief.get("duration_ms"), maximum=86_400_000)
    if duration is not None:
        result["duration_ms"] = duration
    for key in ("must_include", "must_exclude"):
        safe_list = _safe_string_list(brief.get(key), limit=32, item_limit=500)
        if safe_list is not None:
            result[key] = safe_list
    missing_assets = _missing_asset_list(brief.get("missing_assets"))
    if missing_assets is not None:
        result["missing_assets"] = missing_assets
    assumptions = _safe_string_list(
        brief.get("assumptions"), limit=50, item_limit=1_000
    )
    if assumptions is not None:
        result["assumptions"] = assumptions
    profile = brief.get("creator_profile_ref")
    if isinstance(profile, dict):
        profile_id = _identifier(profile.get("profile_id"))
        revision = _positive_int(profile.get("revision"), maximum=1_000_000)
        digest = (
            str(profile["content_sha256"]).casefold()
            if isinstance(profile.get("content_sha256"), str)
            and _SHA256.fullmatch(str(profile["content_sha256"]))
            else None
        )
        if profile_id is not None and revision is not None and digest is not None:
            result["creator_profile_ref"] = {
                "profile_id": profile_id,
                "revision": revision,
                "content_sha256": digest,
            }
    applied_fields = _safe_string_list(
        brief.get("applied_profile_fields"), limit=32, item_limit=100
    )
    if applied_fields is not None:
        result["applied_profile_fields"] = applied_fields
    return result


def _brief_projection(
    raw: Any, gaps: list[dict[str, str]]
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        _add_gap(
            gaps,
            "legacy_brief_missing",
            "This project has no persisted legacy creative brief revision.",
        )
        return None, None
    integrity, parsed = _integrity(raw.get("brief_json"), raw.get("content_sha256"))
    _integrity_gaps(integrity, gaps, prefix="brief")
    revision = _positive_int(raw.get("revision"), maximum=1_000_000)
    if revision is None:
        _add_gap(
            gaps,
            "brief_revision_invalid",
            "The latest legacy brief revision number is invalid.",
        )
        return None, None
    fields = _brief_fields(parsed) if parsed is not None else {}
    untrusted_fields = [
        f"fields.{key}"
        for key in fields
        if key not in {"duration_ms", "creator_profile_ref"}
    ]
    projection = {
        "kind": "legacy_brief_projection",
        "is_creative_blueprint": False,
        "revision": revision,
        "content_sha256": integrity["stored_content_sha256"],
        "created_at": _safe_timestamp(raw.get("created_at")),
        "integrity": integrity,
        "data_trust": "untrusted_data",
        "untrusted_data_fields": untrusted_fields,
        "fields": fields,
    }
    return projection, parsed


def _safe_format(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key in ("width", "height", "sample_rate"):
        numeric = _positive_int(value.get(key))
        if numeric is not None:
            result[key] = numeric
    fps = _finite_number(value.get("fps"))
    if fps is not None and fps > 0:
        result["fps"] = fps
    duration = _nonnegative_int(value.get("duration_ms"))
    if duration is not None:
        result["duration_ms"] = duration
    background = value.get("background_color")
    if isinstance(background, str):
        normalized_color = background.strip()
        if _HEX_COLOR.fullmatch(normalized_color):
            result["background_color"] = normalized_color.casefold()
    return result


def _content_counts(timeline: dict[str, Any]) -> tuple[int | None, int | None]:
    tracks = timeline.get("tracks")
    if not isinstance(tracks, list) or len(tracks) > 1_000:
        return None, None
    clip_count = 0
    for track in tracks:
        if not isinstance(track, dict):
            return None, None
        clips = track.get("clips")
        if not isinstance(clips, list) or len(clips) > 10_000:
            return None, None
        clip_count += len(clips)
        if clip_count > 100_000:
            return None, None
    return len(tracks), clip_count


def _brief_binding(
    raw: dict[str, Any], latest_brief_revision: int | None
) -> dict[str, Any]:
    revision = _positive_int(raw.get("brief_revision"), maximum=1_000_000)
    exists = raw.get("brief_binding_exists")
    if not isinstance(exists, bool):
        exists = None
    return {
        "revision": revision,
        "exists": exists,
        "latest_brief_revision": latest_brief_revision,
        "is_latest": (
            revision == latest_brief_revision
            if revision is not None and latest_brief_revision is not None
            else None
        ),
    }


def _timeline_projection(
    raw: Any,
    gaps: list[dict[str, str]],
    *,
    latest_brief_revision: int | None,
    kind: str,
    selection_policy: str,
    require_latest_brief_binding: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not isinstance(raw, dict):
        return None, None
    timeline_id = _identifier(raw.get("timeline_id"))
    project_id = _identifier(raw.get("project_id"))
    revision = _positive_int(raw.get("revision"), maximum=1_000_000)
    if timeline_id is None or project_id is None or revision is None:
        _add_gap(
            gaps,
            "timeline_row_identity_invalid",
            "A persisted Timeline revision row has an invalid identity.",
        )
        return None, None
    expected = (
        {"id": timeline_id, "project_id": project_id, "revision": revision}
        if timeline_id is not None and project_id is not None and revision is not None
        else None
    )
    integrity, parsed = _integrity(
        raw.get("timeline_json"),
        raw.get("content_sha256"),
        expected_identity=expected,
    )
    if expected is None:
        integrity["identity_matches"] = False
        integrity["verified"] = False
        integrity["status"] = "identity_mismatch"
        parsed = None
    _integrity_gaps(integrity, gaps, prefix="timeline")
    binding = _brief_binding(raw, latest_brief_revision)
    if not bool(raw.get("brief_revision_available")):
        _add_gap(
            gaps,
            "timeline_brief_binding_column_unavailable",
            "This Timeline compatibility relation has no brief revision column.",
        )
    if binding["revision"] is None:
        _add_gap(
            gaps,
            "timeline_brief_binding_unavailable",
            "This Timeline relation does not expose a valid brief revision binding.",
        )
    elif binding["exists"] is False:
        _add_gap(
            gaps,
            "timeline_brief_binding_missing",
            "The latest observed Timeline references a missing brief revision.",
        )
    elif binding["exists"] is None:
        _add_gap(
            gaps,
            "timeline_brief_binding_unverified",
            "The Timeline brief revision binding could not be verified.",
        )
    if binding["is_latest"] is False:
        _add_gap(
            gaps,
            "timeline_brief_binding_not_latest",
            "The latest observed Timeline is bound to an older brief revision and is not aligned with the latest legacy brief.",
        )
    parent_revision = raw.get("parent_revision")
    if parent_revision is not None:
        parent_revision = _positive_int(parent_revision, maximum=1_000_000)
    elif revision is not None and revision > 1:
        _add_gap(
            gaps,
            "timeline_parent_binding_unavailable",
            "This Timeline revision does not expose its parent revision binding.",
        )
    if not bool(raw.get("parent_revision_available")):
        _add_gap(
            gaps,
            "timeline_parent_binding_column_unavailable",
            "This Timeline compatibility relation has no parent revision column.",
        )
    if not bool(raw.get("validation_errors_json_available")):
        _add_gap(
            gaps,
            "timeline_validation_details_unavailable",
            "This Timeline compatibility relation has no validation error details column.",
        )
    validation = (
        raw.get("validation_status")
        if raw.get("validation_status") in {"valid", "invalid"}
        else "unknown"
    )
    if validation == "unknown":
        _add_gap(
            gaps,
            "timeline_validation_status_invalid",
            "The persisted Timeline validation status is outside the supported enum.",
        )
    elif validation == "invalid":
        _add_gap(
            gaps,
            "timeline_validation_failed",
            "The persisted Timeline revision is marked invalid and was not replaced by an older revision.",
        )
    derivation_allowed = bool(
        integrity["verified"]
        and validation == "valid"
        and binding["exists"] is True
        and (
            not require_latest_brief_binding
            or binding["is_latest"] is True
        )
    )
    trusted_timeline = parsed if derivation_allowed else None
    if not derivation_allowed:
        _add_gap(
            gaps,
            "timeline_derived_content_withheld",
            "Timeline format, counts, clip evidence, and operations were withheld because integrity, validation, or brief binding was not fully trusted for this view.",
        )
    track_count: int | None = None
    clip_count: int | None = None
    timeline_format: dict[str, Any] | None = None
    if trusted_timeline is not None:
        timeline_format = _safe_format(trusted_timeline.get("format"))
        track_count, clip_count = _content_counts(trusted_timeline)
        if track_count is None:
            _add_gap(
                gaps,
                "timeline_structure_invalid",
                "The trusted Timeline object does not have a bounded track/clip structure.",
            )
    projection = {
        "kind": kind,
        "authoritative_project_head": False,
        "selection_policy": selection_policy,
        "timeline_id": timeline_id,
        "project_id": project_id,
        "revision": revision,
        "parent_revision": parent_revision,
        "brief_revision": binding["revision"],
        "schema_version": _safe_text(raw.get("schema_version"), limit=40),
        "content_sha256": integrity["stored_content_sha256"],
        "validation_status": validation,
        "created_at": _safe_timestamp(raw.get("created_at")),
        "integrity": integrity,
        "format": timeline_format,
        "track_count": track_count,
        "clip_count": clip_count,
        "brief_binding": binding,
    }
    return projection, trusted_timeline


class _EvidenceCollector:
    def __init__(self, gaps: list[dict[str, str]]) -> None:
        self.gaps = gaps
        self._items: dict[tuple[str, str], set[str]] = {}
        self._invalid = 0
        self._truncated = False

    def add(self, kind: str, value: Any, source: str) -> None:
        identifier = _identifier(value)
        if kind not in {"asset", "span"} or identifier is None:
            if value is not None:
                self._invalid += 1
            return
        key = (kind, identifier)
        if key not in self._items and len(self._items) >= _MAX_EVIDENCE:
            self._truncated = True
            return
        self._items.setdefault(key, set()).add(source)

    def from_brief(self, brief: dict[str, Any] | None) -> None:
        if not isinstance(brief, dict):
            return
        candidates = brief.get("candidate_refs")
        if not isinstance(candidates, list):
            return
        if len(candidates) > 200:
            self._truncated = True
        for candidate in candidates[:200]:
            if not isinstance(candidate, dict):
                if candidate is not None:
                    self._invalid += 1
                continue
            self.add("asset", candidate.get("asset_id"), "legacy_candidate_ref")
            self.add("span", candidate.get("segment_id"), "legacy_candidate_ref")
            result_type = candidate.get("result_type")
            if result_type == "video_segment":
                self.add("span", candidate.get("id"), "legacy_candidate_ref")
            elif result_type in {
                "asset",
                "audio_asset",
                "image_asset",
                "video_asset",
            }:
                self.add("asset", candidate.get("id"), "legacy_candidate_ref")

    def from_timeline(self, timeline: dict[str, Any] | None) -> None:
        if not isinstance(timeline, dict):
            return
        tracks = timeline.get("tracks")
        if not isinstance(tracks, list):
            return
        if len(tracks) > 1_000:
            self._truncated = True
        for track in tracks[:1_000]:
            if not isinstance(track, dict):
                continue
            clips = track.get("clips")
            if not isinstance(clips, list):
                continue
            if len(clips) > 10_000:
                self._truncated = True
            for clip in clips[:10_000]:
                if not isinstance(clip, dict):
                    continue
                self.add("asset", clip.get("asset_id"), "timeline_clip")
                self.add("span", clip.get("segment_id"), "timeline_clip")

    def result(self) -> list[dict[str, Any]]:
        if self._invalid:
            _add_gap(
                self.gaps,
                "evidence_references_omitted",
                "One or more untyped or invalid legacy evidence references were omitted.",
            )
        if self._truncated:
            _add_gap(
                self.gaps,
                "evidence_references_truncated",
                f"Resume evidence is bounded to {_MAX_EVIDENCE} distinct references.",
            )
        result: list[dict[str, Any]] = []
        for (kind, identifier), sources in self._items.items():
            result.append(
                {
                    "evidence_id": (
                        asset_evidence_id(identifier)
                        if kind == "asset"
                        else span_evidence_id(identifier)
                    ),
                    "kind": kind,
                    "identifier": identifier,
                    "sources": sorted(sources),
                }
            )
        return result


def _operation_summary(
    timeline: dict[str, Any] | None, gaps: list[dict[str, str]]
) -> dict[str, Any]:
    if not isinstance(timeline, dict):
        _add_gap(
            gaps,
            "operation_summary_withheld",
            "Operation summaries were withheld because this Timeline revision was not eligible for derived-content projection.",
        )
        return {
            "source": "unavailable",
            "observed_count": 0,
            "returned_count": 0,
            "operations": [],
        }
    provenance = timeline.get("provenance")
    operations = provenance.get("operations") if isinstance(provenance, dict) else None
    if not isinstance(operations, list):
        _add_gap(
            gaps,
            "operation_summary_unavailable",
            "Verified Timeline provenance has no bounded operation array.",
        )
        return {
            "source": "unavailable",
            "observed_count": 0,
            "returned_count": 0,
            "operations": [],
        }
    result: list[dict[str, Any]] = []
    omitted = 0
    unknown = 0
    for raw in operations[:100]:
        if not isinstance(raw, dict):
            omitted += 1
            continue
        subject: dict[str, str] | None = None
        for field, kind in _SUBJECT_FIELDS:
            identifier = _identifier(raw.get(field))
            if identifier is not None:
                subject = {"kind": kind, "identifier": identifier}
                break
        operation_type = raw.get("op")
        if not isinstance(operation_type, str) or operation_type not in _OPERATION_TYPES:
            operation_type = "unknown_operation"
            unknown += 1
        result.append({"type": operation_type, "subject": subject})
    if len(operations) > 100:
        _add_gap(
            gaps,
            "operation_summary_truncated",
            "Operation type summaries are bounded to the first 100 persisted operations.",
        )
    if omitted:
        _add_gap(
            gaps,
            "operation_types_omitted",
            "Unsupported or malformed operation records were omitted from the summary.",
        )
    if unknown:
        _add_gap(
            gaps,
            "unknown_operation_types_present",
            "Unknown operation names were replaced with the non-executable unknown_operation marker.",
        )
    return {
        "source": "verified_timeline_content_provenance",
        "observed_count": len(operations),
        "returned_count": len(result),
        "operations": result,
    }


def _base(
    object_name: str,
    *,
    status: str,
    capability_available: bool,
    gaps: list[dict[str, str]],
    next_actions: list[str],
) -> dict[str, Any]:
    return {
        "object": object_name,
        "schema_version": PROJECT_RESUME_SCHEMA_VERSION,
        "status": status,
        "source": "sqlite_read_only",
        "mode": "safe_default_read_only",
        "capability_available": capability_available,
        "gaps": gaps,
        "next_actions": next_actions,
        "safety": safety_summary(),
    }


def _capability_gaps(
    capabilities: dict[str, Any], gaps: list[dict[str, str]]
) -> None:
    if not capabilities.get("database_identity_available"):
        _add_gap(
            gaps,
            "database_identity_unavailable",
            "The snapshot does not expose a verifiable MemoLens database identity.",
        )
    if not capabilities.get("brief_schema_available"):
        _add_gap(
            gaps,
            "creative_brief_schema_unavailable",
            "The installed index does not expose the canonical creative brief relation.",
        )
    if not capabilities.get("timeline_schema_available"):
        _add_gap(
            gaps,
            "timeline_history_schema_unavailable",
            "The installed index does not expose a compatible Timeline revision relation.",
        )
    elif capabilities.get("timeline_schema_mode") == "legacy_compatibility":
        _add_gap(
            gaps,
            "legacy_timeline_relation_adapter",
            "Timeline history is being read through the fixed timeline_revisions compatibility adapter.",
        )


class ProjectResumePresenter:
    """Turn private project rows into bounded, explainable Agent contracts."""

    def project_list(self, raw: dict[str, Any]) -> dict[str, Any]:
        capabilities = raw.get("capabilities") if isinstance(raw, dict) else {}
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        gaps = _gaps()
        _capability_gaps(capabilities, gaps)
        available = bool(capabilities.get("project_list_available"))
        projects: list[dict[str, Any]] = []
        for item in raw.get("projects", []) if isinstance(raw, dict) else []:
            item_gaps: list[dict[str, str]] = []
            identity = _project_identity(item, item_gaps)
            if identity is None:
                _add_gap(
                    gaps,
                    "projects_omitted",
                    "One or more projects with invalid identities were omitted.",
                )
                continue
            brief_revision = _positive_int(
                item.get("brief_revision"), maximum=1_000_000
            )
            brief_count = _nonnegative_int(
                item.get("brief_revision_count"), maximum=1_000_000
            ) or 0
            branch_count = _nonnegative_int(
                item.get("timeline_branch_count"), maximum=1_000_000
            ) or 0
            timeline_count = _nonnegative_int(
                item.get("timeline_revision_count"), maximum=10_000_000
            ) or 0
            if brief_revision is None:
                _add_gap(
                    item_gaps,
                    "legacy_brief_missing",
                    "This project has no persisted legacy brief revision.",
                )
            if timeline_count == 0:
                _add_gap(
                    item_gaps,
                    "timeline_missing",
                    "This project has no persisted Timeline revision.",
                )
            _add_gap(
                item_gaps,
                "project_integrity_unverified_in_list",
                "Project-list reports persisted row presence only; use project-open to verify brief and Timeline integrity.",
            )
            completeness = (
                "presence_only"
                if brief_revision is not None and timeline_count > 0
                else "incomplete"
            )
            projects.append(
                {
                    **identity,
                    "completeness": completeness,
                    "brief_revision": brief_revision,
                    "brief_revision_count": brief_count,
                    "timeline_branch_count": branch_count,
                    "timeline_revision_count": timeline_count,
                    "gaps": item_gaps,
                }
            )
        if projects:
            _add_gap(
                gaps,
                "project_integrity_unverified_in_list",
                "Project-list reports persisted row presence only; use project-open to verify brief and Timeline integrity.",
            )
        completeness = (
            "presence_only"
            if available
            and capabilities.get("project_resume_available")
            and all(item["completeness"] == "presence_only" for item in projects)
            else "incomplete"
        )
        status = "completed" if available else "capability_unavailable"
        return {
            **_base(
                "memolens.project_list",
                status=status,
                capability_available=available,
                gaps=gaps,
                next_actions=["project-open"],
            ),
            "status_filter": (
                raw.get("status_filter")
                if isinstance(raw, dict)
                and raw.get("status_filter")
                in {"current", "draft", "active", "archived", "all"}
                else "current"
            ),
            "completeness": completeness,
            "result_count": len(projects),
            "projects": projects,
            "next_cursor": (
                raw.get("next_cursor")
                if isinstance(raw, dict) and isinstance(raw.get("next_cursor"), str)
                else None
            ),
        }

    def project_open(self, raw: dict[str, Any]) -> dict[str, Any]:
        capabilities = raw.get("capabilities") if isinstance(raw, dict) else {}
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        gaps = _gaps()
        _capability_gaps(capabilities, gaps)
        project = _project_identity(
            raw.get("project") if isinstance(raw, dict) else None, gaps
        )
        blueprint_head = _persisted_blueprint_head(
            raw.get("persisted_blueprint") if isinstance(raw, dict) else None,
            gaps,
        )
        brief, parsed_brief = _brief_projection(
            raw.get("brief") if isinstance(raw, dict) else None, gaps
        )
        latest_brief = brief.get("revision") if isinstance(brief, dict) else None
        head, parsed_timeline = _timeline_projection(
            raw.get("timeline_head") if isinstance(raw, dict) else None,
            gaps,
            latest_brief_revision=(
                latest_brief if isinstance(latest_brief, int) else None
            ),
            kind="latest_observed_timeline_head",
            selection_policy=(
                "per_branch_max_revision_then_created_at_desc_revision_desc_id_asc"
            ),
            require_latest_brief_binding=True,
        )
        if head is None:
            _add_gap(
                gaps,
                "timeline_missing",
                "This project has no persisted Timeline revision.",
            )
        branch_count = (
            _nonnegative_int(
                raw.get("timeline_branch_count") if isinstance(raw, dict) else None,
                maximum=1_000_000,
            )
            or 0
        )
        if branch_count > 1:
            _add_gap(
                gaps,
                "latest_observed_timeline_head_ambiguous",
                "Multiple Timeline branches exist; the returned head is a deterministic observation, not an authoritative project head.",
            )
        evidence = _EvidenceCollector(gaps)
        evidence.from_timeline(parsed_timeline)
        evidence.from_brief(parsed_brief)
        evidence_refs = evidence.result()
        capability_available = bool(capabilities.get("project_resume_available"))
        integrity_complete = bool(
            brief
            and brief["integrity"]["verified"]
            and head
            and head["integrity"]["verified"]
            and head["validation_status"] == "valid"
            and head["brief_binding"]["exists"] is True
            and head["brief_binding"]["is_latest"] is True
        )
        completeness = (
            "complete"
            if project is not None and capability_available and integrity_complete
            else "incomplete"
        )
        if not capabilities.get("project_list_available") or not capability_available:
            status = "capability_unavailable"
        elif completeness == "incomplete":
            status = "incomplete"
        else:
            status = "completed"
        next_actions = (
            ["blueprint-get", "blueprint-history", "wiki-evidence"]
            if blueprint_head is not None
            else ["project-history", "timeline-get", "wiki-evidence"]
        )
        return {
            **_base(
                "memolens.project_resume",
                status=status,
                capability_available=capability_available,
                gaps=gaps,
                next_actions=next_actions,
            ),
            "completeness": completeness,
            "project": project,
            "brief": brief,
            "creative_blueprint_head": blueprint_head,
            "latest_observed_timeline_head": head,
            "evidence_refs": evidence_refs,
            "selection": {
                "policy": (
                    blueprint_head["selection_policy"]
                    if blueprint_head is not None
                    else "per_branch_max_revision_then_created_at_desc_revision_desc_id_asc"
                ),
                "branch_head_count": branch_count,
                "authoritative_project_head": blueprint_head is not None,
            },
        }

    def project_history(self, raw: dict[str, Any]) -> dict[str, Any]:
        capabilities = raw.get("capabilities") if isinstance(raw, dict) else {}
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        gaps = _gaps()
        _capability_gaps(capabilities, gaps)
        project = _project_identity(
            raw.get("project") if isinstance(raw, dict) else None, gaps
        )
        project_id = project.get("project_id") if project else None
        timeline_filter = (
            _identifier(raw.get("timeline_id")) if isinstance(raw, dict) else None
        )
        projected: list[dict[str, Any]] = []
        incomplete = False
        for item in raw.get("revisions", []) if isinstance(raw, dict) else []:
            item_gaps: list[dict[str, str]] = []
            revision, parsed = _timeline_projection(
                item,
                item_gaps,
                latest_brief_revision=None,
                kind="timeline_revision_summary",
                selection_policy="history_created_at_desc_revision_desc_id_asc",
                require_latest_brief_binding=False,
            )
            if revision is None:
                incomplete = True
                _add_gap(
                    gaps,
                    "history_revisions_omitted",
                    "One or more Timeline history rows with invalid identities were omitted.",
                )
                continue
            operation_summary = _operation_summary(parsed, item_gaps)
            revision["operation_summary"] = operation_summary
            revision["gaps"] = item_gaps
            if (
                not revision["integrity"]["verified"]
                or revision["validation_status"] != "valid"
                or revision["brief_binding"]["exists"] is not True
            ):
                incomplete = True
            projected.append(revision)
        available = bool(capabilities.get("project_history_available"))
        if available and not projected:
            incomplete = True
            if timeline_filter is not None:
                _add_gap(
                    gaps,
                    "timeline_history_empty",
                    "The requested Timeline branch has no persisted revisions.",
                )
            else:
                _add_gap(
                    gaps,
                    "timeline_missing",
                    "This project has no persisted Timeline revision.",
                )
        if not available:
            status = "capability_unavailable"
            incomplete = True
        elif incomplete:
            status = "incomplete"
        else:
            status = "completed"
        return {
            **_base(
                "memolens.project_history",
                status=status,
                capability_available=available,
                gaps=gaps,
                next_actions=["project-open", "timeline-get"],
            ),
            "project_id": project_id,
            "timeline_id": timeline_filter,
            "project": project,
            "completeness": "incomplete" if incomplete else "complete",
            "result_count": len(projected),
            "revisions": projected,
            "truncated": bool(raw.get("truncated")) if isinstance(raw, dict) else False,
        }

    # Short aliases keep the presenter ergonomic without changing semantic names.
    list = project_list
    open = project_open
    project_resume = project_open
    history = project_history


__all__ = [
    "PROJECT_RESUME_SCHEMA_VERSION",
    "ProjectResumePresenter",
    "validate_project_identifier",
]
