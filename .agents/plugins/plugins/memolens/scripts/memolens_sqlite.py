"""Read-only SQLite connection policy and schema inspection."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from memolens_contracts import MemoLensError


_KNOWN_RELATIONS = {
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
}


class ReadOnlyDatabase:
    """Open only immutable-style query connections to one configured index."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def configure(self, path: Path | None) -> None:
        self.path = path

    def connection(self) -> sqlite3.Connection:
        if self.path is None:
            raise MemoLensError(
                "No MemoLens SQLite index was discovered. Set MEMOLENS_DB_PATH or open MemoLens.",
                code="database_not_configured",
            )
        if not self.path.is_file():
            raise MemoLensError(
                f"MemoLens SQLite index does not exist: {self.path}",
                code="database_not_found",
            )
        uri = f"file:{quote(str(self.path), safe='/:')}?mode=ro"
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
    def relation_exists(connection: sqlite3.Connection, relation: str) -> bool:
        if relation not in _KNOWN_RELATIONS:
            return False
        try:
            return connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type IN ('table', 'view') AND name = ?",
                (relation,),
            ).fetchone() is not None
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite schema could not be read.",
                code="database_unavailable",
            ) from exc

    def columns(self, connection: sqlite3.Connection, relation: str) -> set[str]:
        if not self.relation_exists(connection, relation):
            return set()
        try:
            return {
                str(row["name"])
                for row in connection.execute(
                    f"PRAGMA table_info({relation})"
                ).fetchall()
            }
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens SQLite schema could not be read.",
                code="database_unavailable",
            ) from exc
