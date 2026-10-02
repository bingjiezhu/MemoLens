from __future__ import annotations

import hashlib
import sqlite3
from copy import deepcopy
from typing import Any

from core.media_db import IdempotentWriteResult, MediaRepository, canonical_json
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.residual_identity_contract import (
    ResidualIdentityContractError,
    require_valid_residual_binding,
)

from .creator_memory import PROFILE_FIELDS, CreatorMemoryService
from .mixed_presenter import present_explicit_matches
from .retrieval import MixedRetrievalService
from .usage_projection import (
    UsageQueryPolicy,
    annotate_usage_candidates,
    materialize_residual_candidates,
    residual_half_open_intervals,
    union_half_open_intervals,
)


_USAGE_SELECTION_POLICIES = frozenset({"unused_only", "prefer_unused", "allow_reuse"})
_USAGE_SELECTION_KEYS = frozenset(
    {"policy", "usage_revision", "derivative_revision", "candidates"}
)
_USAGE_CANDIDATE_KEYS = frozenset(
    {
        "id",
        "asset_id",
        "asset_source_id",
        "result_type",
        "analysis_run_id",
        "analysis_revision",
        "start_ms",
        "end_ms",
        "usage",
    }
)
_USAGE_PROJECTION_KEYS = frozenset(
    {
        "asset_id",
        "media_kind",
        "occurrence_count",
        "used",
        "used_in",
        "source_domain",
        "candidate_domain",
        "used_intervals",
        "residual_intervals",
        "fully_used",
        "has_residual",
    }
)
_CANDIDATE_OBSERVATIONS_MAX_BYTES = 256 * 1024
_MAX_EXPLICIT_CANDIDATES = 24


class CreativeBriefError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: object | None = None,
        status: int = 400,
    ):
        super().__init__(message)
        self.code = code
        self.details = details
        self.status = status


