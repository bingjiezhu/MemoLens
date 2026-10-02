#!/usr/bin/env python3
"""Read-only MemoLens client with safe-default SQLite access.

This module deliberately uses only the Python standard library.  It never
opens photo files and never opens the index database in writable mode.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlencode

from memolens_atlas_presenter import present_cleanup, present_memories
from memolens_creative_blueprint import (
    BLUEPRINT_SCHEMA_AVAILABLE,
    blueprint_contract_summary,
)
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
    decode_cursor as _decode_cursor,
    json_ready,
    media_kinds as _media_kinds,
    safety_summary as _safety_summary,
    timeline_safety_summary as _timeline_safety_summary,
)
from memolens_project_store import validate_project_identifier
from memolens_persisted_blueprint import persisted_blueprint_contract_summary
from memolens_read_store import ReadOnlyMemoLensStore
from memolens_wiki import LiveMediaWiki, parse_evidence_id, parse_page_id

from memolens_timeline import (
    TimelineInputError,
    draft_timeline,
    revise_timeline_draft,
    validate_timeline,
)

TRUST_LOCAL_API_ENV = "MEMOLENS_PLUGIN_TRUST_LOCAL_API"
MAX_WIKI_QUERY_LENGTH = 4096


def _strict_bounded_int(
    value: Any, *, minimum: int, maximum: int, field: str
) -> int:
    """Reject JSON floats, strings, and booleans before applying integer bounds."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise MemoLensError(f"{field} must be an integer.", code="invalid_argument")
    return _bounded_int(value, minimum=minimum, maximum=maximum, field=field)


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
        # Discovery is read-only and conveys no runtime/edit authority. Prefer
        # the native selection projection over a stale backend preference file.
        try:
            desktop = json.loads((state_dir / "desktop-settings.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            desktop = None
        if isinstance(desktop, dict):
            binding = desktop.get("librarySelectionAuthority")
            if (
                desktop.get("schemaVersion") == 2
                and isinstance(binding, dict)
                and binding.get("schemaVersion") == 1
                and isinstance(binding.get("dbPath"), str)
                and isinstance(binding.get("canonicalRoot"), str)
            ):
                candidate = _clean_path(binding["dbPath"])
                if candidate is not None:
                    # A confirmed selection is still the selection when its DB
                    # is missing or unreadable. The read store reports that
                    # failure; discovery must not silently switch Libraries.
                    return {
                        "db_path": str(candidate),
                        "image_library_dir": binding["canonicalRoot"],
                    }
        settings_path = state_dir / "backend-settings.json"
        try:
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        if isinstance(payload.get("db_path"), str) and payload["db_path"].strip():
            return payload
        storage = state_dir / "storage"
        hashed = [path for path in storage.glob("photo-index-*.db")
                  if re.fullmatch(r"photo-index-[0-9a-f]{24}\.db", path.name)
                  and path.is_file()]
        if len(hashed) > 1:
            # A higher-priority state with multiple Libraries is ambiguous.
            # Stop here instead of adopting an unrelated lower-priority state.
            return {}
        if len(hashed) == 1:
            return {**payload, "db_path": str(hashed[0].resolve())}
        candidate = storage / "photo_index.db"
        if candidate.is_file():
            return {**payload, "db_path": str(candidate.resolve())}
        if payload:
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
        self._configured_base_url = configured_base_url
        self._api: LocalApiClient | None = None
        self.base_url = DEFAULT_BASE_URL
        self._store = ReadOnlyMemoLensStore(self.db_path, self.library_dir)
        self._wiki = LiveMediaWiki()

    def _ensure_api(self) -> LocalApiClient:
        """Construct the opt-in HTTP client only for an operation that needs it."""

        self._require_local_api_trust()
        if self._api is None:
            self._api = LocalApiClient(
                self._configured_base_url,
                timeout=self.timeout,
                resolver=socket.getaddrinfo,
            )
            self.base_url = self._api.base_url
        return self._api

    @property
    def _opener(self):  # noqa: ANN201
        """Compatibility hook for transport-focused diagnostics and tests."""

        return self._ensure_api().opener if self.trust_local_api else None

    @_opener.setter
    def _opener(self, opener: Any) -> None:
        self._ensure_api().opener = opener

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
        api = self._ensure_api()
        if verify_identity:
            self.health()
        return api.request_json(
            path,
            method=method,
            body=body,
            verify_identity=False,
        )

    def health(self) -> dict[str, Any]:
        api = self._ensure_api()
        payload = api.health()
        self.db_path = api.db_path
        self.library_dir = api.library_dir
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
                    "creative_blueprint_contract": blueprint_contract_summary(),
                    "persisted_blueprint_contract": persisted_blueprint_contract_summary(),
                    "capabilities": _capabilities(
                        None,
                        legacy_search=False,
                        local_api_reads=False,
                        blueprint_schema_available=BLUEPRINT_SCHEMA_AVAILABLE,
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
                "creative_blueprint_contract": blueprint_contract_summary(),
                "persisted_blueprint_contract": persisted_blueprint_contract_summary(),
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                    blueprint_schema_available=BLUEPRINT_SCHEMA_AVAILABLE,
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
                    "creative_blueprint_contract": blueprint_contract_summary(),
                    "persisted_blueprint_contract": persisted_blueprint_contract_summary(),
                    "capabilities": _capabilities(
                        None,
                        legacy_search=False,
                        local_api_reads=False,
                        blueprint_schema_available=BLUEPRINT_SCHEMA_AVAILABLE,
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
                "creative_blueprint_contract": blueprint_contract_summary(),
                "persisted_blueprint_contract": persisted_blueprint_contract_summary(),
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                    blueprint_schema_available=BLUEPRINT_SCHEMA_AVAILABLE,
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
            "creative_blueprint_contract": blueprint_contract_summary(),
            "persisted_blueprint_contract": persisted_blueprint_contract_summary(),
            "profiles": {
                "vision": effective.get("vision_profile_name"),
                "query": effective.get("query_profile_name"),
            },
            "capabilities": _capabilities(
                database,
                legacy_search=bool(database.get("legacy_search_available")),
                local_api_reads=True,
                blueprint_schema_available=BLUEPRINT_SCHEMA_AVAILABLE,
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
        # Image summaries have one public authority lane. User opt-in to the
        # unauthenticated loopback reader must not promote legacy API results
        # over exact-current canonical SQLite evidence.
        result = self._sqlite_search(normalized_query, normalized_limit)
        result.update(
            {
                "source": "sqlite_read_only",
                "mode": "canonical_image_read_only",
                "local_api": self._local_api_summary(available=None),
                "local_api_used": False,
                "warnings": [
                    "Photo summaries require exact current canonical image authority; the optional loopback retrieval endpoint was not used."
                ],
            }
        )
        return result

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

    def mixed_search(
        self,
        query: str,
        *,
        limit: int = 12,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Search raw media through one cold-audited private SQLite snapshot."""

        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MemoLensError("Search query cannot be empty.", code="invalid_argument")
        normalized_limit = _bounded_int(limit, minimum=1, maximum=36, field="limit")

        return self._store.mixed_material_search(
            normalized_query,
            normalized_limit,
            filters=filters,
        )

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

    def wiki_status(self) -> dict[str, Any]:
        """Describe the honest coverage and limits of the live Wiki read model."""

        try:
            database = self._sqlite_status()
        except MemoLensError as exc:
            database = {
                "media_schema_available": False,
                "wiki_read_error_code": exc.code,
            }
        return self._wiki.status(database)

    def wiki_list(
        self,
        *,
        kinds: list[str] | None = None,
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        normalized_kinds = _media_kinds(kinds)
        normalized_limit = _bounded_int(limit, minimum=1, maximum=100, field="limit")
        decoded_cursor = _decode_cursor(cursor)
        try:
            media = self._sqlite_wiki_media_list(
                kinds=normalized_kinds,
                limit=normalized_limit,
                cursor=decoded_cursor,
            )
        except MemoLensError as exc:
            return self._wiki.unavailable_page_list(
                kinds=normalized_kinds,
                reason_code=(
                    "mixed_media_schema_unavailable"
                    if exc.code == "capability_unavailable"
                    else "media_index_read_unavailable"
                ),
            )
        return self._wiki.list_pages(media)

    def wiki_search(self, query: str, *, limit: int = 12) -> dict[str, Any]:
        normalized_query = str(query or "").strip()
        if not normalized_query:
            raise MemoLensError("Search query cannot be empty.", code="invalid_argument")
        if len(normalized_query) > MAX_WIKI_QUERY_LENGTH:
            raise MemoLensError(
                f"Search query must not exceed {MAX_WIKI_QUERY_LENGTH} characters.",
                code="invalid_argument",
            )
        normalized_limit = _bounded_int(limit, minimum=1, maximum=36, field="limit")
        return self._wiki.search(
            self._reference_only_mixed_search(
                normalized_query,
                limit=normalized_limit,
            )
        )

    def _reference_only_mixed_search(
        self,
        query: str,
        *,
        limit: int,
    ) -> dict[str, Any]:
        """Compatibility projection only for the non-executable live Wiki.

        ``memolens_mixed_search`` never uses this path.  The Wiki does not
        claim derivative absence and its results remain reference-only until a
        later executable-material admission revalidates canonical authority.
        """

        branches: list[tuple[str, dict[str, Any]]] = []
        branch_errors: list[dict[str, str]] = []
        for kind, searcher in (
            ("image", self._sqlite_mixed_image_search),
            ("video_segment", self.video_search),
        ):
            try:
                branches.append((kind, searcher(query, limit=limit)))
            except MemoLensError as exc:
                branch_errors.append(
                    {"result_type": kind, "code": exc.code, "message": str(exc)}
                )
        if not branches:
            raise MemoLensError(
                "Neither the photo index nor the current video-segment index could be searched.",
                code="search_unavailable",
            )
        kind_order = {"image": 0, "video_segment": 1}
        fused: list[tuple[float, int, str, dict[str, Any]]] = []
        for kind, payload in branches:
            raw_results = payload.get("results")
            if not isinstance(raw_results, list):
                continue
            for rank, raw in enumerate(raw_results, start=1):
                if not isinstance(raw, dict):
                    continue
                score = 1.0 / (60.0 + rank)
                item = dict(raw)
                item["result_type"] = kind
                item["media_kind"] = "image" if kind == "image" else "video"
                item["rank_score"] = round(score, 8)
                stable_id = str(
                    item.get("segment_id")
                    or item.get("asset_id")
                    or item.get("id")
                    or ""
                )
                fused.append((score, kind_order[kind], stable_id, item))
        fused.sort(key=lambda value: (-value[0], value[1], value[2]))
        results = [item for _score, _kind, _id, item in fused[:limit]]
        return {
            "object": "memolens.mixed_search",
            "schema_version": "1",
            "status": "completed",
            "source": "read_only_reference_compat",
            "ranking": "reciprocal_rank_fusion",
            "query": query,
            "result_count": len(results),
            "results": results,
            "searched_result_types": [kind for kind, _payload in branches],
            "branch_errors": branch_errors,
            "reference_only": True,
            "derivative_absence_claimed": False,
            "safety": _safety_summary(),
        }

    def wiki_open(self, page_id: str) -> dict[str, Any]:
        reference = parse_page_id(page_id)
        if reference.kind == "library":
            return self._wiki.library_page(self.wiki_status())
        if reference.kind == "asset":
            try:
                detail = self._sqlite_wiki_asset_get(reference.identifier)
            except MemoLensError as exc:
                if exc.code == "media_not_found":
                    raise MemoLensError(
                        "MemoLens Wiki page was not found.",
                        code="wiki_page_not_found",
                    ) from exc
                raise
            return self._wiki.asset_page(detail)
        detail = self._store.segment_get(reference.identifier)
        if detail is None:
            raise MemoLensError(
                "MemoLens Wiki page was not found.", code="wiki_page_not_found"
            )
        return self._wiki.span_page(detail)

    def wiki_evidence(self, evidence_id: str) -> dict[str, Any]:
        reference = parse_evidence_id(evidence_id)
        if reference.kind == "asset":
            try:
                detail = self._sqlite_wiki_asset_get(reference.identifier)
            except MemoLensError as exc:
                if exc.code == "media_not_found":
                    raise MemoLensError(
                        "MemoLens Wiki evidence was not found.",
                        code="wiki_evidence_not_found",
                    ) from exc
                raise
            return self._wiki.asset_evidence(detail)
        detail = self._store.segment_get(reference.identifier)
        if detail is None:
            raise MemoLensError(
                "MemoLens Wiki evidence was not found.",
                code="wiki_evidence_not_found",
            )
        return self._wiki.span_evidence(detail)

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

    def project_list(
        self,
        *,
        status: str = "current",
        limit: int = 24,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(status, str) or status not in {
            "current",
            "draft",
            "active",
            "archived",
            "all",
        }:
            raise MemoLensError("status is invalid.", code="invalid_argument")
        normalized_limit = _strict_bounded_int(
            limit, minimum=1, maximum=100, field="limit"
        )
        return self._store.project_list(
            status=status,
            limit=normalized_limit,
            cursor=_decode_cursor(cursor),
        )

    def project_open(self, project_id: str) -> dict[str, Any]:
        return self._store.project_open(
            validate_project_identifier(project_id, field="project_id")
        )

    def project_history(
        self, project_id: str, *, limit: int = 50
    ) -> dict[str, Any]:
        normalized_limit = _strict_bounded_int(
            limit, minimum=1, maximum=100, field="limit"
        )
        return self._store.project_history(
            validate_project_identifier(project_id, field="project_id"),
            normalized_limit,
        )

    def blueprint_shadow(self, project_id: str) -> dict[str, Any]:
        return self._store.blueprint_shadow(
            validate_project_identifier(project_id, field="project_id")
        )

    def blueprint_validate(self, candidate: Any) -> dict[str, Any]:
        return self._store.blueprint_validate(candidate)

    def blueprint_get(
        self, project_id: str, *, revision: int | None = None
    ) -> dict[str, Any]:
        normalized_revision = None
        if revision is not None:
            normalized_revision = _strict_bounded_int(
                revision,
                minimum=1,
                maximum=1_000_000,
                field="revision",
            )
        return self._store.blueprint_get(
            validate_project_identifier(project_id, field="project_id"),
            normalized_revision,
        )

    def blueprint_history(
        self, project_id: str, *, limit: int = 50
    ) -> dict[str, Any]:
        normalized_limit = _strict_bounded_int(
            limit,
            minimum=1,
            maximum=100,
            field="limit",
        )
        return self._store.blueprint_history(
            validate_project_identifier(project_id, field="project_id"),
            normalized_limit,
        )

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

    def _sqlite_wiki_media_list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self._store.wiki_media_list(
            kinds=kinds,
            limit=limit,
            cursor=cursor,
        )

    def _sqlite_wiki_asset_get(self, asset_id: str) -> dict[str, Any]:
        return self._store.wiki_asset_get(asset_id)

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
