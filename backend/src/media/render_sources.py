from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from PIL import Image, ImageOps

from core.media_db import MediaRepository

from .source_identity import PinnedMediaSource, open_pinned_media_source
from .video import MediaCapabilityError


SourceIdentity = tuple[int, int, int, int]
HashPath = Callable[[Path], str]


@dataclass(frozen=True)
class VerifiedSource:
    record: dict[str, object]
    path: Path
    identity: SourceIdentity
    pinned: PinnedMediaSource

    @property
    def input_path(self) -> Path:
        return self.pinned.child_input_path

    @property
    def pass_fds(self) -> tuple[int, ...]:
        return self.pinned.pass_fds

    def open_handle(self) -> BinaryIO:
        return self.pinned.open_handle()

    def verify_current_identity(self) -> None:
        self.pinned.verify_current_identity()


class SourceVerifier:
    """Verify each immutable source binding once while checking reuse identity."""

    def __init__(
        self,
        repository: MediaRepository,
        *,
        hash_path: HashPath | None = None,
        mark_changed: bool = True,
    ):
        self.repository = repository
        self.hash_path = hash_path
        self.mark_changed = mark_changed
        self._verified: dict[tuple[str, str], VerifiedSource] = {}

    def verify(self, clip: dict[str, object]) -> VerifiedSource:
        source = self.repository.get_asset_source(str(clip["asset_source_id"]))
        if not source or source.get("availability") != "available":
            raise MediaCapabilityError("source_unavailable", "A timeline source is unavailable.")

        verification_key = (str(source["id"]), str(source["sha256"]))
        prior = self._verified.get(verification_key)
        if prior is not None:
            try:
                prior.verify_current_identity()
                return prior
            except (OSError, ValueError):
                self._mark_changed(source)
                raise MediaCapabilityError(
                    "source_changed",
                    "A timeline source changed during rendering.",
                ) from None

        pinned: PinnedMediaSource | None = None
        source_path = Path(str(source["root_path"])) / str(source["relative_path"])
        try:
            pinned = open_pinned_media_source(
                library_root=Path(str(source["root_path"])),
                relative_path=str(source["relative_path"]),
            )
            source_path = pinned.display_path
            identity = (
                pinned.identity.device,
                pinned.identity.inode,
                pinned.identity.size,
                pinned.identity.mtime_ns,
            )
            if "source_file_id" in source and str(source.get("source_file_id") or "") != str(
                pinned.identity.inode
            ):
                raise ValueError("Persisted source inode changed.")
            if "observed_size" in source and int(source.get("observed_size") or -1) != pinned.identity.size:
                raise ValueError("Persisted source size changed.")
            if "observed_mtime_ns" in source and int(
                source.get("observed_mtime_ns") or -1
            ) != pinned.identity.mtime_ns:
                raise ValueError("Persisted source timestamp changed.")
            # ``hash_path`` remains only as an explicit test seam.  Production
            # callers hash the held descriptor and never reopen source_path.
            digest = self.hash_path(source_path) if self.hash_path is not None else pinned.sha256()
            if digest != source["sha256"]:
                raise ValueError("Persisted source digest changed.")
            pinned.verify_current_identity()
            verified = VerifiedSource(
                record=source,
                path=source_path,
                identity=identity,
                pinned=pinned,
            )
            self._verified[verification_key] = verified
            return verified
        except (OSError, ValueError, RuntimeError):
            if pinned is not None:
                pinned.close()
            self._mark_changed(source)
            raise MediaCapabilityError(
                "source_changed",
                "A timeline source changed after import.",
            ) from None

    def verify_all(self) -> None:
        for source in self._verified.values():
            try:
                source.verify_current_identity()
            except (OSError, ValueError):
                self._mark_changed(source.record)
                raise MediaCapabilityError(
                    "source_changed",
                    "A timeline source changed during rendering.",
                ) from None

    def close(self) -> None:
        for source in self._verified.values():
            source.pinned.close()
        self._verified.clear()

    def __enter__(self) -> "SourceVerifier":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    def _mark_changed(self, source: dict[str, object]) -> None:
        if self.mark_changed:
            self.repository.mark_source_availability(str(source["id"]), "changed")


def freeze_still_image(source: Path | BinaryIO, output: Path) -> None:
    """Normalize the first frame so stills and animated image formats share one contract."""
    with Image.open(source) as image:
        try:
            image.seek(0)
        except EOFError:
            pass
        ImageOps.exif_transpose(image).convert("RGB").save(output, "PNG")
