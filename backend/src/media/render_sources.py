from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from core.media_db import MediaRepository, sha256_path

from .video import MediaCapabilityError, resolve_inside_root


SourceIdentity = tuple[int, int, int, int]
HashPath = Callable[[Path], str]


@dataclass(frozen=True)
class VerifiedSource:
    record: dict[str, object]
    path: Path
    identity: SourceIdentity


class SourceVerifier:
    """Verify each immutable source binding once while checking reuse identity."""

    def __init__(self, repository: MediaRepository, *, hash_path: HashPath = sha256_path):
        self.repository = repository
        self.hash_path = hash_path
        self._verified: dict[tuple[str, str], VerifiedSource] = {}

    def verify(self, clip: dict[str, object]) -> VerifiedSource:
        source = self.repository.get_asset_source(str(clip["asset_source_id"]))
        if not source or source.get("availability") != "available":
            raise MediaCapabilityError("source_unavailable", "A timeline source is unavailable.")

        source_path = resolve_inside_root(Path(str(source["root_path"])), str(source["relative_path"]))
        metadata = os.stat(source_path, follow_symlinks=False)
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        verification_key = (str(source["id"]), str(source["sha256"]))
        prior = self._verified.get(verification_key)
        if prior is not None and (prior.path, prior.identity) != (source_path, identity):
            self._mark_changed(source)
            raise MediaCapabilityError("source_changed", "A timeline source changed during rendering.")
        if prior is None and self.hash_path(source_path) != source["sha256"]:
            self._mark_changed(source)
            raise MediaCapabilityError("source_changed", "A timeline source changed after import.")

        verified = VerifiedSource(record=source, path=source_path, identity=identity)
        self._verified[verification_key] = verified
        return verified

    def _mark_changed(self, source: dict[str, object]) -> None:
        self.repository.mark_source_availability(str(source["id"]), "changed")


def freeze_still_image(source: Path, output: Path) -> None:
    """Normalize the first frame so stills and animated image formats share one contract."""
    with Image.open(source) as image:
        try:
            image.seek(0)
        except EOFError:
            pass
        ImageOps.exif_transpose(image).convert("RGB").save(output, "PNG")
