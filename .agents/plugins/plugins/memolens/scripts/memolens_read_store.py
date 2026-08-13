"""Facade over cohesive read-only MemoLens SQLite repositories."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from memolens_contracts import MemoLensError
from memolens_creator_store import CreatorMemoryReader
from memolens_media_store import MediaIndexReader
from memolens_photo_store import PhotoIndexReader
from memolens_sqlite import ReadOnlyDatabase
from memolens_timeline_store import TimelineIndexReader


class ReadOnlyMemoLensStore:
    """Coordinate one read-only database across photo, media, and timeline readers."""

    def __init__(self, db_path: Path | None, library_dir: Path | None) -> None:
        self.database = ReadOnlyDatabase(db_path)
        self.photos = PhotoIndexReader(self.database, library_dir)
        self.media = MediaIndexReader(self.database)
        self.timelines = TimelineIndexReader(self.database)
        self.creator = CreatorMemoryReader(self.database)

    @property
    def db_path(self) -> Path | None:
        return self.database.path

    def configure(self, *, db_path: Path | None, library_dir: Path | None) -> None:
        self.database.configure(db_path)
        self.photos.configure_library(library_dir)

    def connection(self) -> sqlite3.Connection:
        return self.database.connection()

    def status(self) -> dict[str, Any]:
        with closing(self.database.connection()) as connection:
            image_columns = self.database.columns(connection, "image_index")
            asset_columns = self.database.columns(connection, "assets")
            self.photos.validate_identity(image_columns)
            self.media.validate_identity(asset_columns)
            if not image_columns and not asset_columns:
                raise MemoLensError(
                    "The SQLite file is not a MemoLens media index.",
                    code="database_identity_mismatch",
                )
            source_columns = self.database.columns(connection, "asset_sources")
            segment_columns = self.database.columns(connection, "video_segments")
            try:
                counts = self._status_counts(
                    connection,
                    image_columns=image_columns,
                    asset_columns=asset_columns,
                    source_columns=source_columns,
                    segment_columns=segment_columns,
                )
                creator_capabilities = self.creator.schema_capabilities(connection)
                creator_capabilities["inbox_available"] = bool(
                    creator_capabilities["inbox_available"]
                    and self.media.schema_available(asset_columns, source_columns)
                )
                counts.update(creator_capabilities)
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "The MemoLens SQLite index could not be queried.",
                    code="database_unavailable",
                ) from exc
        return {
            "available": True,
            "open_mode": "read_only",
            **counts,
            "legacy_search_available": bool(image_columns),
            "media_schema_available": self.media.schema_available(
                asset_columns, source_columns
            ),
        }

    def _status_counts(
        self,
        connection: sqlite3.Connection,
        *,
        image_columns: set[str],
        asset_columns: set[str],
        source_columns: set[str],
        segment_columns: set[str],
    ) -> dict[str, Any]:
        image_count = self.photos.count(connection, image_columns)
        kind_counts = self.media.kind_counts(connection, asset_columns)
        asset_count = sum(kind_counts.values()) if asset_columns else image_count
        video_mode, video_available, video_count = self.media.video_status(
            connection,
            asset_columns=asset_columns,
            source_columns=source_columns,
            segment_columns=segment_columns,
        )
        timeline_schema = self.timelines.schema(connection)
        timeline_count, revision_count = self.timelines.counts(
            connection, timeline_schema
        )
        return {
            "asset_count": asset_count,
            "legacy_image_count": image_count,
            "asset_kind_counts": kind_counts,
            "video_segment_count": video_count,
            "timeline_count": timeline_count,
            "timeline_revision_count": revision_count,
            "video_search_available": video_available,
            "video_schema_mode": video_mode,
            "timeline_read_available": timeline_schema is not None,
            "embedding_backends": self.photos.embedding_backends(
                connection, image_columns
            ),
        }

    def search(self, query: str, limit: int) -> dict[str, Any]:
        return self.photos.search(query, limit)

    def mixed_image_search(self, query: str, limit: int) -> dict[str, Any]:
        return self.photos.search(
            query,
            limit,
            require_media_provenance=True,
            include_paths=False,
        )

    def media_list(
        self, *, kinds: list[str], limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.media.list(kinds=kinds, limit=limit, cursor=cursor)

    def media_get(self, asset_id: str) -> dict[str, Any]:
        return self.media.get(asset_id)

    def video_search(self, query: str, limit: int) -> dict[str, Any]:
        return self.media.video_search(query, limit)

    def creator_context(self) -> dict[str, Any]:
        return self.creator.creator_context()

    def inbox_list(
        self,
        *,
        state: str,
        kinds: list[str],
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        return self.creator.inbox_list(
            state=state,
            kinds=kinds,
            limit=limit,
            cursor=cursor,
        )

    def timeline_list(
        self, *, project_id: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.timelines.list(
            project_id=project_id,
            limit=limit,
            cursor=cursor,
        )

    def timeline_get(
        self, timeline_id: str, revision: int | None
    ) -> dict[str, Any]:
        return self.timelines.get(timeline_id, revision)
