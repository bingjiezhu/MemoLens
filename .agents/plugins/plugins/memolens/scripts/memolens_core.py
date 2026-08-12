#!/usr/bin/env python3
"""Read-only MemoLens client with safe-default SQLite access.

This module deliberately uses only the Python standard library.  It never
opens photo files and never opens the index database in writable mode.
"""

from __future__ import annotations

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
            "User-Agent": "MemoLens-Codex-Plugin/0.2.0",
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
                    "mode": "safe_default",
                    "local_api": self._local_api_summary(available=None),
                    "database": {
                        "available": False,
                        "path": str(self.db_path) if self.db_path else None,
                        "error": str(database_error),
                        "error_code": database_error.code,
                    },
                    "capabilities": {
                        "status": True,
                        "search": False,
                        "memories": False,
                        "cleanup": False,
                    },
                    "warnings": [
                        "Local API access is disabled and no readable SQLite index was found."
                    ],
                    "safety": _safety_summary(),
                }
            return {
                "object": "memolens.status",
                "status": "ok",
                "source": "sqlite_read_only",
                "mode": "safe_default",
                "local_api": self._local_api_summary(available=None),
                "database": database,
                "library_dir": str(self.library_dir) if self.library_dir else None,
                "capabilities": {
                    "status": True,
                    "search": True,
                    "memories": False,
                    "cleanup": False,
                },
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
                    "capabilities": {
                        "status": True,
                        "search": False,
                        "memories": False,
                        "cleanup": False,
                    },
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
                "capabilities": {
                    "status": True,
                    "search": True,
                    "memories": False,
                    "cleanup": False,
                },
                "warnings": [
                    "The MemoLens service is offline; only status and lexical search are available."
                ],
                "safety": _safety_summary(),
            }

        settings = self._settings_cache or {}
        effective = settings.get("effective") if isinstance(settings, dict) else {}
        if not isinstance(effective, dict):
            effective = {}
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
            "database": {
                "available": True,
                "path": effective.get("db_path"),
                "index_stats": settings.get("index_stats"),
                "embedding_backend": effective.get("embedding_backend"),
            },
            "profiles": {
                "vision": effective.get("vision_profile_name"),
                "query": effective.get("query_profile_name"),
            },
            "capabilities": {
                "status": True,
                "search": True,
                "memories": True,
                "cleanup": True,
            },
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
                    "mode": "safe_default",
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

    def _sqlite_columns(self, connection: sqlite3.Connection) -> set[str]:
        try:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'image_index'"
            ).fetchone()
            if exists is None:
                raise MemoLensError(
                    "The SQLite file is not a MemoLens image index.",
                    code="database_identity_mismatch",
                )
            return {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(image_index)").fetchall()
            }
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite schema could not be read.",
                code="database_unavailable",
            ) from exc

    def _sqlite_status(self) -> dict[str, Any]:
        with closing(self._sqlite_connection()) as connection:
            columns = self._sqlite_columns(connection)
            required = {"id", "filename", "relative_path"}
            if not required.issubset(columns):
                raise MemoLensError(
                    "The SQLite image_index table is missing required MemoLens columns.",
                    code="database_identity_mismatch",
                )
            try:
                count = int(
                    connection.execute("SELECT COUNT(*) FROM image_index").fetchone()[0]
                )
                backends: list[dict[str, Any]] = []
                if "embedding_backend" in columns:
                    backends = [
                        {"name": row[0], "count": int(row[1])}
                        for row in connection.execute(
                            "SELECT embedding_backend, COUNT(*) FROM image_index "
                            "GROUP BY embedding_backend ORDER BY COUNT(*) DESC"
                        ).fetchall()
                    ]
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be queried.",
                    code="database_unavailable",
                ) from exc
        return {
            "available": True,
            "path": str(self.db_path),
            "open_mode": "read_only",
            "asset_count": count,
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
        "remote_network_allowed": False,
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
