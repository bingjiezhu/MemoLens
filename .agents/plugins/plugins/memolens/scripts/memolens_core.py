#!/usr/bin/env python3
"""Read-only MemoLens client with safe-default SQLite access.

This module deliberately uses only the Python standard library.  It never
opens photo files and never opens the index database in writable mode.
"""

from __future__ import annotations

import base64
import heapq
import ipaddress
import json
import os
import re
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from memolens_timeline import (
    TimelineInputError,
    draft_timeline,
    revise_timeline_draft,
    validate_timeline,
)


DEFAULT_BASE_URL = "http://127.0.0.1:5519"
DEFAULT_TIMEOUT = 2.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
TRUST_LOCAL_API_ENV = "MEMOLENS_PLUGIN_TRUST_LOCAL_API"


class MemoLensError(RuntimeError):
    """Expected, user-actionable plugin failure."""

    def __init__(self, message: str, *, code: str = "memolens_error") -> None:
        super().__init__(message)
        self.code = code


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise MemoLensError(
            "The local MemoLens service attempted an HTTP redirect; refusing it.",
            code="redirect_refused",
        )


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
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise MemoLensError(
            "MemoLens base URL must use http or https.", code="unsafe_base_url"
        )
    if parsed.hostname is None or parsed.hostname.casefold() not in LOOPBACK_HOSTS:
        raise MemoLensError(
            "MemoLens base URL must target 127.0.0.1, ::1, or localhost.",
            code="unsafe_base_url",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise MemoLensError(
            "MemoLens base URL cannot contain credentials, query text, or a fragment.",
            code="unsafe_base_url",
        )
    if parsed.path not in {"", "/"}:
        raise MemoLensError(
            "MemoLens base URL cannot contain a path.", code="unsafe_base_url"
        )
    try:
        resolved = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(parsed.hostname, parsed.port or 5519)
        }
    except (OSError, ValueError) as exc:
        raise MemoLensError(
            "MemoLens loopback host could not be resolved safely.",
            code="unsafe_base_url",
        ) from exc
    if not resolved or not all(address.is_loopback for address in resolved):
        raise MemoLensError(
            "MemoLens host resolved outside the loopback interface.",
            code="unsafe_base_url",
        )
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _service_identity(payload: Any) -> bool:
    """Match fields unique to the MemoLens health contract available in v0.1."""

    if not isinstance(payload, dict):
        return False
    return (
        payload.get("status") == "ok"
        and payload.get("object") == "health.check"
        and payload.get("service") == "memolens-backend"
        and payload.get("api_version") == "1"
    )


def _local_api_opted_in() -> bool:
    """Require the exact documented opt-in; truthy aliases are not accepted."""

    return os.getenv(TRUST_LOCAL_API_ENV, "").strip() == "1"


def _validated_settings(payload: Any) -> tuple[dict[str, Any], Path, Path]:
    """Validate the settings contract and return authoritative local paths."""

    if not isinstance(payload, dict) or payload.get("object") != "memolens.settings":
        raise MemoLensError(
            "MemoLens settings returned an unexpected object type.",
            code="invalid_response",
        )
    effective = payload.get("effective")
    if not isinstance(effective, dict):
        raise MemoLensError(
            "MemoLens settings did not include effective settings.",
            code="invalid_response",
        )

    resolved: dict[str, Path] = {}
    for field in ("image_library_dir", "db_path"):
        raw_value = effective.get(field)
        if not isinstance(raw_value, str) or not raw_value.strip():
            raise MemoLensError(
                f"MemoLens settings field `{field}` must be a non-empty path.",
                code="invalid_response",
            )
        raw_path = Path(raw_value).expanduser()
        if not raw_path.is_absolute():
            raise MemoLensError(
                f"MemoLens settings field `{field}` must be an absolute path.",
                code="invalid_response",
            )
        try:
            resolved[field] = raw_path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise MemoLensError(
                f"MemoLens settings field `{field}` could not be resolved safely.",
                code="invalid_response",
            ) from exc

    if not isinstance(effective.get("embedding_backend"), str):
        raise MemoLensError(
            "MemoLens settings did not identify the embedding backend.",
            code="invalid_response",
        )
    if not isinstance(payload.get("index_stats"), dict):
        raise MemoLensError(
            "MemoLens settings did not include index statistics.",
            code="invalid_response",
        )
    return effective, resolved["db_path"], resolved["image_library_dir"]


def _safe_absolute_path(relative_path: Any, library_dir: Path | None) -> dict[str, Any]:
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


