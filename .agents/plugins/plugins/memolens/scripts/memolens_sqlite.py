"""Read-only SQLite snapshot policy and schema inspection."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
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
    "asset_review_revisions",
    "creator_profile_revisions",
    "database_meta",
}

_SQLITE_HEADER = b"SQLite format 3\x00"
_WAL_MAGIC = {0x377F0682, 0x377F0683}
_WAL_VERSION = 3_007_000
_SNAPSHOT_ATTEMPTS = 4
_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _FileSignature:
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int


class _SnapshotChanged(RuntimeError):
    """An optimistic raw snapshot raced with a database writer."""


class _SnapshotConnection(sqlite3.Connection):
    """SQLite connection that owns and removes its private snapshot."""

    _snapshot_owner: tempfile.TemporaryDirectory[str] | None = None

    def attach_snapshot(
        self, owner: tempfile.TemporaryDirectory[str]
    ) -> None:
        self._snapshot_owner = owner

    def close(self) -> None:
        owner = self._snapshot_owner
        self._snapshot_owner = None
        try:
            super().close()
        finally:
            if owner is not None:
                owner.cleanup()


class ReadOnlyDatabase:
    """Query a stable private snapshot without opening original SQLite sidecars."""

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

        snapshot_owner: tempfile.TemporaryDirectory[str] | None = None
        connection: _SnapshotConnection | None = None
        try:
            snapshot_path, snapshot_owner, live_wal = self._create_snapshot()
            query = "mode=ro" if live_wal else "mode=ro&immutable=1"
            uri = f"file:{quote(str(snapshot_path), safe='/:')}?{query}"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=5.0,
                factory=_SnapshotConnection,
            )
            connection.attach_snapshot(snapshot_owner)
            snapshot_owner = None
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            query_only = int(connection.execute("PRAGMA query_only").fetchone()[0])
            foreign_keys = int(
                connection.execute("PRAGMA foreign_keys").fetchone()[0]
            )
            busy_timeout = int(
                connection.execute("PRAGMA busy_timeout").fetchone()[0]
            )
            if query_only != 1 or foreign_keys != 1 or busy_timeout != 5000:
                raise sqlite3.OperationalError(
                    "SQLite read-only safety pragmas were not applied"
                )
            journal_mode = str(
                connection.execute("PRAGMA journal_mode").fetchone()[0]
            ).casefold()
            if live_wal and journal_mode != "wal":
                raise MemoLensError(
                    "The private MemoLens snapshot did not preserve its WAL.",
                    code="database_wal_unsafe",
                )
            return connection
        except MemoLensError:
            if connection is not None:
                connection.close()
            elif snapshot_owner is not None:
                snapshot_owner.cleanup()
            raise
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            elif snapshot_owner is not None:
                snapshot_owner.cleanup()
            raise MemoLensError(
                "The MemoLens SQLite index could not be opened from a private snapshot.",
                code="database_unavailable",
            ) from exc

    def _wal_path(self) -> Path:
        assert self.path is not None
        return Path(f"{self.path}-wal")

    @staticmethod
    def _signature_from_stat(result: os.stat_result) -> _FileSignature:
        return _FileSignature(
            device=result.st_dev,
            inode=result.st_ino,
            mode=result.st_mode,
            size=result.st_size,
            mtime_ns=result.st_mtime_ns,
            ctime_ns=result.st_ctime_ns,
        )

    @classmethod
    def _file_signature(
        cls,
        path: Path,
        *,
        missing_ok: bool,
        error_code: str,
    ) -> _FileSignature | None:
        try:
            result = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise MemoLensError(
                "The configured MemoLens SQLite index does not exist.",
                code="database_not_found",
            ) from None
        except OSError as exc:
            raise MemoLensError(
                "The MemoLens SQLite file state could not be inspected.",
                code=error_code,
            ) from exc
        if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
            raise MemoLensError(
                "The MemoLens SQLite file state is unsafe for snapshot access.",
                code=error_code,
            )
        return cls._signature_from_stat(result)

    def _create_snapshot(
        self,
    ) -> tuple[Path, tempfile.TemporaryDirectory[str], bool]:
        assert self.path is not None
        for _attempt in range(_SNAPSHOT_ATTEMPTS):
            owner = tempfile.TemporaryDirectory(prefix="memolens-sqlite-snapshot-")
            snapshot_path = Path(owner.name) / "memolens.db"
            try:
                live_wal = self._populate_snapshot(snapshot_path)
                return snapshot_path, owner, live_wal
            except _SnapshotChanged:
                owner.cleanup()
                continue
            except Exception:
                owner.cleanup()
                raise
        raise MemoLensError(
            "The MemoLens index changed while a private read snapshot was being prepared. Retry shortly.",
            code="database_snapshot_busy",
        )

    def _populate_snapshot(self, snapshot_path: Path) -> bool:
        assert self.path is not None
        database_before = self._file_signature(
            self.path,
            missing_ok=False,
            error_code="database_unavailable",
        )
        assert database_before is not None
        wal_path = self._wal_path()
        wal_before = self._file_signature(
            wal_path,
            missing_ok=True,
            error_code="database_wal_unsafe",
        )
        live_wal = bool(wal_before and wal_before.size > 0)

        self._copy_exact(self.path, snapshot_path, database_before)
        if live_wal:
            assert wal_before is not None
            self._copy_exact(
                wal_path,
                Path(f"{snapshot_path}-wal"),
                wal_before,
            )

        database_after = self._file_signature(
            self.path,
            missing_ok=False,
            error_code="database_unavailable",
        )
        wal_after = self._file_signature(
            wal_path,
            missing_ok=True,
            error_code="database_wal_unsafe",
        )
        if database_after != database_before or wal_after != wal_before:
            raise _SnapshotChanged

        page_size = self._validate_database_snapshot(snapshot_path)
        if live_wal:
            self._validate_wal_snapshot(Path(f"{snapshot_path}-wal"), page_size)
        return live_wal

    @classmethod
    def _copy_exact(
        cls,
        source_path: Path,
        destination_path: Path,
        expected: _FileSignature,
    ) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(source_path, flags)
        except FileNotFoundError:
            raise _SnapshotChanged from None
        except OSError as exc:
            raise MemoLensError(
                "The MemoLens SQLite snapshot source could not be opened safely.",
                code="database_wal_unsafe",
            ) from exc
        try:
            opened = cls._signature_from_stat(os.fstat(descriptor))
            if opened != expected or not stat.S_ISREG(opened.mode):
                raise _SnapshotChanged
            with os.fdopen(descriptor, "rb", closefd=False) as source, destination_path.open(
                "xb"
            ) as destination:
                os.chmod(destination_path, 0o600)
                while chunk := source.read(_COPY_CHUNK_BYTES):
                    destination.write(chunk)
            if cls._signature_from_stat(os.fstat(descriptor)) != expected:
                raise _SnapshotChanged
        finally:
            os.close(descriptor)

    @staticmethod
    def _page_size(encoded: int) -> int:
        page_size = 65_536 if encoded == 1 else encoded
        if not 512 <= page_size <= 65_536 or page_size & (page_size - 1):
            raise MemoLensError(
                "The MemoLens SQLite page size is invalid.",
                code="database_identity_mismatch",
            )
        return page_size

    @classmethod
    def _validate_database_snapshot(cls, path: Path) -> int:
        try:
            with path.open("rb") as database:
                header = database.read(100)
        except OSError as exc:
            raise MemoLensError(
                "The private MemoLens database snapshot could not be read.",
                code="database_unavailable",
            ) from exc
        if len(header) != 100 or header[:16] != _SQLITE_HEADER:
            raise MemoLensError(
                "The SQLite file is not a MemoLens-compatible database.",
                code="database_identity_mismatch",
            )
        if header[18:20] != b"\x02\x02":
            raise MemoLensError(
                "MemoLens requires a WAL-mode SQLite index for read-only access.",
                code="database_wal_required",
            )
        page_size = cls._page_size(int.from_bytes(header[16:18], "big"))
        try:
            snapshot_size = path.stat().st_size
        except OSError as exc:
            raise MemoLensError(
                "The private MemoLens database snapshot could not be inspected.",
                code="database_unavailable",
            ) from exc
        if snapshot_size < page_size or snapshot_size % page_size != 0:
            raise MemoLensError(
                "The MemoLens SQLite database snapshot is incomplete.",
                code="database_identity_mismatch",
            )
        return page_size

    @staticmethod
    def _checksum_words(
        payload: bytes,
        *,
        byte_order: str,
        seed: tuple[int, int] = (0, 0),
    ) -> tuple[int, int]:
        if len(payload) % 8 != 0:
            raise MemoLensError(
                "The MemoLens SQLite WAL checksum input is incomplete.",
                code="database_wal_unsafe",
            )
        first, second = seed
        for offset in range(0, len(payload), 8):
            left = int.from_bytes(payload[offset : offset + 4], byte_order)
            right = int.from_bytes(payload[offset + 4 : offset + 8], byte_order)
            first = (first + left + second) & 0xFFFFFFFF
            second = (second + right + first) & 0xFFFFFFFF
        return first, second

    @classmethod
    def _validate_wal_snapshot(cls, path: Path, database_page_size: int) -> None:
        try:
            wal_size = path.stat().st_size
            with path.open("rb") as wal:
                header = wal.read(32)
                if len(header) != 32:
                    raise MemoLensError(
                        "The MemoLens SQLite WAL header is incomplete.",
                        code="database_wal_unsafe",
                    )
                magic = int.from_bytes(header[0:4], "big")
                version = int.from_bytes(header[4:8], "big")
                page_size = cls._page_size(int.from_bytes(header[8:12], "big"))
                frame_size = 24 + page_size
                if (
                    magic not in _WAL_MAGIC
                    or version != _WAL_VERSION
                    or page_size != database_page_size
                    or wal_size < 32
                    or (wal_size - 32) % frame_size != 0
                ):
                    raise MemoLensError(
                        "The MemoLens SQLite WAL header is invalid.",
                        code="database_wal_unsafe",
                    )
                byte_order = "little" if magic == 0x377F0682 else "big"
                checksum = cls._checksum_words(
                    header[:24],
                    byte_order=byte_order,
                )
                stored_header_checksum = (
                    int.from_bytes(header[24:28], "big"),
                    int.from_bytes(header[28:32], "big"),
                )
                if checksum != stored_header_checksum:
                    raise MemoLensError(
                        "The MemoLens SQLite WAL header checksum is invalid.",
                        code="database_wal_unsafe",
                    )
                salts = header[16:24]
                remaining = wal_size - 32
                while remaining:
                    frame_header = wal.read(24)
                    page = wal.read(page_size)
                    if (
                        len(frame_header) != 24
                        or len(page) != page_size
                        or int.from_bytes(frame_header[0:4], "big") == 0
                        or frame_header[8:16] != salts
                    ):
                        raise MemoLensError(
                            "The MemoLens SQLite WAL frame is invalid.",
                            code="database_wal_unsafe",
                        )
                    checksum = cls._checksum_words(
                        frame_header[:8] + page,
                        byte_order=byte_order,
                        seed=checksum,
                    )
                    stored_frame_checksum = (
                        int.from_bytes(frame_header[16:20], "big"),
                        int.from_bytes(frame_header[20:24], "big"),
                    )
                    if checksum != stored_frame_checksum:
                        raise MemoLensError(
                            "The MemoLens SQLite WAL frame checksum is invalid.",
                            code="database_wal_unsafe",
                        )
                    remaining -= frame_size
        except MemoLensError:
            raise
        except OSError as exc:
            raise MemoLensError(
                "The private MemoLens WAL snapshot could not be validated.",
                code="database_wal_unsafe",
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
