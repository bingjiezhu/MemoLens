from __future__ import annotations

import base64
import hashlib
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

from core.schemas import LocalImageMetadata


def ensure_heif_support() -> None:
    """Register HEIC/HEIF openers when pillow-heif is installed."""
    try:
        import pillow_heif
    except ImportError:
        return
    pillow_heif.register_heif_opener()


ensure_heif_support()

GPS_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "GPSInfo")
DATE_TIME_ORIGINAL_TAG_ID = next(
    key for key, value in ExifTags.TAGS.items() if value == "DateTimeOriginal"
)
DATE_TIME_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "DateTime")
MAX_LOCAL_IMAGE_BYTES = 128 * 1024 * 1024


def _source_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _relative_source_path(value: Path) -> Path:
    if value.is_absolute() or not value.parts or any(part in {"", ".", ".."} for part in value.parts):
        raise ValueError("Image source must be one normalized library-relative path.")
    return value


def _secure_directory_flags() -> int:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise RuntimeError("Secure image source opening is unavailable on this platform.")
    return os.O_RDONLY | directory | nofollow | getattr(os, "O_CLOEXEC", 0)


def _open_absolute_directory(library_root: Path) -> tuple[Path, int]:
    """Open every absolute root component without following a symlink."""

    directory_flags = _secure_directory_flags()
    # Normalize trusted configuration/native-selection aliases (notably
    # macOS /var -> /private/var) before component-wise O_NOFOLLOW traversal.
    # A supplied native dev/ino binding is checked on the resulting fd.
    root_path = Path(os.path.realpath(os.path.abspath(library_root.expanduser())))
    if not root_path.is_absolute():  # pragma: no cover - abspath is absolute
        raise ValueError("Image library root must be absolute.")
    root_fd = os.open(root_path.anchor, directory_flags)
    try:
        for component in root_path.parts[1:]:
            next_fd = os.open(component, directory_flags, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = next_fd
    except Exception:
        os.close(root_fd)
        raise
    metadata = os.fstat(root_fd)
    if not stat.S_ISDIR(metadata.st_mode):  # pragma: no cover - O_DIRECTORY enforces this
        os.close(root_fd)
        raise ValueError("Image library root must be a directory.")
    return root_path, root_fd


class PinnedLibraryRoot:
    """One root directory object held open for a complete indexing request."""

    def __init__(self, *, root_path: Path, root_fd: int) -> None:
        self.root_path = root_path
        self.root_fd = root_fd
        self.metadata = os.fstat(root_fd)
        self.identity = _directory_identity(self.metadata)
        self._closed = False

    def assert_expected_identity(
        self,
        expected_identity: tuple[int, int] | None,
    ) -> None:
        if expected_identity is not None and self.identity != expected_identity:
            raise PermissionError(
                "Image library root does not match the native-approved filesystem identity."
            )

    def duplicate_fd(self) -> int:
        if self._closed:
            raise ValueError("Image library root is no longer pinned.")
        return os.dup(self.root_fd)

    def open_relative_regular(self, relative_path: Path) -> int:
        """Open one regular file relative to the pinned root fd."""

        if self._closed:
            raise ValueError("Image library root is no longer pinned.")
        relative_path = _relative_source_path(relative_path)
        directory_flags = _secure_directory_flags()
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:  # pragma: no cover - checked by directory flags
            raise RuntimeError("Secure image source opening is unavailable on this platform.")
        file_flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        current_fd = self.duplicate_fd()
        file_fd: int | None = None
        try:
            for component in relative_path.parts[:-1]:
                next_fd = os.open(component, directory_flags, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            file_fd = os.open(relative_path.parts[-1], file_flags, dir_fd=current_fd)
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Image source must be a regular non-symlink file.")
            return file_fd
        except Exception:
            if file_fd is not None:
                os.close(file_fd)
            raise
        finally:
            os.close(current_fd)

    def verify_current_path_identity(self) -> None:
        """Prove the configured path still names this held root object."""

        if self._closed:
            raise ValueError("Image library root is no longer pinned.")
        current_path, current_fd = _open_absolute_directory(self.root_path)
        try:
            if current_path != self.root_path:
                raise ValueError("Image library root path changed while indexing was active.")
            if _directory_identity(os.fstat(current_fd)) != self.identity:
                raise ValueError("Image library root changed while indexing was active.")
        finally:
            os.close(current_fd)

    def close(self) -> None:
        if not self._closed:
            os.close(self.root_fd)
            self._closed = True

    def __enter__(self) -> "PinnedLibraryRoot":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def open_pinned_library_root(
    library_root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> PinnedLibraryRoot:
    root_path, root_fd = _open_absolute_directory(library_root)
    try:
        pinned = PinnedLibraryRoot(root_path=root_path, root_fd=root_fd)
        pinned.assert_expected_identity(expected_identity)
        return pinned
    except Exception:
        os.close(root_fd)
        raise


def collect_pinned_library_candidates(
    pinned_root: PinnedLibraryRoot,
    *,
    recursive: bool,
) -> list[Path]:
    """Enumerate from fd-relative directory handles without following links."""

    directory_flags = _secure_directory_flags()
    candidates: list[Path] = []
    pending: list[tuple[Path, int]] = [(Path(), pinned_root.duplicate_fd())]
    try:
        while pending:
            relative_directory, directory_fd = pending.pop()
            try:
                names = sorted(os.listdir(directory_fd))
                for name in names:
                    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
                        raise ValueError("Image library contains an invalid directory entry.")
                    relative_path = relative_directory / name
                    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                        if is_supported_image(relative_path):
                            candidates.append(relative_path)
                        continue
                    if recursive and stat.S_ISDIR(metadata.st_mode):
                        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
                        if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                            os.close(child_fd)
                            raise ValueError("Image library directory binding changed during scan.")
                        pending.append((relative_path, child_fd))
            finally:
                os.close(directory_fd)
    except Exception:
        for _, pending_fd in pending:
            os.close(pending_fd)
        raise
    return sorted(candidates)


class PinnedLocalImageSource:
    """One bounded source snapshot held open across analysis and persistence."""

    def __init__(
        self,
        *,
        pinned_root: PinnedLibraryRoot,
        owns_root: bool,
        relative_path: Path,
        file_fd: int,
        metadata: os.stat_result,
        content_bytes: bytes,
    ) -> None:
        self.pinned_root = pinned_root
        self.library_root = pinned_root.root_path
        self._owns_root = owns_root
        self.relative_path = _relative_source_path(relative_path)
        self.file_fd = file_fd
        self.root_identity = pinned_root.identity
        self.metadata = metadata
        self.identity = _source_identity(metadata)
        self.content_bytes = content_bytes
        self._closed = False

    @property
    def filename(self) -> str:
        return self.relative_path.name

    def verify_current_identity(self) -> None:
        """Prove both the held inode and current root binding are unchanged."""

        if self._closed:
            raise ValueError("Image source is no longer pinned.")
        if _source_identity(os.fstat(self.file_fd)) != self.identity:
            raise ValueError("Image source changed while it was being analyzed.")

        self.pinned_root.verify_current_path_identity()
        current_fd = self.pinned_root.open_relative_regular(self.relative_path)
        try:
            if self.pinned_root.identity != self.root_identity:
                raise ValueError("Image library root changed while indexing was active.")
            if _source_identity(os.fstat(current_fd)) != self.identity:
                raise ValueError("Image source binding changed while it was being analyzed.")
        finally:
            os.close(current_fd)

    def close(self) -> None:
        if not self._closed:
            os.close(self.file_fd)
            self._closed = True
            if self._owns_root:
                self.pinned_root.close()

    def __enter__(self) -> "PinnedLocalImageSource":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def open_pinned_local_image(
    *,
    library_root: Path | None = None,
    pinned_root: PinnedLibraryRoot | None = None,
    relative_path: Path,
    max_bytes: int = MAX_LOCAL_IMAGE_BYTES,
) -> PinnedLocalImageSource:
    """Read bounded bytes from one O_NOFOLLOW descriptor and keep it pinned."""

    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("Image source byte limit must be positive.")
    if (library_root is None) == (pinned_root is None):
        raise ValueError("Provide exactly one pinned or path-based image library root.")
    owns_root = pinned_root is None
    root = pinned_root or open_pinned_library_root(library_root or Path("."))
    file_fd = root.open_relative_regular(relative_path)
    try:
        before = os.fstat(file_fd)
        if before.st_size > max_bytes:
            raise ValueError("Image source exceeds the bounded local byte limit.")
        os.lseek(file_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Image source exceeds the bounded local byte limit.")
        after = os.fstat(file_fd)
        if _source_identity(after) != _source_identity(before) or total != before.st_size:
            raise ValueError("Image source changed while its bounded snapshot was read.")
        return PinnedLocalImageSource(
            pinned_root=root,
            owns_root=owns_root,
            relative_path=relative_path,
            file_fd=file_fd,
            metadata=before,
            content_bytes=b"".join(chunks),
        )
    except Exception:
        os.close(file_fd)
        if owns_root:
            root.close()
        raise


@dataclass
class PreparedImage:
    source_name: str
    image: Image.Image
    width: int
    height: int
    mime_type: str
    content_bytes: bytes


def extract_local_image_metadata(source: PinnedLocalImageSource) -> LocalImageMetadata:
    with Image.open(BytesIO(source.content_bytes)) as image:
        width, height = image.size
        image_format = image.format
        exif = image.getexif() or {}

    gps_info = exif.get(GPS_TAG_ID)
    lat, lon, altitude = _parse_gps(gps_info)
    taken_at = _normalize_exif_datetime(
        exif.get(DATE_TIME_ORIGINAL_TAG_ID) or exif.get(DATE_TIME_TAG_ID)
    )
    sha256 = _sha256_bytes(source.content_bytes)
    mime_type = Image.MIME.get(image_format) or mimetypes.guess_type(source.filename)[0]

    return LocalImageMetadata(
        id=f"img_{sha256[:24]}",
        sha256=sha256,
        filename=source.filename,
        relative_path=str(source.relative_path),
        mime_type=mime_type or "application/octet-stream",
        file_size=len(source.content_bytes),
        width=width,
        height=height,
        taken_at=taken_at,
        lat=lat,
        lon=lon,
        altitude=altitude,
    )


def extract_uploaded_image_metadata(
    *,
    content_bytes: bytes,
    filename: str,
    relative_path: str | None = None,
    mime_type: str | None = None,
) -> LocalImageMetadata:
    with Image.open(BytesIO(content_bytes)) as image:
        width, height = image.size
        image_format = image.format
        exif = image.getexif() or {}

    gps_info = exif.get(GPS_TAG_ID)
    lat, lon, altitude = _parse_gps(gps_info)
    taken_at = _normalize_exif_datetime(
        exif.get(DATE_TIME_ORIGINAL_TAG_ID) or exif.get(DATE_TIME_TAG_ID)
    )
    sha256 = _sha256_bytes(content_bytes)
    resolved_mime_type = mime_type or Image.MIME.get(image_format) or mimetypes.guess_type(filename)[0]

    return LocalImageMetadata(
        id=f"img_{sha256[:24]}",
        sha256=sha256,
        filename=filename,
        relative_path=relative_path or filename,
        mime_type=resolved_mime_type or "application/octet-stream",
        file_size=len(content_bytes),
        width=width,
        height=height,
        taken_at=taken_at,
        lat=lat,
        lon=lon,
        altitude=altitude,
    )


def prepare_image_for_modeling(
    source: PinnedLocalImageSource,
    target_width: int,
) -> PreparedImage:
    with Image.open(BytesIO(source.content_bytes)) as image_source:
        image = ImageOps.exif_transpose(image_source).convert("RGB")

    if image.width <= 0:
        raise ValueError(f"Invalid image width for {source.filename}")

    if image.width != target_width:
        target_height = max(1, round(image.height * target_width / image.width))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((target_width, target_height), resampling)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)

    return PreparedImage(
        source_name=source.relative_path.stem,
        image=image,
        width=image.width,
        height=image.height,
        mime_type="image/jpeg",
        content_bytes=buffer.getvalue(),
    )


def prepare_uploaded_image_for_modeling(
    *,
    content_bytes: bytes,
    filename: str,
    target_width: int,
) -> PreparedImage:
    with Image.open(BytesIO(content_bytes)) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    if image.width <= 0:
        raise ValueError(f"Invalid image width for {filename}")

    if image.width != target_width:
        target_height = max(1, round(image.height * target_width / image.width))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((target_width, target_height), resampling)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)

    return PreparedImage(
        source_name=Path(filename).stem,
        image=image,
        width=image.width,
        height=image.height,
        mime_type="image/jpeg",
        content_bytes=buffer.getvalue(),
    )


def is_supported_image(path: Path) -> bool:
    return path.suffix.lower() in {
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
        ".bmp",
        ".gif",
        ".tif",
        ".tiff",
        ".heic",
        ".heif",
    }


def decode_base64_image(payload: str) -> tuple[bytes, str | None]:
    text = payload.strip()
    mime_type = None

    match = re.match(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", text, re.DOTALL)
    if match:
        mime_type = match.group("mime").strip()
        text = match.group("data").strip()

    try:
        content_bytes = base64.b64decode(text, validate=True)
    except Exception as exc:
        raise ValueError("Invalid base64 image payload.") from exc

    return content_bytes, mime_type


def _sha256_bytes(content_bytes: bytes) -> str:
    return hashlib.sha256(content_bytes).hexdigest()


def _normalize_exif_datetime(raw_value) -> str | None:
    if not raw_value:
        return None

    text = str(raw_value).strip()
    if not text:
        return None

    match = re.fullmatch(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", text)
    if not match:
        return text

    parsed = datetime(
        year=int(match.group(1)),
        month=int(match.group(2)),
        day=int(match.group(3)),
        hour=int(match.group(4)),
        minute=int(match.group(5)),
        second=int(match.group(6)),
    )
    return parsed.isoformat()


def _parse_gps(gps_info) -> tuple[float | None, float | None, float | None]:
    if not gps_info:
        return None, None, None

    normalized = {
        ExifTags.GPSTAGS.get(tag_id, tag_id): value for tag_id, value in dict(gps_info).items()
    }

    lat = _to_degrees(normalized.get("GPSLatitude"), normalized.get("GPSLatitudeRef"))
    lon = _to_degrees(normalized.get("GPSLongitude"), normalized.get("GPSLongitudeRef"))
    altitude = _to_float(normalized.get("GPSAltitude"))

    return lat, lon, altitude


def _to_degrees(value, ref) -> float | None:
    if not value or not ref:
        return None

    try:
        degrees = _to_float(value[0])
        minutes = _to_float(value[1])
        seconds = _to_float(value[2])
    except (IndexError, TypeError, ZeroDivisionError):
        return None

    decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
    if str(ref).upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def _to_float(value) -> float | None:
    if value is None:
        return None

    if isinstance(value, tuple) and len(value) == 2:
        numerator, denominator = value
        if denominator == 0:
            return None
        return float(numerator) / float(denominator)

    numerator = getattr(value, "numerator", None)
    denominator = getattr(value, "denominator", None)
    if numerator is not None and denominator is not None:
        if denominator == 0:
            return None
        return float(numerator) / float(denominator)

    try:
        return float(value)
    except (TypeError, ValueError):
        return None