def _parse_tags(raw_tags: Any) -> list[str]:
    if isinstance(raw_tags, list):
        return [str(item) for item in raw_tags if str(item).strip()]
    if not isinstance(raw_tags, str):
        return []
    try:
        parsed = json.loads(raw_tags)
    except json.JSONDecodeError:
        return [item.strip() for item in raw_tags.split(",") if item.strip()]
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _compact_asset(raw: Any, library_dir: Path | None) -> dict[str, Any]:
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
        )
        if raw.get(key) is not None
    }
    asset["tags"] = _parse_tags(raw.get("tags", raw.get("tags_json")))
    asset.update(_safe_absolute_path(raw.get("relative_path"), library_dir))
    return asset


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _compact_media_asset(raw: Any) -> dict[str, Any]:
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
        )
        if raw.get(key) is not None
    }
    asset["object"] = "memolens.media_asset"
    asset["schema_version"] = "1"
    asset["codec"] = _parse_json_object(raw.get("codec_json"))
    return asset


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(value: Any, *, field: str = "cursor") -> str | None:
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
        self.base_url = (
            validate_base_url(configured_base_url)
            if self.trust_local_api
            else DEFAULT_BASE_URL
        )
        self.db_path, self.library_dir = resolve_local_paths(
            db_path=db_path, library_dir=library_dir
        )
        self.timeout = max(0.1, min(float(timeout), 30.0))
        # Never inherit HTTP(S)_PROXY for a service whose security contract is
        # strictly loopback-only.
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())
        self._health_cache: dict[str, Any] | None = None
        self._settings_cache: dict[str, Any] | None = None

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
        if verify_identity:
            self.health()
        encoded_body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "MemoLens-Codex-Plugin/0.3.0",
        }
        if body is not None:
            encoded_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{path}", data=encoded_body, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except MemoLensError:
            raise
        except HTTPError as exc:
            raise MemoLensError(
                f"MemoLens returned HTTP {exc.code} for {path}.", code="http_error"
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise MemoLensError(
                "The local MemoLens service is unavailable.", code="service_unavailable"
            ) from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoLensError(
                "MemoLens response exceeded the local safety limit.",
                code="response_too_large",
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MemoLensError(
                "MemoLens returned a non-JSON response.", code="invalid_response"
            ) from exc
        if not isinstance(payload, dict):
            raise MemoLensError(
                "MemoLens returned an unexpected JSON shape.", code="invalid_response"
            )
        return payload

    def health(self) -> dict[str, Any]:
        self._require_local_api_trust()
        if self._health_cache is not None:
            return self._health_cache
        payload = self._request_json("/healthz", verify_identity=False)
        if not _service_identity(payload):
            raise MemoLensError(
                "The loopback service does not match the MemoLens health contract.",
                code="service_identity_mismatch",
            )
        settings = self._request_json("/v1/settings", verify_identity=False)
        _, db_path, library_dir = _validated_settings(settings)
        # Cache only after both contracts have been validated. A transient or
        # spoofed settings response must not make later calls skip validation.
        self._settings_cache = settings
        self.db_path = db_path
        self.library_dir = library_dir
        self._health_cache = payload
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
                        "path": str(self.db_path) if self.db_path else None,
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
                "library_dir": str(self.library_dir) if self.library_dir else None,
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                ),
                "warnings": [
                    "Local API access is disabled by default; only read-only SQLite status and search are available."
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
                        "path": str(self.db_path) if self.db_path else None,
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
                "library_dir": str(self.library_dir) if self.library_dir else None,
                "capabilities": _capabilities(
                    database,
                    legacy_search=bool(database.get("legacy_search_available")),
                    local_api_reads=False,
                ),
                "warnings": [
                    "The MemoLens service is offline; only status and lexical search are available."
                ],
                "safety": _safety_summary(),
            }

        settings = self._settings_cache or {}
        effective = settings.get("effective") if isinstance(settings, dict) else {}
        if not isinstance(effective, dict):
            effective = {}
        try:
            database = self._sqlite_status()
        except MemoLensError as database_error:
            database = {
                "available": False,
                "path": effective.get("db_path"),
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
            "library_dir": effective.get("image_library_dir"),
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
        payload = self._request_json(
            f"/v1/atlas/workbench?{urlencode(params)}"
        )
        if payload.get("object") != "atlas.workbench":
            raise MemoLensError(
                "MemoLens Atlas returned an unexpected object type.",
                code="invalid_response",
            )
        raw_memories = payload.get("memories")
        if not isinstance(raw_memories, list):
            raise MemoLensError(
                "MemoLens Atlas did not return a memory list.",
                code="invalid_response",
            )
        memories: list[dict[str, Any]] = []
        for raw in raw_memories[:normalized_limit]:
            if not isinstance(raw, dict):
                continue
            card = {
                key: raw.get(key)
                for key in (
                    "id",
                    "kind",
                    "label",
                    "asset_count",
                    "top_concepts",
                    "place_label",
                    "time_label",
                    "score",
                    "duplicate_count",
                    "chapter_count",
                )
                if raw.get(key) is not None
            }
            representatives = raw.get("representative_assets")
            if not isinstance(representatives, list):
                representatives = raw.get("best_assets")
            card["representative_assets"] = [
                _compact_asset(asset, self.library_dir)
                for asset in (representatives or [])[:5]
                if isinstance(asset, dict)
            ]
            memories.append(card)
        return {
            "object": "memolens.memories",
            "status": "completed",
            "source": "local_api",
            "mode": "opt_in_local_api",
            "local_api": self._local_api_summary(
                checked=True, available=True, identity_verified=True
            ),
            "query": query.strip() if query and query.strip() else None,
            "memory_count": len(memories),
            "memories": memories,
            "suggested_queries": payload.get("suggested_queries", []),
            "storylines": payload.get("storylines", []),
            "safety": _safety_summary(),
        }

    def cleanup(self) -> dict[str, Any]:
        payload = self._request_json("/v1/atlas/cleanup")
        if payload.get("object") != "atlas.cleanup":
            raise MemoLensError(
                "MemoLens cleanup report returned an unexpected object type.",
                code="invalid_response",
            )
        stacks: list[dict[str, Any]] = []
        for raw in payload.get("stacks", [])[:48]:
            if not isinstance(raw, dict):
                continue
            stack = {
                key: raw.get(key)
                for key in (
                    "id",
                    "kind",
                    "count",
                    "asset_ids",
                    "representative_asset_id",
                    "best_asset_id",
                    "score",
                    "reason",
                )
                if raw.get(key) is not None
            }
            stack["assets"] = [
                _compact_asset(asset, self.library_dir)
                for asset in raw.get("assets", [])[:8]
                if isinstance(asset, dict)
            ]
            if isinstance(raw.get("best_asset"), dict):
                stack["best_asset"] = _compact_asset(raw["best_asset"], self.library_dir)
            stacks.append(stack)

        def compact_list(key: str) -> list[dict[str, Any]]:
            raw_items = payload.get(key, [])
            if not isinstance(raw_items, list):
                return []
            return [
                _compact_asset(item, self.library_dir)
                for item in raw_items[:24]
                if isinstance(item, dict)
            ]

        return {
            "object": "memolens.cleanup_report",
            "status": "completed",
            "source": "local_api",
            "mode": "opt_in_local_api",
            "local_api": self._local_api_summary(
                checked=True, available=True, identity_verified=True
            ),
            "read_only": True,
            "counts": {
                key: payload.get(key, 0)
                for key in (
                    "duplicate_stack_count",
                    "similar_stack_count",
                    "low_quality_count",
                    "missing_time_count",
                    "missing_place_count",
                    "people_review_count",
                )
            },
            "stacks": stacks,
            "low_quality_assets": compact_list("low_quality_assets"),
            "missing_time_assets": compact_list("missing_time_assets"),
            "missing_place_assets": compact_list("missing_place_assets"),
            "people_review_assets": compact_list("people_review_assets"),
            "warning": "Review candidates only. No file was deleted, moved, or modified.",
            "safety": _safety_summary(),
        }

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
            ("image", self.search),
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

    def _sqlite_connection(self) -> sqlite3.Connection:
        if self.db_path is None:
            raise MemoLensError(
                "No MemoLens SQLite index was discovered. Set MEMOLENS_DB_PATH or open MemoLens.",
                code="database_not_configured",
            )
        if not self.db_path.is_file():
            raise MemoLensError(
                f"MemoLens SQLite index does not exist: {self.db_path}",
                code="database_not_found",
            )
        uri = f"file:{quote(str(self.db_path), safe='/:')}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise MemoLensError(
                "The MemoLens SQLite index could not be opened read-only.",
                code="database_unavailable",
            ) from exc

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        if table not in {
            "image_index",
            "library_roots",
            "assets",
            "asset_sources",
            "analysis_runs",
            "asset_analysis_heads",
            "video_segments",
            "current_video_segments",
            "creative_projects",
            "timelines",
            "timeline_revisions",
        }:
            return False
        try:
            return connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name = ?",
                (table,),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite schema could not be read.",
                code="database_unavailable",
            ) from exc

    def _table_columns(
        self, connection: sqlite3.Connection, table: str
    ) -> set[str]:
        if not self._table_exists(connection, table):
            return set()
        try:
            return {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite schema could not be read.",
                code="database_unavailable",
            ) from exc

    def _sqlite_columns(self, connection: sqlite3.Connection) -> set[str]:
        columns = self._table_columns(connection, "image_index")
        if not columns:
            raise MemoLensError(
                "The SQLite file does not contain the legacy MemoLens image index.",
                code="capability_unavailable",
            )
        return columns

    def _sqlite_status(self) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            image_columns = self._table_columns(connection, "image_index")
            asset_columns = self._table_columns(connection, "assets")
            if image_columns and not {"id", "filename", "relative_path"}.issubset(
                image_columns
            ):
                raise MemoLensError(
                    "The SQLite image_index table is missing required MemoLens columns.",
                    code="database_identity_mismatch",
                )
            if asset_columns and not {"id", "kind", "sha256"}.issubset(asset_columns):
                raise MemoLensError(
                    "The SQLite assets table is missing required MemoLens columns.",
                    code="database_identity_mismatch",
                )
            if not image_columns and not asset_columns:
                raise MemoLensError(
                    "The SQLite file is not a MemoLens media index.",
                    code="database_identity_mismatch",
                )
            try:
                image_count = (
                    int(connection.execute("SELECT COUNT(*) FROM image_index").fetchone()[0])
                    if image_columns
                    else 0
                )
                backends: list[dict[str, Any]] = []
                if "embedding_backend" in image_columns:
                    backends = [
                        {"name": row[0], "count": int(row[1])}
                        for row in connection.execute(
                            "SELECT embedding_backend, COUNT(*) FROM image_index "
                            "GROUP BY embedding_backend ORDER BY COUNT(*) DESC"
                        ).fetchall()
                    ]
                kind_counts: dict[str, int] = {}
                asset_count = image_count
                if asset_columns:
                    rows = connection.execute(
                        "SELECT kind, COUNT(*) FROM assets GROUP BY kind ORDER BY kind"
                    ).fetchall()
                    kind_counts = {str(row[0]): int(row[1]) for row in rows}
                    asset_count = sum(kind_counts.values())
                source_columns = self._table_columns(connection, "asset_sources")
                segment_columns = self._table_columns(connection, "video_segments")
                video_schema_mode = self._video_schema_mode(
                    connection, asset_columns, segment_columns
                )
                video_search_available = bool(video_schema_mode and source_columns)
                video_segment_count = 0
                if video_search_available:
                    video_sql, _ = self._video_rows_sql(
                        connection,
                        asset_columns,
                        source_columns,
                        segment_columns,
                    )
                    video_segment_count = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM ({video_sql}) current_segments"
                        ).fetchone()[0]
                    )
                timeline_schema = self._timeline_schema(connection)
                timeline_read_available = timeline_schema is not None
                timeline_revision_count = 0
                timeline_count = 0
                if timeline_schema is not None:
                    timeline_table, timeline_id_column = timeline_schema
                    timeline_revision_count = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {timeline_table}"
                        ).fetchone()[0]
                    )
                    timeline_count = int(
                        connection.execute(
                            f"SELECT COUNT(DISTINCT {timeline_id_column}) "
                            f"FROM {timeline_table}"
                        ).fetchone()[0]
                    )
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be queried.",
                    code="database_unavailable",
                ) from exc
        return {
            "available": True,
            "path": str(self.db_path),
            "open_mode": "read_only",
            "asset_count": asset_count,
            "legacy_image_count": image_count,
            "asset_kind_counts": kind_counts,
            "video_segment_count": video_segment_count,
            "timeline_count": timeline_count,
            "timeline_revision_count": timeline_revision_count,
            "legacy_search_available": bool(image_columns),
            "media_schema_available": bool(
                {"id", "kind", "sha256"}.issubset(asset_columns)
                and {"id", "asset_id", "relative_path"}.issubset(source_columns)
                and {"display_filename", "filename"} & source_columns
                and {"availability", "status"} & source_columns
            ),
            "video_search_available": video_search_available,
            "video_schema_mode": video_schema_mode,
            "timeline_read_available": timeline_read_available,
            "embedding_backends": backends,
        }

    def _sqlite_search(self, query: str, limit: int) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            columns = self._sqlite_columns(connection)
            required = {"id", "filename", "relative_path"}
            if not required.issubset(columns):
                raise MemoLensError(
                    "The SQLite image_index table is missing required MemoLens columns.",
                    code="database_identity_mismatch",
                )

            desired = [
                "id",
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
            select_parts = [
                f'"{name}" AS "{name}"' if name in columns else f'NULL AS "{name}"'
                for name in desired
            ]
            phrase = query.casefold()
            terms = _query_terms(query)
            scored: list[tuple[float, str, int, dict[str, Any]]] = []
            scanned_count = 0
            sequence = 0
            try:
                cursor = connection.execute(
                    f"SELECT {', '.join(select_parts)} FROM image_index"
                )
                while True:
                    rows = cursor.fetchmany(512)
                    if not rows:
                        break
                    for row in rows:
                        scanned_count += 1
                        raw = dict(row)
                        tags = _parse_tags(raw.get("tags_json"))
                        fields = {
                            "filename": str(raw.get("filename") or "").casefold(),
                            "path": str(raw.get("relative_path") or "").casefold(),
                            "place": " ".join(
                                str(raw.get(key) or "")
                                for key in ("place_name", "country")
                            ).casefold(),
                            "description": str(raw.get("description") or "").casefold(),
                            "tags": " ".join(tags).casefold(),
                            "combined": str(raw.get("combined_text") or "").casefold(),
                        }
                        blob = " ".join(fields.values())
                        score = 0.0
                        matched: list[str] = []
                        if phrase in blob:
                            score += 8.0
                            matched.append(query)
                        for term in terms:
                            term_score = 0.0
                            term_score += 3.0 if term in fields["tags"] else 0.0
                            term_score += 2.5 if term in fields["place"] else 0.0
                            term_score += 2.0 if term in fields["description"] else 0.0
                            term_score += 1.5 if term in fields["filename"] or term in fields["path"] else 0.0
                            term_score += 1.0 if term in fields["combined"] else 0.0
                            if term_score:
                                matched.append(term)
                                score += term_score
                        if score <= 0:
                            continue
                        quality = _quality_value(
                            raw.get("aesthetic_score"),
                            raw.get("technical_quality_score"),
                        )
                        score += quality * 0.25
                        raw["tags"] = tags
                        raw["score"] = round(score, 4)
                        raw["matched_terms"] = list(dict.fromkeys(matched))
                        candidate = (
                            score,
                            str(raw.get("taken_at") or ""),
                            sequence,
                            raw,
                        )
                        sequence += 1
                        if len(scored) < limit:
                            heapq.heappush(scored, candidate)
                        elif candidate[:3] > scored[0][:3]:
                            heapq.heapreplace(scored, candidate)
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be searched.",
                    code="database_unavailable",
                ) from exc

        scored.sort(key=lambda item: item[:3], reverse=True)
        results = [
            _compact_asset(raw, self.library_dir) for _, _, _, raw in scored
        ]
        return {
            "object": "memolens.search",
            "status": "completed",
            "source": "sqlite_fallback",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "database_path": str(self.db_path),
            "safety": _safety_summary(),
        }

    def _require_media_schema(
        self, connection: sqlite3.Connection
    ) -> tuple[set[str], set[str], set[str]]:
        asset_columns = self._table_columns(connection, "assets")
        source_columns = self._table_columns(connection, "asset_sources")
        segment_columns = self._table_columns(connection, "video_segments")
        if not {"id", "kind", "sha256"}.issubset(asset_columns):
            raise MemoLensError(
                "The installed MemoLens index does not yet expose the mixed-media schema.",
                code="capability_unavailable",
            )
        required_source = {"id", "asset_id", "relative_path"}
        has_filename = bool({"display_filename", "filename"} & source_columns)
        has_availability = bool({"availability", "status"} & source_columns)
        if (
            not required_source.issubset(source_columns)
            or not has_filename
            or not has_availability
        ):
            raise MemoLensError(
                "The installed MemoLens index does not expose safe media sources.",
                code="capability_unavailable",
            )
        return asset_columns, source_columns, segment_columns

    @staticmethod
    def _media_select_parts(
        asset_columns: set[str], source_columns: set[str]
    ) -> list[str]:
        desired = [
            "id",
            "kind",
            "sha256",
            "mime_type",
            "file_size",
            "duration_ms",
            "width",
            "height",
            "rotation_degrees",
            "codec_json",
            "captured_at",
            "error_code",
        ]
        parts = [
            f'a."{name}" AS "{name}"'
            if name in asset_columns
            else f'NULL AS "{name}"'
            for name in desired
        ]
        status_column = "probe_status" if "probe_status" in asset_columns else "status"
        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        parts.extend(
            [
                (
                    f'a."{status_column}" AS "probe_status"'
                    if status_column in asset_columns
                    else 'NULL AS "probe_status"'
                ),
                'src."id" AS "asset_source_id"',
                f'src."{filename_column}" AS "filename"',
            'src."relative_path" AS "relative_path"',
                f'src."{availability_column}" AS "source_availability"',
            ]
        )
        return parts

    @staticmethod
    def _media_source_join(
        source_columns: set[str], *, available_only: bool = False
    ) -> str:
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        available_filter = (
            f" AND src2.{availability_column} = 'available'" if available_only else ""
        )
        preferred_order = (
            "CASE WHEN src2.is_preferred = 1 THEN 0 ELSE 1 END, "
            if "is_preferred" in source_columns
            else ""
        )
        return (
            "LEFT JOIN asset_sources src ON src.id = ("
            "SELECT src2.id FROM asset_sources src2 WHERE src2.asset_id = a.id "
            f"{available_filter} ORDER BY "
            f"CASE WHEN src2.{availability_column} = 'available' THEN 0 ELSE 1 END, "
            f"{preferred_order}src2.id LIMIT 1)"
        )

    def _video_schema_mode(
        self,
        connection: sqlite3.Connection,
        asset_columns: set[str],
        segment_columns: set[str],
    ) -> str | None:
        common_segments = {
            "id",
            "asset_id",
            "start_ms",
            "end_ms",
            "combined_text",
        }
        if not common_segments.issubset(segment_columns):
            return None
        run_columns = self._table_columns(connection, "analysis_runs")
        final_runs = {"id", "asset_id", "revision", "status"}.issubset(run_columns)
        if "analysis_run_id" in segment_columns and final_runs:
            view_columns = self._table_columns(connection, "current_video_segments")
            if {
                *common_segments,
                "analysis_run_id",
                "analysis_revision",
            }.issubset(view_columns):
                return "analysis_heads_view"
            head_columns = self._table_columns(connection, "asset_analysis_heads")
            if {"asset_id", "analysis_run_id"}.issubset(head_columns):
                return "analysis_heads_join"
        if (
            "analysis_revision" in segment_columns
            and "current_analysis_revision" in asset_columns
        ):
            return "explicit_revision_head_compat"
        return None

    def _video_rows_sql(
        self,
        connection: sqlite3.Connection,
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
        *,
        asset_id_filter: bool = False,
    ) -> tuple[str, str]:
        mode = self._video_schema_mode(connection, asset_columns, segment_columns)
        if mode is None:
            raise MemoLensError(
                "No safe current-successful video analysis selector is available.",
                code="video_index_unavailable",
            )
        relation_columns = (
            self._table_columns(connection, "current_video_segments")
            if mode == "analysis_heads_view"
            else segment_columns
        )
        desired_segments = [
            "id",
            "asset_id",
            "ordinal",
            "start_ms",
            "end_ms",
            "boundary_reason",
            "summary",
            "visible_text",
            "combined_text",
            "semantic_json",
            "visual_status",
            "transcript_status",
            "confidence",
        ]
        select_parts = [
            f's."{name}" AS "{name}"'
            if name in relation_columns
            else f'NULL AS "{name}"'
            for name in desired_segments
        ]
        if mode.startswith("analysis_heads"):
            select_parts.extend(
                [
                    's."analysis_run_id" AS "analysis_run_id"',
                    'ar."revision" AS "analysis_revision"',
                ]
            )
        else:
            select_parts.extend(
                [
                    'NULL AS "analysis_run_id"',
                    's."analysis_revision" AS "analysis_revision"',
                ]
            )
        for name in (
            "sha256",
            "duration_ms",
            "width",
            "height",
            "rotation_degrees",
        ):
            select_parts.append(
                f'a."{name}" AS "asset_{name}"'
                if name in asset_columns
                else f'NULL AS "asset_{name}"'
            )
        filename_column = (
            "display_filename" if "display_filename" in source_columns else "filename"
        )
        availability_column = (
            "availability" if "availability" in source_columns else "status"
        )
        select_parts.extend(
            [
                'src."id" AS "asset_source_id"',
                f'src."{filename_column}" AS "filename"',
                'src."relative_path" AS "relative_path"',
                f'src."{availability_column}" AS "source_availability"',
            ]
        )
        if mode == "analysis_heads_view":
            from_sql = (
                "FROM current_video_segments s "
                "JOIN assets a ON a.id = s.asset_id "
                "JOIN analysis_runs ar ON ar.id = s.analysis_run_id "
                "AND ar.asset_id = s.asset_id AND ar.status = 'succeeded' "
            )
        elif mode == "analysis_heads_join":
            from_sql = (
                "FROM video_segments s "
                "JOIN asset_analysis_heads ah ON ah.asset_id = s.asset_id "
                "AND ah.analysis_run_id = s.analysis_run_id "
                "JOIN analysis_runs ar ON ar.id = s.analysis_run_id "
                "AND ar.asset_id = s.asset_id AND ar.status = 'succeeded' "
                "JOIN assets a ON a.id = s.asset_id "
            )
        else:
            from_sql = "FROM video_segments s JOIN assets a ON a.id = s.asset_id "
        from_sql += self._media_source_join(source_columns, available_only=True)
        if (
            "library_root_id" in source_columns
            and self._table_exists(connection, "library_roots")
            and {"id", "status"}.issubset(
                self._table_columns(connection, "library_roots")
            )
        ):
            from_sql += (
                " JOIN library_roots root ON root.id = src.library_root_id "
                "AND root.status = 'active'"
            )
        where = ["a.kind = 'video'", "src.id IS NOT NULL"]
        if mode == "explicit_revision_head_compat":
            where.append("s.analysis_revision = a.current_analysis_revision")
        if asset_id_filter:
            where.append("a.id = ?")
        return (
            f"SELECT {', '.join(select_parts)} {from_sql} "
            f"WHERE {' AND '.join(where)}",
            mode,
        )

    def _sqlite_media_list(
        self,
        *,
        kinds: list[str],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            asset_columns, source_columns, _segment_columns = self._require_media_schema(
                connection
            )
            placeholders = ", ".join("?" for _kind in kinds)
            where = [f"a.kind IN ({placeholders})"]
            params: list[Any] = list(kinds)
            if cursor is not None:
                where.append("a.id > ?")
                params.append(cursor)
            params.append(limit + 1)
            try:
                rows = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(asset_columns, source_columns))} "
                    f"FROM assets a {self._media_source_join(source_columns)} "
                    f"WHERE {' AND '.join(where)} ORDER BY a.id ASC LIMIT ?",
                    params,
                ).fetchall()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens mixed-media index could not be listed.",
                    code="database_unavailable",
                ) from exc
        has_more = len(rows) > limit
        selected = rows[:limit]
        assets = [_compact_media_asset(dict(row)) for row in selected]
        return {
            "object": "memolens.media_list",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "kinds": kinds,
            "result_count": len(assets),
            "assets": assets,
            "next_cursor": (
                _encode_cursor(str(selected[-1]["id"]))
                if has_more and selected
                else None
            ),
            "safety": _safety_summary(),
        }

    def _sqlite_media_get(self, asset_id: str) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_media_schema(
                connection
            )
            try:
                row = connection.execute(
                    f"SELECT {', '.join(self._media_select_parts(asset_columns, source_columns))} "
                    f"FROM assets a {self._media_source_join(source_columns)} WHERE a.id = ?",
                    (asset_id,),
                ).fetchone()
                if row is None:
                    raise MemoLensError(
                        "Media asset was not found.", code="media_not_found"
                    )
                raw = dict(row)
                segments: list[dict[str, Any]] = []
                video_index_status = None
                if raw.get("kind") == "video":
                    try:
                        video_sql, video_schema_mode = self._video_rows_sql(
                            connection,
                            asset_columns,
                            source_columns,
                            segment_columns,
                            asset_id_filter=True,
                        )
                    except MemoLensError as exc:
                        if exc.code != "video_index_unavailable":
                            raise
                        video_index_status = "video_index_unavailable"
                    else:
                        segment_rows = connection.execute(
                            f"{video_sql} ORDER BY s.start_ms ASC, s.id ASC LIMIT 500",
                            (asset_id,),
                        ).fetchall()
                        segments = [self._compact_video_segment(dict(item)) for item in segment_rows]
                        video_index_status = video_schema_mode
            except MemoLensError:
                raise
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens media asset could not be read.",
                    code="database_unavailable",
                ) from exc
        return {
            "object": "memolens.media_detail",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "asset": _compact_media_asset(raw),
            "segments": segments,
            "video_index_status": video_index_status,
            "safety": _safety_summary(),
        }

    def _sqlite_video_search(self, query: str, limit: int) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            asset_columns, source_columns, segment_columns = self._require_media_schema(
                connection
            )
            video_sql, video_schema_mode = self._video_rows_sql(
                connection, asset_columns, source_columns, segment_columns
            )
            phrase = query.casefold()
            terms = _query_terms(query)
            scored: list[tuple[float, str, int, dict[str, Any]]] = []
            scanned_count = 0
            sequence = 0
            try:
                cursor = connection.execute(
                    video_sql
                )
                while True:
                    rows = cursor.fetchmany(512)
                    if not rows:
                        break
                    for row in rows:
                        scanned_count += 1
                        raw = dict(row)
                        fields = {
                            "summary": str(raw.get("summary") or "").casefold(),
                            "visible_text": str(raw.get("visible_text") or "").casefold(),
                            "combined": str(raw.get("combined_text") or "").casefold(),
                            "semantic": str(raw.get("semantic_json") or "").casefold(),
                            "filename": str(raw.get("filename") or "").casefold(),
                        }
                        blob = " ".join(fields.values())
                        score = 0.0
                        matched: list[str] = []
                        if phrase in blob:
                            score += 8.0
                            matched.append(query)
                        for term in terms:
                            term_score = 0.0
                            term_score += 3.0 if term in fields["summary"] else 0.0
                            term_score += 2.5 if term in fields["visible_text"] else 0.0
                            term_score += 2.0 if term in fields["semantic"] else 0.0
                            term_score += 1.5 if term in fields["filename"] else 0.0
                            term_score += 1.0 if term in fields["combined"] else 0.0
                            if term_score:
                                score += term_score
                                matched.append(term)
                        if score <= 0:
                            continue
                        raw["matched_terms"] = list(dict.fromkeys(matched))
                        raw["raw_score"] = score
                        candidate = (score, str(raw.get("id") or ""), sequence, raw)
                        sequence += 1
                        if len(scored) < limit:
                            heapq.heappush(scored, candidate)
                        elif candidate[:3] > scored[0][:3]:
                            heapq.heapreplace(scored, candidate)
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens video-segment index could not be searched.",
                    code="database_unavailable",
                ) from exc
        scored.sort(key=lambda item: item[:3], reverse=True)
        results = [self._compact_video_match(raw) for _, _, _, raw in scored]
        return {
            "object": "memolens.video_search",
            "schema_version": "1",
            "status": "completed",
            "ranking": "deterministic_lexical",
            "query": query,
            "result_count": len(results),
            "scanned_count": scanned_count,
            "results": results,
            "video_schema_mode": video_schema_mode,
            "safety": _safety_summary(),
        }

    @staticmethod
    def _compact_video_segment(raw: dict[str, Any]) -> dict[str, Any]:
        semantic = _parse_json_object(raw.get("semantic_json"))
        return {
            key: raw.get(key)
            for key in (
                "id",
                "asset_id",
                "asset_source_id",
                "ordinal",
                "start_ms",
                "end_ms",
                "analysis_run_id",
                "analysis_revision",
                "boundary_reason",
                "summary",
                "visible_text",
                "visual_status",
                "transcript_status",
                "confidence",
            )
            if raw.get(key) is not None
        } | {"semantic": semantic or None}

    def _compact_video_match(self, raw: dict[str, Any]) -> dict[str, Any]:
        score = float(raw.get("raw_score") or 0.0)
        match = {
            "object": "creative_asset_match",
            "schema_version": "1",
            "result_type": "video_segment",
            "id": raw.get("id"),
            "asset_id": raw.get("asset_id"),
            "asset_source_id": raw.get("asset_source_id"),
            "asset_sha256": raw.get("asset_sha256"),
            "segment_id": raw.get("id"),
            "start_ms": raw.get("start_ms"),
            "end_ms": raw.get("end_ms"),
            "asset_duration_ms": raw.get("asset_duration_ms"),
            "filename": raw.get("filename"),
            "relative_path": raw.get("relative_path"),
            "source_availability": raw.get("source_availability"),
            "summary": raw.get("summary"),
            "visible_text": raw.get("visible_text"),
            "matched_terms": raw.get("matched_terms", []),
            "score": round(score / (score + 5.0), 6),
            "confidence": raw.get("confidence"),
            "analysis_run_id": raw.get("analysis_run_id"),
            "analysis_revision": raw.get("analysis_revision"),
            "semantic": _parse_json_object(raw.get("semantic_json")) or None,
            "provenance": [
                source
                for source, available in (
                    ("visual", bool(raw.get("summary") or raw.get("visible_text"))),
                    ("transcript", raw.get("transcript_status") == "complete"),
                )
                if available
            ],
        }
        return match

    def _timeline_schema(
        self, connection: sqlite3.Connection
    ) -> tuple[str, str] | None:
        required = {
            "project_id",
            "revision",
            "schema_version",
            "timeline_json",
            "content_sha256",
            "provenance_json",
            "validation_status",
            "created_at",
        }
        for table, timeline_id_column in (
            ("timelines", "id"),
            ("timeline_revisions", "timeline_id"),
        ):
            columns = self._table_columns(connection, table)
            if required | {timeline_id_column} <= columns:
                return table, timeline_id_column
        return None

    def _require_timeline_schema(
        self, connection: sqlite3.Connection
    ) -> tuple[str, str]:
        schema = self._timeline_schema(connection)
        if schema is None:
            raise MemoLensError(
                "The installed MemoLens index does not expose persisted timeline revisions.",
                code="capability_unavailable",
            )
        return schema

    def _sqlite_timeline_list(
        self,
        *,
        project_id: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            timeline_table, timeline_id_column = self._require_timeline_schema(
                connection
            )
            where: list[str] = []
            params: list[Any] = []
            if project_id is not None:
                where.append("t.project_id = ?")
                params.append(project_id)
            if cursor is not None:
                where.append(f"t.{timeline_id_column} > ?")
                params.append(cursor)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            try:
                rows = connection.execute(
                    f"SELECT t.{timeline_id_column} AS timeline_id, t.project_id, "
                    "t.revision, t.schema_version, t.timeline_json, t.content_sha256, "
                    "t.validation_status, t.created_at "
                    f"FROM {timeline_table} t JOIN ("
                    f"SELECT {timeline_id_column} AS timeline_id, "
                    "MAX(revision) AS latest_revision "
                    f"FROM {timeline_table} GROUP BY {timeline_id_column}"
                    f") latest ON latest.timeline_id = t.{timeline_id_column} "
                    "AND latest.latest_revision = t.revision "
                    f"{where_sql} ORDER BY t.{timeline_id_column} ASC LIMIT ?",
                    [*params, limit + 1],
                ).fetchall()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Persisted timelines could not be listed.", code="database_unavailable"
                ) from exc
        has_more = len(rows) > limit
        selected = rows[:limit]
        timelines = []
        for row in selected:
            raw = dict(row)
            timeline = _parse_json_object(raw.pop("timeline_json", None))
            raw["format"] = timeline.get("format") if timeline else None
            raw["track_count"] = len(timeline.get("tracks", [])) if timeline else 0
            timelines.append(raw)
        return {
            "object": "memolens.timeline_list",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            "project_id": project_id,
            "result_count": len(timelines),
            "timelines": timelines,
            "next_cursor": (
                _encode_cursor(str(selected[-1]["timeline_id"]))
                if has_more and selected
                else None
            ),
            "safety": _safety_summary(),
        }

    def _sqlite_timeline_get(
        self, timeline_id: str, revision: int | None
    ) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            timeline_table, timeline_id_column = self._require_timeline_schema(
                connection
            )
            query = (
                f"SELECT {timeline_id_column} AS timeline_id, project_id, revision, "
                "schema_version, timeline_json, content_sha256, provenance_json, "
                f"validation_status, created_at FROM {timeline_table} "
                f"WHERE {timeline_id_column} = ?"
            )
            params: list[Any] = [timeline_id]
            if revision is not None:
                query += " AND revision = ?"
                params.append(revision)
            query += " ORDER BY revision DESC LIMIT 1"
            try:
                row = connection.execute(query, params).fetchone()
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The persisted timeline could not be read.",
                    code="database_unavailable",
                ) from exc
        if row is None:
            raise MemoLensError("Timeline revision was not found.", code="timeline_not_found")
        raw = dict(row)
        timeline = _parse_json_object(raw.pop("timeline_json", None))
        if not timeline:
            raise MemoLensError(
                "The persisted timeline JSON is invalid.", code="invalid_persisted_timeline"
            )
        stored_provenance = _parse_json_object(raw.pop("provenance_json", None))
        return {
            "object": "memolens.timeline_detail",
            "schema_version": "1",
            "status": "completed",
            "source": "sqlite_read_only",
            "mode": "safe_default_read_only",
            **raw,
            "stored_provenance": stored_provenance,
            "timeline": timeline,
            "validation": validate_timeline(timeline),
            "safety": _safety_summary(),
        }


