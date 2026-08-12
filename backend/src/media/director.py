from __future__ import annotations

from typing import Any

from core.media_db import MediaRepository

from .retrieval import MixedRetrievalService


class CreativeBriefError(ValueError):
    def __init__(self, code: str, message: str, *, details: object | None = None):
        super().__init__(message)
        self.code = code
        self.details = details


class CreativeDirector:
    """Rules-v1 director that freezes grounded retrieval references into an immutable brief."""

    def __init__(self, repository: MediaRepository, retrieval: MixedRetrievalService):
        self.repository = repository
        self.retrieval = retrieval

    def create_brief(self, payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        goal = str(payload.get("goal") or payload.get("prompt") or payload.get("query") or "").strip()
        if not goal:
            raise ValueError("`goal` must be a non-empty string.")
        title = str(payload.get("title") or goal[:80]).strip()
        duration_ms = payload.get("duration_ms", 30_000)
        if (
            not isinstance(duration_ms, int)
            or isinstance(duration_ms, bool)
            or not 1_000 <= duration_ms <= 30 * 60 * 1000
        ):
            raise ValueError("`duration_ms` must be an integer from 1000 to 1800000.")
        aspect_ratio = str(payload.get("aspect_ratio") or "16:9")
        if aspect_ratio not in {"16:9", "9:16", "1:1", "4:5"}:
            raise ValueError("Unsupported `aspect_ratio`.")
        audience = self._short_text(payload, "audience", "General audience")
        platform = self._short_text(payload, "platform", "General")
        tone = self._short_text(payload, "tone", "natural")
        pace = self._short_text(payload, "pace", "balanced")
        narrative_arc = self._short_text(
            payload, "narrative_arc", "A clear beginning, development, and ending.", limit=1000
        )
        must_include = self._string_list(payload, "must_include")
        must_exclude = self._string_list(payload, "must_exclude")
        raw_refs = payload.get("candidate_refs")
        if raw_refs is not None and not isinstance(raw_refs, list):
            raise ValueError("`candidate_refs` must be a string array.")
        requested_ids = [str(value) for value in raw_refs or [] if isinstance(value, str) and value]
        filters: dict[str, Any] = {"required_terms": must_include, "excluded_terms": must_exclude}
        search = self.retrieval.search(
            {
                "query": goal,
                "types": ["image_asset", "video_segment"],
                "top_k": 24,
                "filters": filters,
            }
        )
        if requested_ids:
            candidates = self.retrieval.resolve_matches(requested_ids)
            found = {str(item["id"]) for item in candidates}
            missing = [value for value in requested_ids if value not in found]
            if missing:
                raise CreativeBriefError(
                    "candidate_unavailable",
                    "One or more selected candidates are unavailable.",
                    details={"candidate_refs": missing[:8]},
                )
            conflicts = self.retrieval.constraint_conflicts(
                requested_ids,
                required_terms=must_include,
                excluded_terms=must_exclude,
            )
            if conflicts:
                raise CreativeBriefError(
                    "candidate_constraint_conflict",
                    "Selected candidates conflict with the brief's authoritative include/exclude constraints.",
                    details={"conflicts": conflicts},
                )
        else:
            candidates = list(search["results"][:12])
            if must_include:
                supplemented = self.retrieval.matches_for_required_terms(
                    must_include,
                    excluded_terms=must_exclude,
                    top_k=24,
                )
                candidates = list(
                    {str(item["id"]): item for item in [*supplemented, *candidates]}.values()
                )[:12]
                conflicts = self.retrieval.constraint_conflicts(
                    [str(item["id"]) for item in candidates],
                    required_terms=must_include,
                    excluded_terms=must_exclude,
                )
                if conflicts:
                    raise CreativeBriefError(
                        "candidate_constraint_conflict",
                        "Local assets do not collectively satisfy the brief's include constraints.",
                        details={"conflicts": conflicts},
                    )
        if not candidates:
            raise CreativeBriefError(
                "no_grounded_matches",
                "No grounded local asset matches this brief.",
                details={"missing_assets": [goal], "search_revision": search["search_revision"]},
            )
        brief = {
            "schema_version": "1",
            "goal": goal,
            "duration_ms": duration_ms,
            "aspect_ratio": aspect_ratio,
            "audience": audience,
            "platform": platform,
            "tone": tone,
            "pace": pace,
            "must_include": must_include,
            "must_exclude": must_exclude,
            "narrative_arc": narrative_arc,
            "candidate_ref_ids": [str(item["id"]) for item in candidates],
            "candidate_refs": candidates,
            "candidates": candidates,
            "missing_assets": [],
            "assumptions": ["Local rules-v1 director; no external model or media upload was used."],
            "director": {"profile": "rules-v1", "external_model": False, "search_revision": search["search_revision"]},
        }
        project = self.repository.create_project(
            title,
            brief,
            {
                "created_by": "rules-v1",
                "search_revision": search["search_revision"],
                "analysis_heads": search["analysis_heads"],
                "external_model": False,
            },
        )
        project["candidates"] = candidates
        return project, search

    @staticmethod
    def _short_text(payload: dict[str, object], key: str, default: str, *, limit: int = 200) -> str:
        raw = payload.get(key)
        if raw is None:
            return default
        if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > limit:
            raise ValueError(f"`{key}` must be a non-empty string of at most {limit} characters.")
        return raw.strip()

    @staticmethod
    def _string_list(payload: dict[str, object], key: str) -> list[str]:
        raw = payload.get(key, [])
        if not isinstance(raw, list) or len(raw) > 32:
            raise ValueError(f"`{key}` must be a string array with at most 32 entries.")
        if any(not isinstance(value, str) or not value.strip() or len(value.strip()) > 120 for value in raw):
            raise ValueError(f"`{key}` entries must be non-empty strings of at most 120 characters.")
        return list(dict.fromkeys(value.strip() for value in raw))