class CreativeDirector:
    """Rules-v1 director that freezes grounded retrieval references into an immutable brief."""

    def __init__(
        self,
        repository: MediaRepository,
        retrieval: MixedRetrievalService,
        creator_memory: CreatorMemoryService | None = None,
    ):
        self.repository = repository
        self.retrieval = retrieval
        self.creator_memory = creator_memory

    def create_brief(
        self,
        payload: dict[str, object],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        if connection is None:
            with self.repository.transaction(immediate=True) as target:
                return self.create_brief(payload, connection=target)
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
        creator_profile_ref, applied_profile_fields = self._creator_profile_context(payload)
        raw_refs = payload.get("candidate_refs")
        if raw_refs is not None and not isinstance(raw_refs, list):
            raise ValueError("`candidate_refs` must be a bounded reference array.")
        assert connection is not None
        requested_ids = self._candidate_reference_ids(
            raw_refs,
            connection=connection,
        )
        usage_selection = self._usage_selection(payload, raw_refs, requested_ids)
        filters: dict[str, Any] = {"required_terms": must_include, "excluded_terms": must_exclude}
        search = self.retrieval.search(
            {
                "query": goal,
                "types": ["image_asset", "video_segment"],
                "top_k": 24,
                "filters": filters,
            },
            connection=connection,
        )
        if requested_ids:
            if usage_selection is not None:
                candidates, usage_selection = self._admit_usage_selection(
                    usage_selection,
                    requested_ids=requested_ids,
                    connection=connection,
                )
            else:
                candidates = self.retrieval.resolve_matches(
                    requested_ids,
                    connection=connection,
                )
                found = {str(item["id"]) for item in candidates}
                missing = [value for value in requested_ids if value not in found]
                if missing:
                    raise CreativeBriefError(
                        "candidate_unavailable",
                        "One or more selected candidates are unavailable.",
                        details={"candidate_refs": missing[:8]},
                    )
            constraint_ids = [
                str(
                    item.get("parent_segment_id")
                    if item.get("residual_binding") is not None
                    else item["id"]
                )
                for item in candidates
            ]
            conflicts = self.retrieval.constraint_conflicts(
                constraint_ids,
                required_terms=must_include,
                excluded_terms=must_exclude,
                connection=connection,
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
                    connection=connection,
                )
                candidates = list({str(item["id"]): item for item in [*supplemented, *candidates]}.values())[:12]
                conflicts = self.retrieval.constraint_conflicts(
                    [str(item["id"]) for item in candidates],
                    required_terms=must_include,
                    excluded_terms=must_exclude,
                    connection=connection,
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
        if usage_selection is not None and not requested_ids:
            candidates, usage_selection = self._admit_usage_selection(
                usage_selection,
                requested_ids=requested_ids,
                connection=connection,
            )
        candidate_observations = self._admit_candidate_observations(
            payload,
            candidates=candidates,
            requested_ids=requested_ids,
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
        if usage_selection is not None:
            binding = self._usage_provenance_binding(usage_selection)
            brief["usage_selection"] = usage_selection
            brief["director"]["usage_selection"] = binding
        if candidate_observations:
            observation_binding = self._candidate_observation_binding(
                candidate_observations,
            )
            brief["candidate_observations"] = candidate_observations
            brief["director"]["candidate_observations"] = observation_binding
        if creator_profile_ref is not None:
            brief["creator_profile_ref"] = creator_profile_ref
            brief["applied_profile_fields"] = applied_profile_fields
        provenance = {
            "created_by": "rules-v1",
            "search_revision": search["search_revision"],
            "analysis_heads": search["analysis_heads"],
            "external_model": False,
        }
        if usage_selection is not None:
            provenance["usage_selection"] = self._usage_provenance_binding(usage_selection)
        if candidate_observations:
            provenance["candidate_observations"] = self._candidate_observation_binding(
                candidate_observations,
            )
        if creator_profile_ref is not None:
            provenance["creator_profile_ref"] = creator_profile_ref
            provenance["applied_profile_fields"] = applied_profile_fields
        project = self.repository.create_project(
            title,
            brief,
            provenance,
            connection=connection,
        )
        project["candidates"] = candidates
        return project, search

    def _candidate_reference_ids(
        self,
        raw_refs: object,
        *,
        connection: sqlite3.Connection,
    ) -> list[str]:
        if raw_refs is None:
            return []
        if not isinstance(raw_refs, list) or len(raw_refs) > _MAX_EXPLICIT_CANDIDATES:
            raise ValueError(
                f"`candidate_refs` must contain at most {_MAX_EXPLICIT_CANDIDATES} references."
            )
        normalized: list[str] = []
        for value in raw_refs:
            if type(value) is str:
                candidate_id = self._identifier(value, "Candidate reference")
            elif type(value) is dict and set(value) == {"legacy_id", "library_root_id"}:
                legacy_id = self._identifier(value.get("legacy_id"), "Legacy image reference")
                library_root_id = self._identifier(
                    value.get("library_root_id"),
                    "Legacy image Library scope",
                )
                try:
                    resolved = self.repository.resolve_image_asset_reference_in_transaction(
                        connection,
                        legacy_id,
                        library_root_id=library_root_id,
                    )
                except ImageAnalysisPersistenceError as exc:
                    raise CreativeBriefError(
                        "candidate_alias_conflict",
                        "The scoped legacy image reference is no longer authoritative.",
                        status=409,
                    ) from exc
                if resolved is None:
                    raise CreativeBriefError(
                        "candidate_unavailable",
                        "The scoped legacy image reference is unavailable.",
                        status=409,
                    )
                candidate_id = str(resolved["asset_id"])
            else:
                raise ValueError(
                    "Each candidate reference must be a canonical ID or a closed scoped legacy alias."
                )
            normalized.append(candidate_id)
        if len(set(normalized)) != len(normalized):
            raise ValueError("`candidate_refs` must resolve to unique canonical candidates.")
        return normalized

    @staticmethod
    def _candidate_observations_request(payload: dict[str, object]) -> list[dict[str, object]] | None:
        if "candidate_observations" not in payload:
            return None
        raw = payload.get("candidate_observations")
        if type(raw) is not list or len(raw) > _MAX_EXPLICIT_CANDIDATES:
            raise CreativeBriefError(
                "candidate_observation_invalid",
                "`candidate_observations` must be a bounded array.",
            )
        if any(type(value) is not dict for value in raw):
            raise CreativeBriefError(
                "candidate_observation_invalid",
                "Each candidate observation must be one canonical image observation object.",
            )
        normalized = [deepcopy(value) for value in raw]
        try:
            encoded = canonical_json(normalized).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise CreativeBriefError(
                "candidate_observation_invalid",
                "Candidate observations must be canonical JSON values.",
            ) from exc
        if len(encoded) > _CANDIDATE_OBSERVATIONS_MAX_BYTES:
            raise CreativeBriefError(
                "candidate_observation_invalid",
                "Candidate observations exceed the 256 KiB canonical envelope limit.",
            )
        return normalized

    def _admit_candidate_observations(
        self,
        payload: dict[str, object],
        *,
        candidates: list[dict[str, object]],
        requested_ids: list[str],
    ) -> list[dict[str, object]]:
        client = self._candidate_observations_request(payload)
        images = [
            candidate
            for candidate in candidates
            if candidate.get("result_type") == "image_asset"
        ]
        authoritative: list[dict[str, object]] = []
        for candidate in images:
            self._admit_current_image_analysis_binding(candidate)
            observation = candidate.get("canonical_image_observation")
            assert isinstance(observation, dict)
            authoritative.append(deepcopy(observation))
        if not requested_ids:
            if client not in (None, []):
                raise CreativeBriefError(
                    "candidate_observation_invalid",
                    "Client observations require explicit candidate references.",
                )
            return authoritative
        if not images:
            if client not in (None, []):
                raise CreativeBriefError(
                    "candidate_observation_invalid",
                    "Video-only selections cannot carry image observations.",
                )
            return []
        if client is None or len(client) != len(authoritative):
            raise CreativeBriefError(
                "candidate_observation_conflict",
                "Selected images require their exact current canonical observations.",
                status=409,
            )
        for expected, observed in zip(authoritative, client, strict=True):
            if canonical_json(expected) != canonical_json(observed):
                raise CreativeBriefError(
                    "candidate_observation_conflict",
                    "A selected image changed after search; search again before creating the brief.",
                    details={"candidate_ref": expected.get("asset_id")},
                    status=409,
                )
        return client

    @staticmethod
    def _candidate_observation_binding(
        observations: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "schema_version": "1",
            "asset_ids": [str(value["asset_id"]) for value in observations],
            "content_sha256": hashlib.sha256(
                canonical_json(observations).encode("utf-8")
            ).hexdigest(),
        }

    @staticmethod
    def _usage_selection(
        payload: dict[str, object],
        raw_refs: object,
        requested_ids: list[str],
    ) -> dict[str, object] | None:
        if "usage_selection" not in payload:
            return None
        if (
            not isinstance(raw_refs, list)
            or not raw_refs
            or len(raw_refs) > 24
            or any(type(value) is not str or not value or len(value) > 256 or "\x00" in value for value in raw_refs)
            or len(set(raw_refs)) != len(raw_refs)
        ):
            raise CreativeBriefError(
                "usage_selection_requires_candidates",
                "Usage-aware brief creation requires 1 to 24 unique explicit candidate references.",
            )
        raw = payload.get("usage_selection")
        if type(raw) is not dict or set(raw) != _USAGE_SELECTION_KEYS:
            raise CreativeBriefError(
                "usage_selection_invalid",
                "`usage_selection` must be a closed policy, revision, and candidates object.",
            )
        policy = raw.get("policy")
        if type(policy) is not str or policy not in _USAGE_SELECTION_POLICIES:
            raise CreativeBriefError(
                "usage_selection_policy_invalid",
                "`usage_selection.policy` is unsupported.",
            )
        usage_revision = raw.get("usage_revision")
        if not CreativeDirector._is_sha256(usage_revision):
            raise CreativeBriefError(
                "usage_selection_invalid",
                "`usage_selection.usage_revision` must be a lowercase SHA-256 digest.",
            )
        derivative_revision = raw.get("derivative_revision")
        if not CreativeDirector._is_sha256(derivative_revision):
            raise CreativeBriefError(
                "usage_selection_invalid",
                "`usage_selection.derivative_revision` must be a lowercase SHA-256 digest.",
            )
        raw_candidates = raw.get("candidates")
        if type(raw_candidates) is not list or not raw_candidates or len(raw_candidates) > 24:
            raise CreativeBriefError(
                "usage_selection_requires_candidates",
                "Usage-aware brief creation requires 1 to 24 exact candidate projections.",
            )
        normalized_candidates = [CreativeDirector._usage_candidate(value) for value in raw_candidates]
        candidate_ids = [str(value["id"]) for value in normalized_candidates]
        if candidate_ids != requested_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise CreativeBriefError(
                "usage_selection_requires_candidates",
                "Usage candidate projections must correspond one-for-one with `candidate_refs` in the same order.",
            )
        return {
            "policy": policy,
            "usage_revision": usage_revision,
            "derivative_revision": derivative_revision,
            "candidates": normalized_candidates,
        }

    @staticmethod
    def _usage_candidate(raw: object) -> dict[str, object]:
        raw_keys = set(raw) if type(raw) is dict else set()
        candidate_id_value = raw.get("id") if type(raw) is dict else None
        is_residual = (
            type(candidate_id_value) is str
            and candidate_id_value.startswith("rseg_")
        )
        expected_keys = (
            _USAGE_CANDIDATE_KEYS | {"residual_binding"}
            if is_residual
            else _USAGE_CANDIDATE_KEYS
        )
        if type(raw) is not dict or raw_keys != expected_keys:
            raise CreativeBriefError(
                "usage_selection_invalid",
                "Each Usage candidate must use the closed candidate projection schema.",
            )
        candidate_id = CreativeDirector._identifier(raw.get("id"), "Usage candidate identity")
        asset_id = CreativeDirector._identifier(raw.get("asset_id"), "Usage candidate asset identity")
        asset_source_id = CreativeDirector._identifier(
            raw.get("asset_source_id"),
            "Usage candidate source identity",
        )
        result_type = raw.get("result_type")
        if result_type not in {"image_asset", "video_segment"}:
            raise CreativeBriefError("usage_selection_invalid", "Usage candidate media kind is invalid.")
        analysis_run_id = raw.get("analysis_run_id")
        analysis_revision = raw.get("analysis_revision")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        if result_type == "image_asset":
            if start_ms is not None or end_ms is not None:
                raise CreativeBriefError(
                    "usage_selection_invalid",
                    "Image Usage candidates cannot carry source ranges.",
                )
            analysis_run_id = CreativeDirector._identifier(
                analysis_run_id,
                "Usage candidate analysis identity",
            )
            if type(analysis_revision) is not int or not 1 <= analysis_revision <= 1_000_000:
                raise CreativeBriefError(
                    "usage_selection_invalid",
                    "Usage candidate analysis revision is invalid.",
                )
        else:
            analysis_run_id = CreativeDirector._identifier(
                analysis_run_id,
                "Usage candidate analysis identity",
            )
            if type(analysis_revision) is not int or not 1 <= analysis_revision <= 1_000_000:
                raise CreativeBriefError(
                    "usage_selection_invalid",
                    "Usage candidate analysis revision is invalid.",
                )
            CreativeDirector._interval([start_ms, end_ms], "Usage candidate source range")
        usage = CreativeDirector._usage_projection(
            raw.get("usage"),
            candidate_asset_id=asset_id,
            result_type=str(result_type),
            start_ms=start_ms,
            end_ms=end_ms,
        )
        normalized = {
            "id": candidate_id,
            "asset_id": asset_id,
            "asset_source_id": asset_source_id,
            "result_type": result_type,
            "analysis_run_id": analysis_run_id,
            "analysis_revision": analysis_revision,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "usage": usage,
        }
        if is_residual:
            try:
                residual_binding = require_valid_residual_binding(
                    raw.get("residual_binding")
                )
            except (ResidualIdentityContractError, TypeError, ValueError) as exc:
                raise CreativeBriefError(
                    "usage_selection_invalid",
                    "Residual Usage candidate binding is invalid.",
                ) from exc
            if not (
                residual_binding["residual_id"] == candidate_id
                and residual_binding["asset_id"] == asset_id
                and residual_binding["asset_source_id"] == asset_source_id
                and residual_binding["analysis_run_id"] == analysis_run_id
                and residual_binding["analysis_revision"] == analysis_revision
                and residual_binding["source_in_ms"] == start_ms
                and residual_binding["source_out_ms"] == end_ms
            ):
                raise CreativeBriefError(
                    "usage_selection_invalid",
                    "Residual Usage candidate binding does not match its projection.",
                )
            normalized["residual_binding"] = residual_binding
        return normalized

    @staticmethod
    def _usage_projection(
        raw: object,
        *,
        candidate_asset_id: str,
        result_type: str,
        start_ms: object,
        end_ms: object,
    ) -> dict[str, object]:
        if type(raw) is not dict or set(raw) != _USAGE_PROJECTION_KEYS:
            raise CreativeBriefError(
                "usage_selection_invalid",
                "Each Usage projection must use the closed canonical projection schema.",
            )
        asset_id = CreativeDirector._identifier(raw.get("asset_id"), "Usage projection asset identity")
        if asset_id != candidate_asset_id:
            raise CreativeBriefError("usage_selection_invalid", "Usage projection asset identity does not match.")
        media_kind = raw.get("media_kind")
        expected_kind = "image" if result_type == "image_asset" else "video"
        if media_kind != expected_kind:
            raise CreativeBriefError("usage_selection_invalid", "Usage projection media kind does not match.")
        occurrence_count = raw.get("occurrence_count")
        if type(occurrence_count) is not int or not 0 <= occurrence_count <= 1_000_000:
            raise CreativeBriefError("usage_selection_invalid", "Usage occurrence count is invalid.")
        used = raw.get("used")
        fully_used = raw.get("fully_used")
        has_residual = raw.get("has_residual")
        if any(type(value) is not bool for value in (used, fully_used, has_residual)):
            raise CreativeBriefError("usage_selection_invalid", "Usage state flags must be booleans.")
        if used != (occurrence_count > 0):
            raise CreativeBriefError("usage_selection_invalid", "Usage state contradicts its occurrence count.")
        used_in = CreativeDirector._used_in(raw.get("used_in"), occurrence_count)
        source_domain = raw.get("source_domain")
        candidate_domain = raw.get("candidate_domain")
        used_intervals = CreativeDirector._interval_list(raw.get("used_intervals"), "used")
        residual_intervals = CreativeDirector._interval_list(raw.get("residual_intervals"), "residual")
        if result_type == "image_asset":
            if source_domain is not None or candidate_domain is not None or used_intervals or residual_intervals:
                raise CreativeBriefError("usage_selection_invalid", "Image Usage projection has an invalid time domain.")
            if fully_used != used or has_residual == used:
                raise CreativeBriefError("usage_selection_invalid", "Image Usage state is inconsistent.")
        else:
            source = CreativeDirector._interval(source_domain, "Usage source domain")
            candidate = CreativeDirector._interval(candidate_domain, "Usage candidate domain")
            if source[0] != 0 or candidate != (start_ms, end_ms) or not source[0] <= candidate[0] < candidate[1] <= source[1]:
                raise CreativeBriefError("usage_selection_invalid", "Video Usage candidate domain is inconsistent.")
            try:
                normalized_used = union_half_open_intervals(used_intervals, domain=candidate)
                expected_residual = residual_half_open_intervals(candidate, normalized_used)
            except ValueError as exc:
                raise CreativeBriefError("usage_selection_invalid", "Video Usage intervals are invalid.") from exc
            if used_intervals != normalized_used or residual_intervals != expected_residual:
                raise CreativeBriefError("usage_selection_invalid", "Video Usage intervals do not partition the candidate.")
            if fully_used != (bool(used_intervals) and not residual_intervals) or has_residual != bool(residual_intervals):
                raise CreativeBriefError("usage_selection_invalid", "Video Usage state is inconsistent.")
        return {
            "asset_id": asset_id,
            "media_kind": media_kind,
            "occurrence_count": occurrence_count,
            "used": used,
            "used_in": used_in,
            "source_domain": source_domain,
            "candidate_domain": candidate_domain,
            "used_intervals": [[start, end] for start, end in used_intervals],
            "residual_intervals": [[start, end] for start, end in residual_intervals],
            "fully_used": fully_used,
            "has_residual": has_residual,
        }

    @staticmethod
    def _used_in(raw: object, occurrence_count: int) -> list[dict[str, object]]:
        if type(raw) is not list or len(raw) > min(4_096, occurrence_count):
            raise CreativeBriefError("usage_selection_invalid", "Usage occurrence provenance is invalid.")
        normalized: list[dict[str, object]] = []
        for value in raw:
            if type(value) is not dict or set(value) != {"project_id", "export_revision"}:
                raise CreativeBriefError("usage_selection_invalid", "Usage occurrence provenance is invalid.")
            project_id = CreativeDirector._identifier(value.get("project_id"), "Usage project identity")
            export_revision = value.get("export_revision")
            if type(export_revision) is not int or not 1 <= export_revision <= 1_000_000:
                raise CreativeBriefError("usage_selection_invalid", "Usage export revision is invalid.")
            normalized.append({"project_id": project_id, "export_revision": export_revision})
        if normalized != sorted(normalized, key=lambda item: (str(item["project_id"]), int(item["export_revision"]))) or len(
            {(str(item["project_id"]), int(item["export_revision"])) for item in normalized}
        ) != len(normalized):
            raise CreativeBriefError("usage_selection_invalid", "Usage occurrence provenance must be unique and sorted.")
        return normalized

    @staticmethod
    def _interval(raw: object, label: str) -> tuple[int, int]:
        if (
            type(raw) is not list
            or len(raw) != 2
            or type(raw[0]) is not int
            or type(raw[1]) is not int
            or raw[0] < 0
            or raw[1] <= raw[0]
        ):
            raise CreativeBriefError("usage_selection_invalid", f"{label} is invalid.")
        return raw[0], raw[1]

    @staticmethod
    def _interval_list(raw: object, label: str) -> list[tuple[int, int]]:
        if type(raw) is not list or len(raw) > 4_096:
            raise CreativeBriefError("usage_selection_invalid", f"Usage {label} interval list is invalid.")
        return [CreativeDirector._interval(value, f"Usage {label} interval") for value in raw]

    @staticmethod
    def _identifier(raw: object, label: str) -> str:
        if type(raw) is not str or not raw or len(raw) > 256 or "\x00" in raw:
            raise CreativeBriefError("usage_selection_invalid", f"{label} is invalid.")
        return raw

    @staticmethod
    def _is_sha256(raw: object) -> bool:
        return type(raw) is str and len(raw) == 64 and all(character in "0123456789abcdef" for character in raw)

    def _admit_usage_selection(
        self,
        selection: dict[str, object],
        *,
        requested_ids: list[str],
        connection: sqlite3.Connection,
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        current_candidates, _ = self.repository.mixed_candidates_in_transaction(connection)
        current_asset_ids = sorted({str(value["asset_id"]) for value in current_candidates})
        facts = self.repository.canonical_material_search_facts_in_transaction(
            connection,
            current_asset_ids,
        )
        if type(facts) is not dict or set(facts) != {
            "usage_occurrences",
            "derivative_facts",
            "derivative_revision",
        }:
            raise CreativeBriefError(
                "derivative_projection_invalid",
                "Canonical derivative facts could not be validated for admission.",
                status=409,
            )
        occurrences = facts["usage_occurrences"]
        derivative_facts = facts["derivative_facts"]
        derivative_revision = facts["derivative_revision"]
        if not (
            type(occurrences) is list
            and type(derivative_facts) is list
            and self._is_sha256(derivative_revision)
            and hashlib.sha256(canonical_json(derivative_facts).encode("utf-8")).hexdigest()
            == derivative_revision
        ):
            raise CreativeBriefError(
                "derivative_projection_invalid",
                "Canonical derivative facts could not be validated for admission.",
                status=409,
            )
        if derivative_revision != selection["derivative_revision"]:
            raise CreativeBriefError(
                "derivative_revision_conflict",
                "Successful export outputs changed after search; search again before creating the brief.",
                details={
                    "expected_derivative_revision": selection["derivative_revision"],
                    "current_derivative_revision": derivative_revision,
                },
                status=409,
            )
        current_revision = hashlib.sha256(canonical_json(occurrences).encode("utf-8")).hexdigest()
        if current_revision != selection["usage_revision"]:
            raise CreativeBriefError(
                "usage_revision_conflict",
                "Canonical Usage changed after search; search again before creating the brief.",
                details={"expected_usage_revision": selection["usage_revision"], "current_usage_revision": current_revision},
                status=409,
            )
        derivative_sha256s = {
            str(value["video_sha256"])
            for value in derivative_facts
            if type(value) is dict and type(value.get("video_sha256")) is str
        }
        executable_candidates = [
            value
            for value in current_candidates
            if value.get("asset_sha256") not in derivative_sha256s
        ]
        policy_name = str(selection["policy"])
        policy = UsageQueryPolicy(
            requested=True,
            unused_only=policy_name == "unused_only",
            prefer_unused=policy_name == "prefer_unused",
            allow_reuse=policy_name != "unused_only",
        )
        projected = annotate_usage_candidates(
            executable_candidates,
            occurrences,
            policy,
        )
        projected = materialize_residual_candidates(
            projected,
            usage_revision=current_revision,
            policy=policy,
        )
        current_by_id = {str(value["id"]): value for value in projected}
        admitted: list[dict[str, object]] = []
        normalized_current: list[dict[str, object]] = []
        for candidate_id in requested_ids:
            current = current_by_id.get(candidate_id)
            if current is None:
                raise CreativeBriefError(
                    "usage_candidate_conflict",
                    "A selected candidate is no longer current and available.",
                    details={"candidate_ref": candidate_id},
                    status=409,
                )
            self._admit_current_image_analysis_binding(current)
            presented = present_explicit_matches([current], [candidate_id])[0]
            admitted.append(presented)
            normalized_current.append(
                self._usage_candidate(
                    {
                        "id": current["id"],
                        "asset_id": current["asset_id"],
                        "asset_source_id": current["asset_source_id"],
                        "result_type": current["result_type"],
                        "analysis_run_id": current.get("analysis_run_id"),
                        "analysis_revision": current.get("analysis_revision"),
                        "start_ms": current.get("start_ms"),
                        "end_ms": current.get("end_ms"),
                        "usage": current["canonical_usage"],
                        **(
                            {"residual_binding": current["residual_binding"]}
                            if current.get("residual_binding") is not None
                            else {}
                        ),
                    }
                )
            )
        if normalized_current != selection["candidates"]:
            raise CreativeBriefError(
                "usage_candidate_conflict",
                "A selected candidate projection changed after search; search again before creating the brief.",
                status=409,
            )
        if selection["policy"] == "unused_only":
            reused = [
                value["id"]
                for value in normalized_current
                if (
                    value["result_type"] == "image_asset" and value["usage"]["used"]
                )
                or (
                    value["result_type"] == "video_segment" and bool(value["usage"]["used_intervals"])
                )
            ]
            if reused:
                raise CreativeBriefError(
                    "usage_candidate_already_used",
                    "Unused-only admission rejected candidates that now include canonical Usage.",
                    details={"candidate_refs": reused[:8]},
                    status=409,
                )
        return admitted, selection

    @staticmethod
    def _admit_current_image_analysis_binding(candidate: dict[str, object]) -> None:
        """Fail closed on the mutable image status before comparing wire identity.

        The full v1 observation, not an ID-only or run/revision-only projection,
        is the image selection authority consumed by Create.
        """

        if candidate.get("result_type") != "image_asset":
            return
        status = candidate.get("analysis_status")
        analysis_run_id = candidate.get("analysis_run_id")
        analysis_revision = candidate.get("analysis_revision")
        binding_is_valid = (
            type(analysis_run_id) is str
            and bool(analysis_run_id)
            and len(analysis_run_id) <= 256
            and "\x00" not in analysis_run_id
            and type(analysis_revision) is int
            and 1 <= analysis_revision <= 1_000_000
        )
        observation = candidate.get("canonical_image_observation")
        observed_binding = (
            observation.get("analysis_binding")
            if isinstance(observation, dict)
            else None
        )
        if (
            status == "current"
            and binding_is_valid
            and isinstance(observation, dict)
            and observation.get("object") == "memolens.canonical_image_observation"
            and observation.get("schema_version") == "1"
            and observation.get("status") == "current"
            and observation.get("authority") == "canonical_image_analysis"
            and observation.get("provenance_status") == "verified_current"
            and observation.get("asset_id") == candidate.get("asset_id")
            and isinstance(observed_binding, dict)
            and observed_binding.get("analysis_run_id") == analysis_run_id
            and observed_binding.get("revision") == analysis_revision
            and CreativeDirector._is_sha256(
                observed_binding.get("content_sha256")
            )
        ):
            return
        raise CreativeBriefError(
            "candidate_observation_conflict",
            "The selected image candidate lacks one exact verified-current observation.",
            details={"candidate_ref": candidate.get("id"), "analysis_status": status},
            status=409,
        )

    @staticmethod
    def _usage_provenance_binding(selection: dict[str, object]) -> dict[str, object]:
        candidates = selection["candidates"]
        return {
            "policy": selection["policy"],
            "usage_revision": selection["usage_revision"],
            "derivative_revision": selection["derivative_revision"],
            "candidate_projection_sha256": hashlib.sha256(canonical_json(candidates).encode("utf-8")).hexdigest(),
        }

    def create_brief_idempotent(
        self,
        payload: dict[str, object],
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> IdempotentWriteResult:
        def mutation(connection: sqlite3.Connection) -> tuple[dict[str, object], int, str]:
            project, search = self.create_brief(payload, connection=connection)
            project_id = str(project["id"])
            body = {
                "object": "creative.project",
                "schema_version": "1",
                "project": project,
                "search": search,
                "id": project_id,
            }
            return body, 201, project_id

        return self.repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=idempotency_key,
            request_sha256=request_sha256,
            resource_type="creative_project",
            mutation=mutation,
        )

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

    def _creator_profile_context(
        self,
        payload: dict[str, object],
    ) -> tuple[dict[str, object] | None, list[str]]:
        raw_ref = payload.get("creator_profile_ref")
        raw_fields = payload.get("applied_profile_fields")
        if raw_ref is None and raw_fields is None:
            return None, []
        if raw_ref is None or raw_fields is None:
            raise ValueError("`creator_profile_ref` and `applied_profile_fields` must be provided together.")
        if self.creator_memory is None:
            raise ValueError("Creator Memory is unavailable in this runtime.")
        if not isinstance(raw_fields, list) or len(raw_fields) > len(PROFILE_FIELDS):
            raise ValueError("`applied_profile_fields` must be a bounded string array.")
        if any(not isinstance(field, str) or field not in PROFILE_FIELDS for field in raw_fields):
            raise ValueError("`applied_profile_fields` contains an unsupported preference field.")
        fields = list(dict.fromkeys(raw_fields))
        stored = self.creator_memory.resolve_profile_ref(raw_ref)
        profile = stored["profile"]
        if not isinstance(profile, dict):
            raise ValueError("Creator profile snapshot is invalid.")
        for field in fields:
            if field not in profile or field not in payload:
                raise ValueError(f"Applied creator preference `{field}` must be explicit in the brief payload.")
            normalized = self.creator_memory.validate_profile({field: payload[field]})[field]
            if normalized != profile[field]:
                raise ValueError(f"Brief field `{field}` does not match the referenced creator profile revision.")
        reference = {
            "profile_id": stored["profile_id"],
            "revision": stored["revision"],
            "content_sha256": stored["content_sha256"],
        }
        return reference, fields
