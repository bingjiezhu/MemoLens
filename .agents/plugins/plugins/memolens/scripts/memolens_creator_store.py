"""Read-only Creator Memory and Media Inbox projections for Codex."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from memolens_contracts import MemoLensError, encode_cursor, safety_summary
from memolens_sqlite import ReadOnlyDatabase


_PROFILE_FIELDS = {
    "platform",
    "audience",
    "default_duration_ms",
    "duration_ms",
    "aspect_ratio",
    "tone",
    "pace",
    "narrative_arc",
    "must_include",
    "must_exclude",
}
_PROFILE_TEXT_FIELDS = {
    "platform",
    "audience",
    "aspect_ratio",
    "tone",
    "pace",
    "narrative_arc",
}
_PROFILE_DURATION_FIELDS = {"default_duration_ms", "duration_ms"}
_PROFILE_LIST_FIELDS = {"must_include", "must_exclude"}
_PROFILE_SOURCES = {"user_edit", "confirmed_suggestion", "reset"}
_REVIEW_STATES = {"inbox", "kept", "archived"}


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _json_value(raw: Any, fallback: Any) -> Any:
    if not isinstance(raw, str) or not raw or len(raw) > 1_000_000:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _profile_value(field: str, value: Any) -> Any:
    if field in _PROFILE_TEXT_FIELDS and isinstance(value, str):
        return value[:1000]
    if (
        field in _PROFILE_DURATION_FIELDS
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    ):
        return value
    if field in _PROFILE_LIST_FIELDS and isinstance(value, list):
        return [
            item[:200]
            for item in value[:50]
            if isinstance(item, str) and item.strip()
        ]
    return None


def _safe_profile(raw: Any) -> dict[str, Any]:
    parsed = _json_value(raw, {})
    if not isinstance(parsed, dict):
        return {}
    result: dict[str, Any] = {}
    for field in _PROFILE_FIELDS:
        if field not in parsed:
            continue
        value = _profile_value(field, parsed[field])
        if value is not None:
            result[field] = value
    return result


def _evidence_summary(raw: Any) -> dict[str, Any]:
    parsed = _json_value(raw, [])
    items = parsed if isinstance(parsed, list) else []
    by_kind: dict[str, int] = {}
    project_references = 0
    asset_references = 0
    for item in items[:10_000]:
        if isinstance(item, dict):
            kind = next(
                (
                    str(item[key]).strip()
                    for key in ("kind", "type", "source")
                    if isinstance(item.get(key), str) and str(item[key]).strip()
                ),
                "confirmed_evidence",
            )
            if item.get("project_id") is not None:
                project_references += 1
            if item.get("asset_id") is not None:
                asset_references += 1
        else:
            kind = "confirmed_evidence"
        normalized_kind = kind[:80]
        by_kind[normalized_kind] = by_kind.get(normalized_kind, 0) + 1
    return {
        "count": len(items),
        "by_kind": dict(sorted(by_kind.items())),
        "project_reference_count": project_references,
        "asset_reference_count": asset_references,
        "raw_references_included": False,
    }


def _learning_policy() -> dict[str, Any]:
    return {
        "policy": "confirmed_only",
        "confirmed_only": True,
        "hidden_inference": False,
        "accepted_sources": sorted(_PROFILE_SOURCES),
    }


class CreatorMemoryReader:
    """Project current confirmed creator context and non-destructive inbox state."""

    def __init__(self, database: ReadOnlyDatabase) -> None:
        self.database = database

    def schema_capabilities(self, connection: sqlite3.Connection) -> dict[str, bool]:
        profile_columns = self.database.columns(
            connection, "creator_profile_revisions"
        )
        review_columns = self.database.columns(
            connection, "asset_review_revisions"
        )
        return {
            "creator_context_available": {
                "profile_id",
                "revision",
                "profile_json",
                "content_sha256",
                "evidence_json",
                "source",
                "created_at",
            }.issubset(profile_columns),
            "inbox_available": {
                "asset_id",
                "revision",
                "inbox_state",
                "favorite",
                "project_ready",
                "note",
            }.issubset(review_columns),
        }

    def creator_context(self) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            capabilities = self.schema_capabilities(connection)
            if not capabilities["creator_context_available"]:
                return self._empty_creator_context("capability_unavailable", False)
            try:
                row = connection.execute(
                    "SELECT profile_id,revision,profile_json,content_sha256,"
                    "evidence_json,source,created_at "
                    "FROM creator_profile_revisions WHERE profile_id='default' "
                    "ORDER BY revision DESC,created_at DESC LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Creator Memory could not be read from the MemoLens index.",
                    code="database_unavailable",
                ) from exc
        if row is None:
            return self._empty_creator_context("completed", True)
        source = str(row["source"] or "")
        if source not in _PROFILE_SOURCES:
            raise MemoLensError(
                "Creator Memory contains an unconfirmed profile source.",
                code="database_identity_mismatch",
            )
        digest = str(row["content_sha256"] or "")
        if not _valid_sha256(digest):
            raise MemoLensError(
                "Creator Memory content provenance is invalid.",
                code="database_identity_mismatch",
            )
        return {
            "object": "memolens.creator_context",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "capability_available": True,
            "profile_id": str(row["profile_id"]),
            "profile_revision": int(row["revision"]),
            "profile_content_sha256": digest,
            "profile_source": source,
            "profile": _safe_profile(row["profile_json"]),
            "evidence_summary": _evidence_summary(row["evidence_json"]),
            "learning": _learning_policy(),
            "write_boundary": (
                "Creator Memory is read-only here. Edit, confirm, or reset it in the "
                "MemoLens app."
            ),
            "safety": safety_summary(),
        }

    @staticmethod
    def _empty_creator_context(
        status: str, capability_available: bool
    ) -> dict[str, Any]:
        return {
            "object": "memolens.creator_context",
            "schema_version": "1",
            "status": status,
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "capability_available": capability_available,
            "profile_id": None,
            "profile_revision": 0,
            "profile_content_sha256": None,
            "profile_source": None,
            "profile": {},
            "evidence_summary": {
                "count": 0,
                "by_kind": {},
                "project_reference_count": 0,
                "asset_reference_count": 0,
                "raw_references_included": False,
            },
            "learning": _learning_policy(),
            "write_boundary": (
                "Creator Memory is read-only here. Edit, confirm, or reset it in the "
                "MemoLens app."
            ),
            "safety": safety_summary(),
        }

    def inbox_list(
        self,
        *,
        state: str,
        kinds: list[str],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            capabilities = self.schema_capabilities(connection)
            asset_columns = self.database.columns(connection, "assets")
            source_columns = self.database.columns(connection, "asset_sources")
            media_available = {
                "id",
                "kind",
                "sha256",
            }.issubset(asset_columns) and {
                "id",
                "asset_id",
                "relative_path",
            }.issubset(source_columns)
            if not capabilities["inbox_available"] or not media_available:
                return self._empty_inbox(state, kinds)

            filename_column = (
                "display_filename"
                if "display_filename" in source_columns
                else "filename"
            )
            availability_column = (
                "availability" if "availability" in source_columns else "status"
            )
            preferred_order = (
                "CASE WHEN src2.is_preferred=1 THEN 0 ELSE 1 END,"
                if "is_preferred" in source_columns
                else ""
            )
            where = [
                f"a.kind IN ({', '.join('?' for _kind in kinds)})",
                f"src.{availability_column}='available'",
            ]
            params: list[Any] = list(kinds)
            if state != "all":
                where.append("COALESCE(rv.inbox_state,'inbox')=?")
                params.append(state)
            if cursor is not None:
                where.append("a.id>?")
                params.append(cursor)
            if (
                "library_root_id" in source_columns
                and self.database.relation_exists(connection, "library_roots")
                and {"id", "status"}.issubset(
                    self.database.columns(connection, "library_roots")
                )
            ):
                root_join = (
                    " JOIN library_roots root ON root.id=src.library_root_id "
                    "AND root.status='active'"
                )
            else:
                root_join = ""
            params.append(limit + 1)
            select_optional = {
                field: f'a."{field}"' if field in asset_columns else "NULL"
                for field in (
                    "captured_at",
                    "duration_ms",
                    "width",
                    "height",
                )
            }
            try:
                rows = connection.execute(
                    "SELECT a.id AS asset_id,a.kind AS media_kind,a.sha256 AS asset_sha256,"
                    f"src.id AS asset_source_id,src.{filename_column} AS filename,"
                    f"src.{availability_column} AS source_availability,"
                    f"{select_optional['captured_at']} AS captured_at,"
                    f"{select_optional['duration_ms']} AS duration_ms,"
                    f"{select_optional['width']} AS width,"
                    f"{select_optional['height']} AS height,"
                    "COALESCE(rv.revision,0) AS review_revision,"
                    "COALESCE(rv.inbox_state,'inbox') AS inbox_state,"
                    "COALESCE(rv.favorite,0) AS favorite,"
                    "COALESCE(rv.project_ready,0) AS project_ready,"
                    "CASE WHEN rv.note IS NULL OR trim(rv.note)='' THEN 0 ELSE 1 END AS has_note "
                    "FROM assets a "
                    "JOIN asset_sources src ON src.id=(SELECT src2.id FROM asset_sources src2 "
                    "WHERE src2.asset_id=a.id ORDER BY "
                    f"CASE WHEN src2.{availability_column}='available' THEN 0 ELSE 1 END,"
                    f"{preferred_order}src2.id LIMIT 1)"
                    f"{root_join} "
                    "LEFT JOIN asset_review_revisions rv ON rv.asset_id=a.id "
                    "AND rv.revision=(SELECT MAX(rv2.revision) FROM asset_review_revisions rv2 "
                    "WHERE rv2.asset_id=a.id) "
                    f"WHERE {' AND '.join(where)} ORDER BY a.id ASC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens Inbox could not be read.",
                    code="database_unavailable",
                ) from exc
        has_more = len(rows) > limit
        selected = rows[:limit]
        assets = [self._inbox_asset(dict(row)) for row in selected]
        return {
            "object": "memolens.inbox_list",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "capability_available": True,
            "state": state,
            "kinds": kinds,
            "result_count": len(assets),
            "assets": assets,
            "next_cursor": (
                encode_cursor(str(selected[-1]["asset_id"]))
                if has_more and selected
                else None
            ),
            "review_boundary": (
                "Suggestions are not decisions. Confirm Keep, Archive, Favorite, or Ready "
                "in the MemoLens app; original files remain unchanged."
            ),
            "safety": safety_summary(),
        }

    @staticmethod
    def _inbox_asset(row: dict[str, Any]) -> dict[str, Any]:
        state = str(row.get("inbox_state") or "inbox")
        if state not in _REVIEW_STATES:
            state = "inbox"
        digest = row.get("asset_sha256")
        if not _valid_sha256(digest):
            raise MemoLensError(
                "Media Inbox asset provenance is invalid.",
                code="database_identity_mismatch",
            )
        return {
            "asset_id": str(row["asset_id"]),
            "media_kind": str(row["media_kind"]),
            "filename": str(row.get("filename") or ""),
            "captured_at": row.get("captured_at"),
            "dimensions": {
                "width": row.get("width"),
                "height": row.get("height"),
            },
            "timing": {"duration_ms": row.get("duration_ms")},
            "review": {
                "revision": int(row.get("review_revision") or 0),
                "inbox_state": state,
                "favorite": bool(row.get("favorite")),
                "project_ready": bool(row.get("project_ready")),
                "has_note": bool(row.get("has_note")),
            },
            "provenance": {
                "asset_source_id": str(row["asset_source_id"]),
                "asset_sha256": str(digest),
                "source_availability": str(row["source_availability"]),
            },
        }

    @staticmethod
    def _empty_inbox(state: str, kinds: list[str]) -> dict[str, Any]:
        return {
            "object": "memolens.inbox_list",
            "schema_version": "1",
            "status": "capability_unavailable",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "capability_available": False,
            "state": state,
            "kinds": kinds,
            "result_count": 0,
            "assets": [],
            "next_cursor": None,
            "review_boundary": (
                "Suggestions are not decisions. Confirm Keep, Archive, Favorite, or Ready "
                "in the MemoLens app; original files remain unchanged."
            ),
            "safety": safety_summary(),
        }
