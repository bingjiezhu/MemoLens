from __future__ import annotations

import hashlib
import re
import sqlite3

from core.media_db import (
    CanonicalExportIntegrityError,
    MediaRepository,
    canonical_json,
    content_sha256,
)

from .mixed_presenter import present_explicit_matches, present_search_response
from .mixed_ranking import (
    find_constraint_conflicts,
    grounded_candidates,
    rank_candidates,
    select_non_overlapping,
)
from .mixed_search_contract import parse_mixed_search_request
from .usage_projection import (
    annotate_usage_candidates,
    materialize_residual_candidates,
)


class MixedRetrievalService:
    """Honest lexical fallback across current images and current successful video segments."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    @staticmethod
    def _empty_material_search_facts() -> dict[str, object]:
        derivative_facts: list[dict[str, object]] = []
        return {
            "usage_occurrences": [],
            "derivative_facts": derivative_facts,
            "derivative_revision": content_sha256(derivative_facts),
        }

    def _opens_transactional_material_snapshot(self) -> bool:
        return callable(getattr(self.repository, "transaction", None)) and callable(
            getattr(
                self.repository,
                "canonical_material_search_facts_in_transaction",
                None,
            )
        )

    @staticmethod
    def _validated_material_search_facts(
        value: object,
    ) -> tuple[list[dict[str, object]], set[str], str]:
        if type(value) is not dict or set(value) != {
            "usage_occurrences",
            "derivative_facts",
            "derivative_revision",
        }:
            raise CanonicalExportIntegrityError(
                "canonical_export_derivative_projection_invalid"
            )
        occurrences = value["usage_occurrences"]
        derivative_facts = value["derivative_facts"]
        derivative_revision = value["derivative_revision"]
        if not (
            type(occurrences) is list
            and type(derivative_facts) is list
            and type(derivative_revision) is str
            and re.fullmatch(r"[0-9a-f]{64}", derivative_revision) is not None
            and content_sha256(derivative_facts) == derivative_revision
        ):
            raise CanonicalExportIntegrityError(
                "canonical_export_derivative_projection_invalid"
            )
        derivative_sha256s: set[str] = set()
        for fact in derivative_facts:
            if not (
                type(fact) is dict
                and set(fact)
                == {
                    "project_id",
                    "export_revision",
                    "export_revision_sha256",
                    "job_id",
                    "operation_id",
                    "output_role",
                    "video_sha256",
                }
                and type(fact["project_id"]) is str
                and bool(fact["project_id"])
                and type(fact["export_revision"]) is int
                and 1 <= fact["export_revision"] <= 1_000_000
                and type(fact["export_revision_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", fact["export_revision_sha256"])
                is not None
                and type(fact["job_id"]) is str
                and bool(fact["job_id"])
                and type(fact["operation_id"]) is str
                and bool(fact["operation_id"])
                and fact["output_role"] == "canonical_rendered_video"
                and type(fact["video_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}", fact["video_sha256"])
                is not None
            ):
                raise CanonicalExportIntegrityError(
                    "canonical_export_derivative_projection_invalid"
                )
            derivative_sha256s.add(str(fact["video_sha256"]))
        fact_order = (
            "project_id",
            "export_revision",
            "export_revision_sha256",
            "job_id",
            "operation_id",
            "output_role",
            "video_sha256",
        )
        ordered = sorted(
            derivative_facts,
            key=lambda fact: tuple(fact[field] for field in fact_order),
        )
        identities = {
            tuple(fact[field] for field in fact_order)
            for fact in derivative_facts
        }
        if derivative_facts != ordered or len(identities) != len(derivative_facts):
            raise CanonicalExportIntegrityError(
                "canonical_export_derivative_projection_invalid"
            )
        return list(occurrences), derivative_sha256s, derivative_revision

    def _material_snapshot(
        self,
        *,
        connection: sqlite3.Connection | None,
        include_archived: bool,
    ) -> tuple[
        list[dict[str, object]],
        dict[str, str],
        list[dict[str, object]],
        str,
    ]:
        if connection is None:
            candidates, heads = (
                self.repository.mixed_candidates(include_archived=True)
                if include_archived
                else self.repository.mixed_candidates()
            )
            projector = getattr(
                self.repository,
                "canonical_material_search_facts",
                None,
            )
        else:
            candidates, heads = (
                self.repository.mixed_candidates_in_transaction(
                    connection,
                    include_archived=True,
                )
                if include_archived
                else self.repository.mixed_candidates_in_transaction(connection)
            )
            projector = getattr(
                self.repository,
                "canonical_material_search_facts_in_transaction",
                None,
            )
        asset_ids = sorted(
            {
                str(candidate["asset_id"])
                for candidate in candidates
                if candidate.get("asset_id") is not None
            }
        )
        if callable(projector):
            facts = (
                projector(connection, asset_ids)
                if connection is not None
                else projector(asset_ids)
            )
        elif isinstance(self.repository, MediaRepository):
            raise CanonicalExportIntegrityError(
                "canonical_export_derivative_projector_unavailable"
            )
        else:
            # Narrow compatibility for isolated ranking test doubles.  The
            # production repository is required to expose the validator above
            # and cannot take this branch.
            facts = self._empty_material_search_facts()
            occurrence_reader = getattr(
                self.repository,
                (
                    "canonical_usage_occurrences_for_search_in_transaction"
                    if connection is not None
                    else "canonical_usage_occurrences_for_search"
                ),
                None,
            )
            if callable(occurrence_reader):
                facts["usage_occurrences"] = (
                    occurrence_reader(connection, asset_ids)
                    if connection is not None
                    else occurrence_reader(asset_ids)
                )
        occurrences, derivative_sha256s, derivative_revision = (
            self._validated_material_search_facts(facts)
        )
        retained: list[dict[str, object]] = []
        for candidate in candidates:
            asset_sha256 = candidate.get("asset_sha256")
            if isinstance(self.repository, MediaRepository) and (
                type(asset_sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", asset_sha256) is None
            ):
                raise CanonicalExportIntegrityError(
                    "canonical_material_asset_sha256_invalid"
                )
            if asset_sha256 in derivative_sha256s:
                continue
            retained.append(candidate)
        return retained, heads, occurrences, derivative_revision

    def search(
        self,
        payload: dict[str, object],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        search_request = parse_mixed_search_request(payload)
        if connection is None and self._opens_transactional_material_snapshot():
            with self.repository.transaction() as snapshot:
                return self.search(payload, connection=snapshot)
        candidates, heads, occurrences, derivative_revision = self._material_snapshot(
            connection=connection,
            include_archived=False,
        )
        usage_revision: str | None = None
        if search_request.usage.requested:
            usage_revision = hashlib.sha256(
                canonical_json(occurrences).encode("utf-8")
            ).hexdigest()
            candidates = annotate_usage_candidates(
                candidates,
                occurrences,
                search_request.usage,
            )
            candidates = materialize_residual_candidates(
                candidates,
                usage_revision=usage_revision,
                policy=search_request.usage,
            )
        ranked = rank_candidates(candidates, search_request)
        grounded = grounded_candidates(ranked)
        selected = select_non_overlapping(grounded, top_k=search_request.top_k)
        return present_search_response(
            search_request,
            analysis_heads=heads,
            ranked=ranked,
            grounded=grounded,
            selected=selected,
            usage_revision=usage_revision,
            derivative_revision=derivative_revision,
        )

    def matches_for_required_terms(
        self,
        required_terms: list[str],
        *,
        excluded_terms: list[str],
        top_k: int = 24,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        """Return a stable union whose candidates collectively cover required terms."""
        if connection is None and self._opens_transactional_material_snapshot():
            with self.repository.transaction() as snapshot:
                return self.matches_for_required_terms(
                    required_terms,
                    excluded_terms=excluded_terms,
                    top_k=top_k,
                    connection=snapshot,
                )
        union: list[dict[str, object]] = []
        seen: set[str] = set()
        for term in required_terms:
            search = self.search(
                {
                    "query": term,
                    "types": ["image_asset", "video_segment"],
                    "top_k": top_k,
                    "filters": {"excluded_terms": excluded_terms},
                },
                connection=connection,
            )
            for item in search["results"]:
                identifier = str(item["id"])
                if identifier not in seen:
                    seen.add(identifier)
                    union.append(item)
                    if len(union) >= top_k:
                        return union
        return union

    def constraint_conflicts(
        self,
        match_ids: list[str],
        *,
        required_terms: list[str],
        excluded_terms: list[str],
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        """Evaluate authoritative brief constraints against explicit user selections."""
        # Explicit references remain resolvable after Archive so an existing
        # project never loses provenance. The presented match carries its review
        # state, allowing callers to ask the user for a replacement deliberately.
        if connection is None and self._opens_transactional_material_snapshot():
            with self.repository.transaction() as snapshot:
                return self.constraint_conflicts(
                    match_ids,
                    required_terms=required_terms,
                    excluded_terms=excluded_terms,
                    connection=snapshot,
                )
        candidates, _heads, _occurrences, _derivative_revision = (
            self._material_snapshot(
                connection=connection,
                include_archived=True,
            )
        )
        return find_constraint_conflicts(
            candidates,
            match_ids,
            required_terms=required_terms,
            excluded_terms=excluded_terms,
        )

    def resolve_matches(
        self,
        match_ids: list[str],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, object]]:
        if connection is None and self._opens_transactional_material_snapshot():
            with self.repository.transaction() as snapshot:
                return self.resolve_matches(match_ids, connection=snapshot)
        candidates, _heads, _occurrences, _derivative_revision = (
            self._material_snapshot(
                connection=connection,
                include_archived=True,
            )
        )
        return present_explicit_matches(candidates, match_ids)
