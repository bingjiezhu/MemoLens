"""Compatibility photo reads with bounded, fail-closed canonical admission."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from memolens_blueprint_authority import validate_exact_managed_schema_manifest
from memolens_contracts import (
    MemoLensError,
    compact_asset,
    parse_tags,
    quality_value,
    query_terms,
    safety_summary,
)
from memolens_ranking import RankedRow, keep_best, lexical_score
from memolens_image_authority import CanonicalImageObservationReader
from memolens_sqlite import ReadOnlyDatabase


_MAX_CANONICAL_AUTHORITY_CANDIDATES = 144
_CANONICAL_SEARCH_RELATIONS = frozenset(
    {
        "assets",
        "asset_sources",
        "database_meta",
        "image_analysis_heads",
        "image_analysis_results",
        "image_index",
        "image_projection_generations",
        "image_projection_manifests",
        "image_projection_read_manifests",
        "image_projection_rows",
        "library_roots",
    }
)


class PhotoIndexReader:
    """Read the photo projection without opening media files."""

    def __init__(
        self, database: ReadOnlyDatabase, library_dir: Path | None
    ) -> None:
        self.database = database
        self.library_dir = library_dir
        self.image_authority = CanonicalImageObservationReader()

    def configure_library(self, library_dir: Path | None) -> None:
        self.library_dir = library_dir

    @staticmethod
    def canonical_search_schema_available(
        connection: sqlite3.Connection,
    ) -> bool:
        try:
            validate_exact_managed_schema_manifest(connection)
        except (MemoLensError, sqlite3.Error):
            return False
        return True

    @staticmethod
    def validate_identity(columns: set[str]) -> None:
        if columns and not {"id", "filename", "relative_path"}.issubset(columns):
            raise MemoLensError(
                "The SQLite image_index table is missing required MemoLens columns.",
                code="database_identity_mismatch",
            )

    def columns_or_error(self, connection: sqlite3.Connection) -> set[str]:
        columns = self.database.columns(connection, "image_index")
        if not columns:
            raise MemoLensError(
                "The SQLite file does not contain the legacy MemoLens image index.",
                code="capability_unavailable",
            )
        return columns

    @staticmethod
    def count(connection: sqlite3.Connection, columns: set[str]) -> int:
        if not columns:
            return 0
        return int(connection.execute("SELECT COUNT(*) FROM image_index").fetchone()[0])

    @staticmethod
    def embedding_backends(
        connection: sqlite3.Connection, columns: set[str]
    ) -> list[dict[str, Any]]:
        if "embedding_backend" not in columns:
            return []
        return [
            {"name": row[0], "count": int(row[1])}
            for row in connection.execute(
                "SELECT embedding_backend, COUNT(*) FROM image_index "
                "GROUP BY embedding_backend ORDER BY COUNT(*) DESC"
            ).fetchall()
        ]

    def search(
        self,
        query: str,
        limit: int,
        *,
        require_canonical_authority: bool,
        require_media_provenance: bool = False,
        include_paths: bool = True,
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            return self.search_in_connection(
                connection,
                query,
                limit,
                require_canonical_authority=require_canonical_authority,
                require_media_provenance=require_media_provenance,
                include_paths=include_paths,
            )

    def search_in_connection(
        self,
        connection: sqlite3.Connection,
        query: str,
        limit: int,
        *,
        require_canonical_authority: bool,
        require_media_provenance: bool = False,
        include_paths: bool = True,
        excluded_asset_sha256: frozenset[str] = frozenset(),
        excluded_asset_ids: frozenset[str] = frozenset(),
    ) -> dict[str, Any]:
        """Search inside a caller-owned snapshot, filtering derivatives pre-rank."""

        columns = self.columns_or_error(connection)
        self.validate_identity(columns)
        if (
            require_canonical_authority
            and not self.canonical_search_schema_available(connection)
        ):
            raise MemoLensError(
                "Canonical image search authority is not available in this index.",
                code="canonical_image_authority_unavailable",
            )
        try:
            select_parts, from_sql, where_sql = self._search_projection(
                connection,
                columns,
                require_media_provenance=require_media_provenance,
                require_canonical_authority=require_canonical_authority,
            )
            (
                scored,
                scanned_count,
                authority_summary,
                excluded_derivative_count,
            ) = self._rank_rows(
                connection,
                connection.execute(
                    f"SELECT {', '.join(select_parts)} {from_sql} {where_sql}"
                ),
                query=query,
                limit=limit,
                require_canonical_authority=require_canonical_authority,
                excluded_asset_sha256=excluded_asset_sha256,
                excluded_asset_ids=excluded_asset_ids,
                return_exclusion_count=True,
            )
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite index could not be searched.",
                code="database_unavailable",
            ) from exc
        scored.sort(key=lambda item: item[:3], reverse=True)
        results: list[dict[str, Any]] = []
        for *_, raw in scored:
            result = compact_asset(
                raw,
                self.library_dir if include_paths else None,
            )
            observation = raw.get("_canonical_image_observation")
            if isinstance(observation, dict):
                # Reuse the verifier's already-bounded, path-free proof object;
                # never manufacture a second image-authority dialect here.
                result["canonical_image_observation"] = observation
            results.append(result)
        if not include_paths:
            for result in results:
                result.pop("absolute_path", None)
                result.pop("path_status", None)
                result.pop("relative_path", None)
        payload = {
            "object": "memolens.search",
            "status": "completed",
            "source": "sqlite_fallback",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "excluded_derivative_count": excluded_derivative_count,
            "safety": safety_summary(),
        }
        if authority_summary is not None:
            payload["canonical_image_authority"] = authority_summary
        return payload

    @staticmethod
    def _select_parts(columns: set[str], *, alias: str = "") -> list[str]:
        desired = [
            "id",
            "sha256",
            "filename",
            "relative_path",
            "taken_at",
            "place_name",
            "country",
            "description",
            "tags_json",
            "combined_text",
            "aesthetic_score",
            "technical_quality_score",
        ]
        prefix = f'{alias}.' if alias else ""
        return [
            f'{prefix}"{name}" AS "{name}"'
            if name in columns
            else f'NULL AS "{name}"'
            for name in desired
        ]

    def _search_projection(
        self,
        connection: sqlite3.Connection,
        image_columns: set[str],
        *,
        require_media_provenance: bool,
        require_canonical_authority: bool,
    ) -> tuple[list[str], str, str]:
        if require_canonical_authority:
            return self._canonical_search_projection(
                connection,
                image_columns,
            )
        asset_columns = self.database.columns(connection, "assets")
        source_columns = self.database.columns(connection, "asset_sources")
        has_asset_identity = {"id", "kind", "sha256"}.issubset(asset_columns)
        has_source_identity = {"id", "asset_id", "relative_path"}.issubset(
            source_columns
        )
        has_filename = bool({"display_filename", "filename"} & source_columns)
        has_availability = bool({"availability", "status"} & source_columns)
        identity_join = None
        if has_asset_identity and "sha256" in image_columns:
            identity_join = "a.sha256=i.sha256"
        elif has_asset_identity and "id" in image_columns:
            identity_join = "a.id=i.id"
        media_join_available = bool(
            identity_join
            and has_source_identity
            and has_filename
            and has_availability
        )
        if not media_join_available:
            if require_media_provenance:
                raise MemoLensError(
                    "Photo results do not yet expose stable asset source provenance.",
                    code="capability_unavailable",
                )
            return (
                self._select_parts(image_columns, alias="i"),
                "FROM image_index i",
                "",
            )

        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        preferred_order = (
            "CASE WHEN src2.is_preferred=1 THEN 0 ELSE 1 END,"
            if "is_preferred" in source_columns
            else ""
        )
        select_parts = self._select_parts(image_columns, alias="i")
        # The source binding, not the legacy image row ID or path, is the timeline
        # provenance authority.
        select_parts.extend(
            [
                "a.id AS asset_id",
                "a.sha256 AS asset_sha256",
                "src.id AS asset_source_id",
                f"src.{availability_column} AS source_availability",
                f"src.{filename_column} AS filename",
            ]
        )
        review_columns = self.database.columns(
            connection, "asset_review_revisions"
        )
        has_reviews = {
            "asset_id",
            "revision",
            "inbox_state",
            "favorite",
            "project_ready",
        }.issubset(review_columns)
        if has_reviews:
            select_parts.extend(
                [
                    "COALESCE(rv.revision,0) AS review_revision",
                    "COALESCE(rv.inbox_state,'inbox') AS inbox_state",
                    "COALESCE(rv.favorite,0) AS favorite",
                    "COALESCE(rv.project_ready,0) AS project_ready",
                ]
            )
            review_join = (
                " LEFT JOIN asset_review_revisions rv ON rv.asset_id=a.id "
                "AND rv.revision=(SELECT MAX(rv2.revision) "
                "FROM asset_review_revisions rv2 WHERE rv2.asset_id=a.id)"
            )
            archived_filter = " AND COALESCE(rv.inbox_state,'inbox')<>'archived'"
        else:
            select_parts.extend(
                [
                    "0 AS review_revision",
                    "'inbox' AS inbox_state",
                    "0 AS favorite",
                    "0 AS project_ready",
                ]
            )
            review_join = ""
            archived_filter = ""
        root_join = ""
        active_root_filter = ""
        if (
            "library_root_id" in source_columns
            and self.database.relation_exists(connection, "library_roots")
            and {"id", "status"}.issubset(
                self.database.columns(connection, "library_roots")
            )
        ):
            active_root_filter = (
                " AND EXISTS (SELECT 1 FROM library_roots root2 "
                "WHERE root2.id=src2.library_root_id AND root2.status='active')"
            )
            root_join = (
                " JOIN library_roots root ON root.id=src.library_root_id "
                "AND root.status='active'"
            )
        from_sql = (
            f"FROM image_index i JOIN assets a ON {identity_join} AND a.kind='image' "
            "JOIN asset_sources src ON src.id=(SELECT src2.id FROM asset_sources src2 "
            f"WHERE src2.asset_id=a.id{active_root_filter} ORDER BY "
            f"CASE WHEN src2.{availability_column}='available' THEN 0 ELSE 1 END,"
            f"{preferred_order}src2.id LIMIT 1)"
            f"{root_join}{review_join}"
        )
        where_sql = (
            f"WHERE src.{availability_column}='available'{archived_filter}"
        )
        return select_parts, from_sql, where_sql

    def _canonical_search_projection(
        self,
        connection: sqlite3.Connection,
        image_columns: set[str],
    ) -> tuple[list[str], str, str]:
        """Select only exact current-result source candidates for verification.

        The joins below are candidate scoping, not authority.  Every selected
        row still has to pass ``CanonicalImageObservationReader`` before any
        summary field is returned.
        """

        present_relations = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type IN ('table','view')"
            ).fetchall()
        }
        if not _CANONICAL_SEARCH_RELATIONS.issubset(present_relations):
            raise MemoLensError(
                "Canonical image search authority is not available in this index.",
                code="canonical_image_authority_unavailable",
            )

        select_parts = self._select_parts(image_columns, alias="i")
        select_parts.extend(
            [
                "a.id AS asset_id",
                "a.sha256 AS asset_sha256",
                "src.id AS asset_source_id",
                "src.availability AS source_availability",
            ]
        )
        review_columns = self.database.columns(
            connection,
            "asset_review_revisions",
        )
        has_reviews = {
            "asset_id",
            "revision",
            "inbox_state",
            "favorite",
            "project_ready",
        }.issubset(review_columns)
        if has_reviews:
            select_parts.extend(
                [
                    "COALESCE(rv.revision,0) AS review_revision",
                    "COALESCE(rv.inbox_state,'inbox') AS inbox_state",
                    "COALESCE(rv.favorite,0) AS favorite",
                    "COALESCE(rv.project_ready,0) AS project_ready",
                ]
            )
            review_join = (
                " LEFT JOIN asset_review_revisions rv ON rv.asset_id=a.id "
                "AND rv.revision=(SELECT MAX(rv2.revision) "
                "FROM asset_review_revisions rv2 WHERE rv2.asset_id=a.id)"
            )
            archived_filter = " AND COALESCE(rv.inbox_state,'inbox')<>'archived'"
        else:
            select_parts.extend(
                [
                    "0 AS review_revision",
                    "'inbox' AS inbox_state",
                    "0 AS favorite",
                    "0 AS project_ready",
                ]
            )
            review_join = ""
            archived_filter = ""

        # Scope the candidate pool to the one clean active projection before
        # lexical scoring and the bounded verifier window.  The joins are not
        # authority: ``CanonicalImageObservationReader`` below still verifies
        # the complete generation/receipt/source binding.  They only prevent
        # stale or unmanaged high-scoring compatibility rows from consuming
        # the bounded candidate budget ahead of canonical rows.
        from_sql = (
            "FROM database_meta meta "
            "JOIN image_projection_generations generation "
            "ON generation.database_uuid=meta.database_uuid "
            "AND generation.projection_contract='legacy-image-index-shadow/v1' "
            "AND generation.status='complete' AND generation.is_active=1 "
            "JOIN image_projection_manifests manifest "
            "ON manifest.generation_id=generation.id "
            "AND manifest.database_uuid=generation.database_uuid "
            "AND manifest.canonical_high_water_position="
            "generation.canonical_high_water_position "
            "AND manifest.compiler_id=generation.compiler_id "
            "AND manifest.projector_version=generation.projector_version "
            "AND manifest.eligible_count=manifest.projected_count "
            "AND manifest.missing_count=0 AND manifest.unexpected_count=0 "
            "AND manifest.mismatched_count=0 AND manifest.blocked_count=0 "
            "JOIN image_projection_read_manifests read_manifest "
            "ON read_manifest.generation_id=generation.id "
            "AND read_manifest.database_uuid=generation.database_uuid "
            "AND read_manifest.canonical_high_water_position="
            "generation.canonical_high_water_position "
            "AND read_manifest.compiler_id=generation.compiler_id "
            "AND read_manifest.projector_version=generation.projector_version "
            "AND read_manifest.eligible_count=read_manifest.projected_count "
            "AND read_manifest.missing_count=0 "
            "AND read_manifest.unexpected_count=0 "
            "AND read_manifest.mismatched_count=0 "
            "AND read_manifest.blocked_count=0 "
            "AND read_manifest.manifest_json=manifest.manifest_json "
            "AND read_manifest.manifest_sha256=manifest.manifest_sha256 "
            "JOIN image_projection_rows projected "
            "ON projected.generation_id=generation.id "
            "JOIN image_index i ON i.id=projected.asset_id "
            "JOIN assets a ON a.id=i.id AND a.sha256=i.sha256 AND a.kind='image' "
            "JOIN image_analysis_heads head ON head.asset_id=projected.asset_id "
            "AND head.analysis_run_id=projected.analysis_run_id "
            "AND head.revision=projected.revision "
            "AND head.content_sha256=projected.content_sha256 "
            "JOIN image_analysis_results result "
            "ON result.asset_id=head.asset_id "
            "AND result.analysis_run_id=head.analysis_run_id "
            "AND result.revision=head.revision "
            "AND result.content_sha256=head.content_sha256 "
            "AND result.source_binding_sha256=projected.source_binding_sha256 "
            "JOIN asset_sources src ON src.id=result.source_id "
            "AND src.id=projected.source_id AND src.asset_id=a.id "
            "JOIN library_roots root ON root.id=src.library_root_id "
            f"AND root.status='active'{review_join}"
        )
        where_sql = (
            f"WHERE meta.singleton=1 AND src.availability='available'{archived_filter}"
        )
        return select_parts, from_sql, where_sql

    def _rank_rows(
        self,
        connection: sqlite3.Connection,
        cursor: sqlite3.Cursor,
        *,
        query: str,
        limit: int,
        require_canonical_authority: bool,
        excluded_asset_sha256: frozenset[str] = frozenset(),
        excluded_asset_ids: frozenset[str] = frozenset(),
        return_exclusion_count: bool = False,
    ) -> (
        tuple[list[RankedRow], int, dict[str, Any] | None]
        | tuple[list[RankedRow], int, dict[str, Any] | None, int]
    ):
        phrase = query.casefold()
        terms = query_terms(query)
        candidate_limit = (
            min(
                max(limit * 4, 36),
                _MAX_CANONICAL_AUTHORITY_CANDIDATES,
            )
            if require_canonical_authority
            else limit
        )
        candidates: list[RankedRow] = []
        scanned_count = 0
        matched_count = 0
        excluded_derivative_count = 0
        sequence = 0
        while rows := cursor.fetchmany(512):
            for row in rows:
                scanned_count += 1
                raw = dict(row)
                if raw.get("asset_sha256") in excluded_asset_sha256:
                    excluded_derivative_count += 1
                    continue
                if raw.get("asset_id") in excluded_asset_ids:
                    continue
                scored_row = self._score_row(raw, query, phrase, terms)
                if scored_row is None:
                    continue
                matched_count += 1
                score, prepared = scored_row
                candidate = (
                    score,
                    str(prepared.get("taken_at") or ""),
                    sequence,
                    prepared,
                )
                sequence += 1
                keep_best(candidates, candidate, candidate_limit)
        if not require_canonical_authority:
            base = (candidates, scanned_count, None)
            return (
                (*base, excluded_derivative_count)
                if return_exclusion_count
                else base
            )

        candidates.sort(key=lambda item: item[:3], reverse=True)
        admitted: list[RankedRow] = []
        reason_counts: dict[str, int] = {}
        checked_count = 0
        for candidate in candidates:
            checked_count += 1
            raw = candidate[-1]
            observation = self._canonical_observation(connection, raw)
            if observation.get("status") != "current":
                reason = str(
                    observation.get("reason_code")
                    or "canonical_image_authority_unavailable"
                )
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                continue
            raw["_canonical_image_observation"] = observation
            admitted.append(candidate)
            if len(admitted) >= limit:
                break
        base = (
            admitted,
            scanned_count,
            {
                "required": True,
                "authority": "canonical_image_analysis",
                "admission": "verified_current_only",
                "matched_candidate_count": matched_count,
                "evaluated_candidate_count": checked_count,
                "admitted_count": len(admitted),
                "excluded_count": sum(reason_counts.values()),
                "exclusion_reason_counts": [
                    {"reason_code": reason, "count": count}
                    for reason, count in sorted(reason_counts.items())
                ],
                "candidate_pool_truncated": matched_count > len(candidates),
                "evaluation_short_circuited": checked_count < len(candidates),
                "max_evaluated_candidates": _MAX_CANONICAL_AUTHORITY_CANDIDATES,
            },
        )
        return (
            (*base, excluded_derivative_count)
            if return_exclusion_count
            else base
        )

    def _canonical_observation(
        self,
        connection: sqlite3.Connection,
        raw: dict[str, Any],
    ) -> dict[str, object]:
        asset_id = raw.get("asset_id")
        if (
            not isinstance(asset_id, str)
            or raw.get("id") != asset_id
            or raw.get("sha256") != raw.get("asset_sha256")
        ):
            return {
                "status": "unavailable",
                "reason_code": "canonical_image_search_identity_mismatch",
            }
        observation = self.image_authority.read(
            connection,
            raw={"id": asset_id, "kind": "image"},
        )
        if not isinstance(observation, dict):
            return {
                "status": "unavailable",
                "reason_code": "canonical_image_authority_unavailable",
            }
        return observation

    @staticmethod
    def _score_row(
        raw: dict[str, Any], query: str, phrase: str, terms: list[str]
    ) -> tuple[float, dict[str, Any]] | None:
        tags = parse_tags(raw.get("tags_json"))
        fields = {
            "filename": str(raw.get("filename") or "").casefold(),
            "path": str(raw.get("relative_path") or "").casefold(),
            "place": " ".join(
                str(raw.get(key) or "") for key in ("place_name", "country")
            ).casefold(),
            "description": str(raw.get("description") or "").casefold(),
            "tags": " ".join(tags).casefold(),
            "combined": str(raw.get("combined_text") or "").casefold(),
        }
        score, matched = lexical_score(
            fields,
            query=query,
            phrase=phrase,
            terms=terms,
            weights={
                "tags": 3.0,
                "place": 2.5,
                "description": 2.0,
                "filename|path": 1.5,
                "combined": 1.0,
            },
        )
        if score <= 0:
            return None
        score += quality_value(
            raw.get("aesthetic_score"), raw.get("technical_quality_score")
        ) * 0.25
        raw["tags"] = tags
        raw["score"] = round(score, 4)
        raw["matched_terms"] = list(dict.fromkeys(matched))
        return score, raw
