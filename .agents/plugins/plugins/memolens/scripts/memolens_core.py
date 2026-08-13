#!/usr/bin/env python3
"""Read-only MemoLens client with safe-default SQLite access.

This module deliberately uses only the Python standard library.  It never
opens photo files and never opens the index database in writable mode.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from memolens_atlas_presenter import present_cleanup, present_memories
from memolens_api_client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    MAX_RESPONSE_BYTES,
    LocalApiClient,
    clamp_timeout,
    validate_base_url as _validate_base_url,
)
from memolens_contracts import (
    MemoLensError,
    bounded_int as _bounded_int,
    capabilities as _capabilities,
    compact_asset as _compact_asset,
    decode_cursor as _decode_cursor,
    json_ready,
    media_kinds as _media_kinds,
    safety_summary as _safety_summary,
    timeline_safety_summary as _timeline_safety_summary,
)
from memolens_read_store import ReadOnlyMemoLensStore

from memolens_timeline import (
    TimelineInputError,
    draft_timeline,
    revise_timeline_draft,
    validate_timeline,
)

TRUST_LOCAL_API_ENV = "MEMOLENS_PLUGIN_TRUST_LOCAL_API"


def _clean_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return Path(value).expanduser().resolve()


def _state_dir_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = _clean_path(os.getenv("MEMOLENS_APP_STATE_DIR"))
    if configured:
        candidates.append(configured)

    if sys.platform == "darwin":
        candidates.append((Path.home() / "Library/Application Support" / "MemoLens").resolve())
    elif os.name == "nt":
        appdata = _clean_path(os.getenv("APPDATA"))
        candidates.append(
            ((appdata or Path.home() / "AppData/Roaming") / "MemoLens").resolve()
        )
    else:
        xdg_state = _clean_path(os.getenv("XDG_STATE_HOME"))
        candidates.append(
            ((xdg_state or Path.home() / ".local/state") / "MemoLens").resolve()
        )
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            unique.append(candidate)
            seen.add(key)
    return unique


def _persisted_settings() -> dict[str, Any]:
    for state_dir in _state_dir_candidates():
        settings_path = state_dir / "backend-settings.json"
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            return payload
    return {}


def resolve_local_paths(
    *,
    db_path: str | os.PathLike[str] | None = None,
    library_dir: str | os.PathLike[str] | None = None,
) -> tuple[Path | None, Path | None]:
    """Resolve explicit/env/persisted paths without searching photo folders."""

    settings = _persisted_settings()
    db = _clean_path(db_path) or _clean_path(os.getenv("MEMOLENS_DB_PATH"))
    library = _clean_path(library_dir) or _clean_path(os.getenv("MEMOLENS_LIBRARY_DIR"))

    if db is None and isinstance(settings.get("db_path"), str):
        db = _clean_path(settings["db_path"])
    if library is None and isinstance(settings.get("image_library_dir"), str):
        library = _clean_path(settings["image_library_dir"])

    if db is None:
        for state_dir in _state_dir_candidates():
            candidate = state_dir / "storage" / "photo_index.db"
            if candidate.is_file():
                db = candidate.resolve()
                break
    return db, library


def validate_base_url(raw_url: str) -> str:
    """Compatibility facade around the loopback transport validator."""

    return _validate_base_url(raw_url, resolver=socket.getaddrinfo)


def _local_api_opted_in() -> bool:
    """Require the exact documented opt-in; truthy aliases are not accepted."""

    return os.getenv(TRUST_LOCAL_API_ENV, "").strip() == "1"


class MemoLensGateway:
    """Read-only gateway used by both the CLI and MCP server."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        db_path: str | os.PathLike[str] | None = None,
        library_dir: str | os.PathLike[str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.trust_local_api = _local_api_opted_in()
        configured_base_url = base_url or os.getenv(
            "MEMOLENS_BASE_URL", DEFAULT_BASE_URL
        )
        # A configured URL is irrelevant in safe-default mode. Avoid even DNS
        # resolution until the user has explicitly opted into the unauthenticated
        # loopback API trust boundary.
        self.db_path, self.library_dir = resolve_local_paths(
            db_path=db_path, library_dir=library_dir
        )
        self.timeout = clamp_timeout(timeout)
        self._api = (
            LocalApiClient(
                configured_base_url,
                timeout=self.timeout,
                resolver=socket.getaddrinfo,
            )
            if self.trust_local_api
            else None
        )
        self.base_url = self._api.base_url if self._api else DEFAULT_BASE_URL
        self._store = ReadOnlyMemoLensStore(self.db_path, self.library_dir)

    @property
    def _opener(self):  # noqa: ANN201
        """Compatibility hook for transport-focused diagnostics and tests."""

        return self._api.opener if self._api else None

    @_opener.setter
    def _opener(self, opener: Any) -> None:
        self._require_local_api_trust()
        assert self._api is not None
        self._api.opener = opener

    def _require_local_api_trust(self) -> None:
        if not self.trust_local_api:
            raise MemoLensError(
                "Local API access is disabled by default because a loopback service "
                "cannot be authenticated. To accept that risk, set "
                f"{TRUST_LOCAL_API_ENV}=1 in the environment that starts Codex, then "
                "restart Codex.",
                code="local_api_not_trusted",
            )

    def _local_api_summary(self, **details: Any) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "enabled": self.trust_local_api,
            "trusted_by_user": self.trust_local_api,
            "authenticated": False,
            "checked": False,
            "opt_in_environment": TRUST_LOCAL_API_ENV,
            "warning": (
                "Loopback API identity is not authenticated; another local process "
                "could impersonate MemoLens."
            ),
        }
        if self.trust_local_api:
            summary["base_url"] = self.base_url
        else:
            summary["opt_in_value"] = "1"
        summary.update(details)
        return summary

    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        verify_identity: bool = True,
    ) -> dict[str, Any]:
        self._require_local_api_trust()
        assert self._api is not None
        if verify_identity:
            self.health()
        return self._api.request_json(
            path,
            method=method,
            body=body,
            verify_identity=False,
        )

    def health(self) -> dict[str, Any]:
        self._require_local_api_trust()
        assert self._api is not None
        payload = self._api.health()
        self.db_path = self._api.db_path
        self.library_dir = self._api.library_dir
        self._store.configure(db_path=self.db_path, library_dir=self.library_dir)
        return payload

    def status(self) -> dict[str, Any]:
        if not self.trust_local_api:
            try:
                database = self._sqlite_status()
            except MemoLensError as database_error:
                return {
                    "object": "memolens.status",
                    "status": "unavailable",
                    "source": "none",
                    "mode": "safe_default_read_only",
                    "local_api": self._local_api_summary(available=None),
                    "database": {
                        "available": False,
                        "error": str(database_error),
                        "error_code": database_error.code,
                    },
                    "capabilities": _capabilities(
                        None, legacy_search=False, local_api_reads=False
                    ),
                    "warnings": [
                        "Local API access is disabled and no readable SQLite index was found."
                    ],
                    "safety": _safety_summary(),
                }
            return {
                "object": "memolens.status",
                "status": "ok",
                "source": "sqlite_read_only",
                "mode": "safe_default_read_only",
                "local_api": self._local_api_summary(available=None),
                "database": database,
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                ),
                "warnings": [
                    "Local API access is disabled by default; confirmed Creator Memory, Media Inbox, search, and timeline reads use SQLite read-only access."
                ],
                "safety": _safety_summary(),
            }

        try:
            self.health()
        except MemoLensError as service_error:
            try:
                database = self._sqlite_status()
            except MemoLensError as database_error:
                return {
                    "object": "memolens.status",
                    "status": "unavailable",
                    "source": "none",
                    "mode": "opt_in_local_api",
                    "local_api": self._local_api_summary(
                        checked=True,
                        available=False,
                        error=str(service_error),
                        error_code=service_error.code,
                    ),
                    "database": {
                        "available": False,
                        "error": str(database_error),
                        "error_code": database_error.code,
                    },
                    "capabilities": _capabilities(
                        None, legacy_search=False, local_api_reads=False
                    ),
                    "safety": _safety_summary(),
                }
            return {
                "object": "memolens.status",
                "status": "degraded",
                "source": "sqlite_fallback",
                "mode": "opt_in_local_api",
                "local_api": self._local_api_summary(
                    checked=True,
                    available=False,
                    error=str(service_error),
                    error_code=service_error.code,
                ),
                "database": database,
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                ),
                "warnings": [
                    "The MemoLens service is offline; confirmed Creator Memory, Media Inbox, and deterministic media reads remain available through SQLite when indexed."
                ],
                "safety": _safety_summary(),
            }

        assert self._api is not None
        settings = self._api.settings_cache or {}
        effective = settings.get("effective") if isinstance(settings, dict) else {}
        if not isinstance(effective, dict):
            effective = {}
        try:
            database = self._sqlite_status()
        except MemoLensError as database_error:
            database = {
                "available": False,
                "error": str(database_error),
                "error_code": database_error.code,
            }
        database["index_stats"] = settings.get("index_stats")
        database["embedding_backend"] = effective.get("embedding_backend")
        return {
            "object": "memolens.status",
            "status": "ok",
            "source": "local_api",
            "mode": "opt_in_local_api",
            "local_api": self._local_api_summary(
                checked=True,
                available=True,
                identity_verified=True,
            ),
            "database": database,
            "profiles": {
                "vision": effective.get("vision_profile_name"),
                "query": effective.get("query_profile_name"),
            },
            "capabilities": _capabilities(
                database,
                legacy_search=True,
                local_api_reads=True,
            ),
            "write_boundary": (
                "The unauthenticated local-API opt-in grants read features only. "
                "Timeline persistence, rendering, and export are not exposed."
            ),
            "safety": _safety_summary(),
        }

    def search(self, query: str, *, limit: int = 12) -> dict[str, Any]:
        normalized_query = query.strip()
        if not normalized_query:
            raise MemoLensError("Search query cannot be empty.", code="invalid_argument")
        normalized_limit = _bounded_int(limit, minimum=1, maximum=36, field="limit")
        if not self.trust_local_api:
            result = self._sqlite_search(normalized_query, normalized_limit)
            result.update(
                {
                    "source": "sqlite_read_only",
                    "mode": "safe_default_read_only",
                    "local_api": self._local_api_summary(available=None),
                    "warnings": [
                        "Local API access is disabled by default; deterministic lexical SQLite ranking was used."
                    ],
                }
            )
            return result
        try:
            payload = self._request_json(
                "/v1/retrieval/query",
                method="POST",
                body={
                    "text": normalized_query,
                    "top_k": normalized_limit,
                    "include_copy": False,
                },
            )
        except MemoLensError as service_error:
            result = self._sqlite_search(normalized_query, normalized_limit)
            result.update(
                {
                    "mode": "opt_in_local_api",
                    "local_api": self._local_api_summary(
                        checked=True,
                        available=False,
                        error=str(service_error),
                        error_code=service_error.code,
                    ),
                    "warnings": [
                        "The MemoLens service was unavailable; deterministic lexical SQLite ranking was used.",
                        str(service_error),
                    ],
                }
            )
            return result
        if payload.get("object") != "retrieval.query":
            raise MemoLensError(
                "MemoLens retrieval returned an unexpected object type.",
                code="invalid_response",
            )
        raw_results = payload.get("data")
        if not isinstance(raw_results, list):
            raise MemoLensError(
                "MemoLens retrieval did not return a result list.",
                code="invalid_response",
            )
        results = [
            _compact_asset(item, self.library_dir)
            for item in raw_results[:normalized_limit]
            if isinstance(item, dict)
        ]
        return {
            "object": "memolens.search",
            "status": "completed",
            "source": "local_api",
            "mode": "opt_in_local_api",
            "local_api": self._local_api_summary(
                checked=True, available=True, identity_verified=True
            ),
            "query": normalized_query,
            "result_count": len(results),
            "results": results,
            "safety": _safety_summary(),
        }

    def memories(self, *, query: str | None = None, limit: int = 8) -> dict[str, Any]:
        normalized_limit = _bounded_int(limit, minimum=1, maximum=24, field="limit")
        params: dict[str, Any] = {"lens": "story", "limit": normalized_limit}
        if query and query.strip():
            params["query"] = query.strip()
        payload = self._request_json(f"/v1/atlas/workbench?{urlencode(params)}")
        return present_memories(
            payload,
            library_dir=self.library_dir,
            query=query,
            limit=normalized_limit,
            local_api=self._local_api_summary(
                checked=True,
                available=True,
                identity_verified=True,
            ),
        )

    def cleanup(self) -> dict[str, Any]:
        return present_cleanup(
            self._request_json("/v1/atlas/cleanup"),
            library_dir=self.library_dir,
            local_api=self._local_api_summary(
                checked=True,
                available=True,
                identity_verified=True,
            ),
        )

    def video_search(self, query: str, *, limit: int = 12) -> dict[str, Any]:
        """Search current video-segment analysis through read-only SQLite only."""

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MemoLensError("Search query cannot be empty.", code="invalid_argument")
        normalized_limit = _bounded_int(limit, minimum=1, maximum=36, field="limit")
        result = self._sqlite_video_search(normalized_query, normalized_limit)
        result.update(
            {
                "source": "sqlite_read_only",
                "mode": "safe_default_read_only",
                "local_api_used": False,
            }
        )
        return result

    def mixed_search(self, query: str, *, limit: int = 12) -> dict[str, Any]:
        """Search photos and current video segments with one deterministic ranking."""

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MemoLensError("Search query cannot be empty.", code="invalid_argument")
        normalized_limit = _bounded_int(limit, minimum=1, maximum=36, field="limit")

        branches: list[tuple[str, dict[str, Any]]] = []
        branch_errors: list[dict[str, str]] = []
        for kind, searcher in (
            ("image", self._sqlite_mixed_image_search),
            ("video_segment", self.video_search),
        ):
            try:
                branches.append(
                    (kind, searcher(normalized_query, limit=normalized_limit))
                )
            except MemoLensError as exc:
                branch_errors.append(
                    {"result_type": kind, "code": exc.code, "message": str(exc)}
                )

        if not branches:
            raise MemoLensError(
                "Neither the photo index nor the current video-segment index could be searched.",
                code="search_unavailable",
            )

        fused: list[tuple[float, int, str, dict[str, Any]]] = []
        kind_order = {"image": 0, "video_segment": 1}
        for kind, payload in branches:
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                continue
            for rank, raw in enumerate(raw_results, start=1):
                if not isinstance(raw, dict):
                    continue
                # Reciprocal-rank fusion avoids comparing unrelated backend score scales.
                fused_score = 1.0 / (60.0 + rank)
                item = dict(raw)
                item["result_type"] = kind
                item["media_kind"] = "image" if kind == "image" else "video"
                item["rank_score"] = round(fused_score, 8)
                stable_id = str(
                    item.get("segment_id")
                    or item.get("asset_id")
                    or item.get("id")
                    or ""
                )
                fused.append(
                    (fused_score, kind_order[kind], stable_id, item)
                )

        fused.sort(key=lambda value: (-value[0], value[1], value[2]))
        results = [item for _score, _kind, _id, item in fused[:normalized_limit]]
        return {
            "object": "memolens.mixed_search",
            "schema_version": "1",
            "status": "completed",
            "source": "read_only_federated",
            "ranking": "reciprocal_rank_fusion",
            "query": normalized_query,
            "result_count": len(results),
            "results": results,
            "searched_result_types": [kind for kind, _payload in branches],
            "branch_errors": branch_errors,
            "safety": _safety_summary(),
        }

    def media_list(
        self,
        *,
        kinds: list[str] | None = None,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = _bounded_int(limit, minimum=1, maximum=100, field="limit")
        normalized_kinds = _media_kinds(kinds)
        return self._sqlite_media_list(
            kinds=normalized_kinds,
            limit=normalized_limit,
            cursor=_decode_cursor(cursor),
        )

    def media_get(self, asset_id: str) -> dict[str, Any]:
        normalized_id = str(asset_id or "").strip()
        if not normalized_id or len(normalized_id) > 200:
            raise MemoLensError("asset_id is invalid.", code="invalid_argument")
        return self._sqlite_media_get(normalized_id)

    def creator_context(self) -> dict[str, Any]:
        """Read only the latest confirmed creator profile revision."""

        return self._store.creator_context()

    def inbox_list(
        self,
        *,
        state: str = "inbox",
        kinds: list[str] | None = None,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_state = str(state or "inbox").strip().casefold()
        if normalized_state not in {"inbox", "kept", "archived", "all"}:
            raise MemoLensError(
                "state must be inbox, kept, archived, or all.",
                code="invalid_argument",
            )
        return self._store.inbox_list(
            state=normalized_state,
            kinds=_media_kinds(kinds),
            limit=_bounded_int(limit, minimum=1, maximum=100, field="limit"),
            cursor=_decode_cursor(cursor),
        )

    def timeline_draft(
        self,
        *,
        project_id: str,
        items: list[dict[str, Any]],
        created_at: str,
        format_options: dict[str, Any] | None = None,
        brief_revision: int = 1,
    ) -> dict[str, Any]:
        try:
            timeline = draft_timeline(
                project_id=project_id,
                items=items,
                created_at=created_at,
                format_options=format_options,
                brief_revision=brief_revision,
            )
        except TimelineInputError as exc:
            raise MemoLensError(
                f"{exc.field}: {exc}", code="invalid_timeline_input"
            ) from exc
        return {
            "object": "memolens.timeline_draft",
            "schema_version": "1",
            "status": "completed",
            "source": "in_memory",
            "timeline": timeline,
            "validation": validate_timeline(timeline),
            "next_step": (
                "This draft is not saved. Review it, then import or confirm it in the "
                "MemoLens desktop application."
            ),
            "safety": _timeline_safety_summary(),
        }

    def timeline_revise_draft(
        self,
        *,
        timeline: dict[str, Any],
        operations: list[dict[str, Any]],
        created_at: str,
    ) -> dict[str, Any]:
        try:
            revised = revise_timeline_draft(
                timeline=timeline,
                operations=operations,
                created_at=created_at,
            )
        except TimelineInputError as exc:
            raise MemoLensError(
                f"{exc.field}: {exc}", code="invalid_timeline_input"
            ) from exc
        return {
            "object": "memolens.timeline_draft_revision",
            "schema_version": "1",
            "status": "completed",
            "source": "in_memory",
            "timeline": revised,
            "validation": validate_timeline(revised),
            "next_step": (
                "This revision is not saved. Review the operation diff, then import or "
                "confirm it in the MemoLens desktop application."
            ),
            "safety": _timeline_safety_summary(),
        }

    @staticmethod
    def timeline_validate(timeline: Any) -> dict[str, Any]:
        return validate_timeline(timeline)

    def timeline_list(
        self,
        *,
        project_id: str | None = None,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_limit = _bounded_int(limit, minimum=1, maximum=100, field="limit")
        normalized_project = str(project_id or "").strip() or None
        if normalized_project and len(normalized_project) > 200:
            raise MemoLensError("project_id is invalid.", code="invalid_argument")
        return self._sqlite_timeline_list(
            project_id=normalized_project,
            limit=normalized_limit,
            cursor=_decode_cursor(cursor),
        )

    def timeline_get(
        self, timeline_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        normalized_id = str(timeline_id or "").strip()
        if not normalized_id or len(normalized_id) > 200:
            raise MemoLensError("timeline_id is invalid.", code="invalid_argument")
        normalized_revision = None
        if revision is not None:
            normalized_revision = _bounded_int(
                revision, minimum=1, maximum=1_000_000, field="revision"
            )
        return self._sqlite_timeline_get(normalized_id, normalized_revision)

    # Compatibility delegates keep the established internal diagnostic hooks
    # while all SQL and connection policy live in ReadOnlyMemoLensStore.
    def _sqlite_connection(self):  # noqa: ANN202
        return self._store.connection()

    def _sqlite_status(self) -> dict[str, Any]:
        return self._store.status()

    def _sqlite_search(self, query: str, limit: int) -> dict[str, Any]:
        return self._store.search(query, limit)

    def _sqlite_mixed_image_search(
        self, query: str, *, limit: int
    ) -> dict[str, Any]:
        return self._store.mixed_image_search(query, limit)

    def _sqlite_media_list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self._store.media_list(kinds=kinds, limit=limit, cursor=cursor)

    def _sqlite_media_get(self, asset_id: str) -> dict[str, Any]:
        return self._store.media_get(asset_id)

    def _sqlite_video_search(self, query: str, limit: int) -> dict[str, Any]:
        return self._store.video_search(query, limit)

    def _sqlite_timeline_list(
        self, *, project_id: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self._store.timeline_list(
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )

    def _sqlite_timeline_get(
        self, timeline_id: str, revision: int | None
    ) -> dict[str, Any]:
        return self._store.timeline_get(timeline_id, revision)


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "MAX_RESPONSE_BYTES",
    "MemoLensError",
    "MemoLensGateway",
    "TRUST_LOCAL_API_ENV",
    "json_ready",
    "validate_base_url",
]
