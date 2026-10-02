from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Sequence

from indexing.files import PinnedLibraryRoot, open_pinned_library_root


SOURCE_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "FileIdentity":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
            ctime_ns=int(value.st_ctime_ns),
        )


def pinned_root_permission_fingerprint(root: PinnedLibraryRoot) -> str:
    metadata = os.fstat(root.root_fd)
    material = (
        f"{root.root_path}\0{metadata.st_dev}\0{metadata.st_ino}\0{metadata.st_mode}"
    )
    return hashlib.sha256(material.encode()).hexdigest()


def normalized_relative_path(value: str | Path) -> Path:
    relative = Path(value)
    if (
        not str(value)
        or "\x00" in str(value)
        or relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("Media source must be one normalized library-relative path.")
    return relative


def descriptor_sha256(descriptor: int) -> str:
    digest = hashlib.sha256()
    with os.fdopen(os.dup(descriptor), "rb") as handle:
        handle.seek(0)
        for chunk in iter(lambda: handle.read(SOURCE_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def descriptor_handle(descriptor: int) -> BinaryIO:
    handle = os.fdopen(os.dup(descriptor), "rb")
    handle.seek(0)
    return handle


def descriptor_input_path(descriptor: int) -> Path:
    """Return the child-process view of one already-open source inode.

    MemoLens currently ships the local media worker on macOS.  Failing closed
    elsewhere is preferable to falling back to the mutable source pathname.
    """

    if os.name != "posix" or not Path("/dev/fd").is_dir():
        raise RuntimeError("Descriptor-backed media input is unavailable on this platform.")
    return Path("/dev/fd") / str(descriptor)


class PinnedMediaSource:
    """One library-relative regular file pinned through a complete operation."""

    def __init__(
        self,
        *,
        root: PinnedLibraryRoot,
        owns_root: bool,
        relative_path: Path,
        descriptor: int,
    ) -> None:
        self.root = root
        self.owns_root = owns_root
        self.relative_path = normalized_relative_path(relative_path)
        self.descriptor = descriptor
        self.identity = FileIdentity.from_stat(os.fstat(descriptor))
        self._closed = False

    @property
    def display_path(self) -> Path:
        return self.root.root_path / self.relative_path

    @property
    def child_input_path(self) -> Path:
        if self._closed:
            raise ValueError("Media source is no longer pinned.")
        return descriptor_input_path(self.descriptor)

    @property
    def pass_fds(self) -> tuple[int, ...]:
        if self._closed:
            raise ValueError("Media source is no longer pinned.")
        return (self.descriptor,)

    def open_handle(self) -> BinaryIO:
        if self._closed:
            raise ValueError("Media source is no longer pinned.")
        return descriptor_handle(self.descriptor)

    def sha256(self) -> str:
        if self._closed:
            raise ValueError("Media source is no longer pinned.")
        return descriptor_sha256(self.descriptor)

    def verify_current_identity(self) -> None:
        """Verify held inode plus the root-relative name that admitted it."""

        if self._closed:
            raise ValueError("Media source is no longer pinned.")
        if FileIdentity.from_stat(os.fstat(self.descriptor)) != self.identity:
            raise ValueError("Media source changed while it was in use.")
        self.root.verify_current_path_identity()
        current = self.root.open_relative_regular(self.relative_path)
        try:
            if FileIdentity.from_stat(os.fstat(current)) != self.identity:
                raise ValueError("Media source binding changed while it was in use.")
        finally:
            os.close(current)

    def close(self) -> None:
        if self._closed:
            return
        os.close(self.descriptor)
        self._closed = True
        if self.owns_root:
            self.root.close()

    def __enter__(self) -> "PinnedMediaSource":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def open_pinned_media_source(
    *,
    relative_path: str | Path,
    library_root: Path | None = None,
    pinned_root: PinnedLibraryRoot | None = None,
    expected_identity: FileIdentity | None = None,
    max_bytes: int | None = None,
) -> PinnedMediaSource:
    if (library_root is None) == (pinned_root is None):
        raise ValueError("Provide exactly one path-based or pinned library root.")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError("Media source byte limit must be a positive integer.")
    owns_root = pinned_root is None
    root = pinned_root or open_pinned_library_root(library_root or Path("."))
    descriptor: int | None = None
    try:
        descriptor = root.open_relative_regular(normalized_relative_path(relative_path))
        metadata = os.fstat(descriptor)
        identity = FileIdentity.from_stat(metadata)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Media source must be a regular non-symlink file.")
        if max_bytes is not None and identity.size > max_bytes:
            raise ValueError("Media source exceeds the bounded local byte limit.")
        if expected_identity is not None and identity != expected_identity:
            raise ValueError("Media source identity changed after admission.")
        return PinnedMediaSource(
            root=root,
            owns_root=owns_root,
            relative_path=normalized_relative_path(relative_path),
            descriptor=descriptor,
        )
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if owns_root:
            root.close()
        raise


def collect_media_relative_paths(
    root: PinnedLibraryRoot,
    *,
    recursive: bool,
    files: Sequence[str] | None,
    extensions: set[str],
    max_files: int,
    max_total_bytes: int,
    deadline: float,
    clock,
) -> list[Path]:
    """Enumerate from directory descriptors without following any component."""

    def require_time() -> None:
        if clock() > deadline:
            raise ValueError("import_manifest_timeout: media discovery exceeded its time budget.")

    if files is not None:
        candidates: Iterable[Path] = [normalized_relative_path(value) for value in files]
    else:
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        discovered: list[Path] = []
        pending: list[tuple[Path, int]] = [(Path(), root.duplicate_fd())]
        try:
            while pending:
                relative_directory, directory_fd = pending.pop()
                try:
                    require_time()
                    for name in sorted(os.listdir(directory_fd)):
                        if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                            raise ValueError("Media library contains an invalid directory entry.")
                        relative = relative_directory / name
                        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                        if stat.S_ISREG(metadata.st_mode):
                            if relative.suffix.casefold() in extensions:
                                discovered.append(relative)
                            continue
                        if recursive and stat.S_ISDIR(metadata.st_mode):
                            child = os.open(name, directory_flags, dir_fd=directory_fd)
                            pending.append((relative, child))
                finally:
                    os.close(directory_fd)
        except Exception:
            for _, descriptor in pending:
                os.close(descriptor)
            raise
        candidates = discovered

    accepted: list[Path] = []
    total_bytes = 0
    for relative in candidates:
        require_time()
        if relative.suffix.casefold() not in extensions:
            continue
        try:
            with open_pinned_media_source(
                pinned_root=root,
                relative_path=relative,
                max_bytes=max_total_bytes,
            ) as source:
                total_bytes += source.identity.size
        except (OSError, ValueError):
            # Discovery is intentionally tolerant.  Explicit entries are later
            # reported as rejected by preparation if they disappear or rebind.
            continue
        accepted.append(relative)
        if len(accepted) > max_files:
            raise ValueError("import_manifest_too_large: too many supported files.")
        if total_bytes > max_total_bytes:
            raise ValueError("import_manifest_too_large: supported files exceed the byte budget.")
    return sorted(accepted)