def _query_terms(query: str) -> list[str]:
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


def _media_kinds(value: Any) -> list[str]:
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


def _quality_value(*values: Any) -> float:
    numeric: list[float] = []
    for value in values:
        try:
            if value is not None:
                numeric.append(max(0.0, min(float(value), 1.0)))
        except (TypeError, ValueError):
            continue
    return sum(numeric) / len(numeric) if numeric else 0.0


def _bounded_int(value: Any, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool):
        raise MemoLensError(f"{field} must be an integer.", code="invalid_argument")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoLensError(f"{field} must be an integer.", code="invalid_argument") from exc
    if parsed < minimum or parsed > maximum:
        raise MemoLensError(
            f"{field} must be between {minimum} and {maximum}.",
            code="invalid_argument",
        )
    return parsed


def _safety_summary() -> dict[str, Any]:
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


def _timeline_safety_summary() -> dict[str, Any]:
    return {
        **_safety_summary(),
        "in_memory_only": True,
        "requires_desktop_confirmation_to_persist": True,
    }


def _capabilities(
    database: dict[str, Any] | None,
    *,
    legacy_search: bool,
    local_api_reads: bool,
) -> dict[str, bool]:
    database = database or {}
    media = bool(database.get("media_schema_available"))
    video = bool(database.get("video_search_available"))
    timelines = bool(database.get("timeline_read_available"))
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
        "create_timeline": False,
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


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "MemoLensError",
    "MemoLensGateway",
    "TRUST_LOCAL_API_ENV",
    "json_ready",
    "validate_base_url",
]
