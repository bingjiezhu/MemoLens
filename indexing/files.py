from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import ExifTags, Image, ImageOps

from core.schemas import LocalImageMetadata


GPS_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "GPSInfo")
DATE_TIME_ORIGINAL_TAG_ID = next(
    key for key, value in ExifTags.TAGS.items() if value == "DateTimeOriginal"
)
DATE_TIME_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "DateTime")


@dataclass
class PreparedImage:
    source_name: str
    image: Image.Image
    width: int
    height: int
    mime_type: str
    content_bytes: bytes


def extract_local_image_metadata(image_path: Path, library_root: Path) -> LocalImageMetadata:
    with Image.open(image_path) as image:
        width, height = image.size
        image_format = image.format
        exif = image.getexif() or {}

    gps_info = exif.get(GPS_TAG_ID)
    lat, lon, altitude = _parse_gps(gps_info)
    taken_at = _normalize_exif_datetime(
        exif.get(DATE_TIME_ORIGINAL_TAG_ID) or exif.get(DATE_TIME_TAG_ID)
    )
    sha256 = _sha256(image_path)
    mime_type = Image.MIME.get(image_format) or mimetypes.guess_type(image_path.name)[0]

    try:
        relative_path = str(image_path.resolve().relative_to(library_root.resolve()))
    except ValueError:
        relative_path = image_path.name

    return LocalImageMetadata(
        id=f"img_{sha256[:24]}",
        sha256=sha256,
        filename=image_path.name,
        relative_path=relative_path,
        mime_type=mime_type or "application/octet-stream",
        file_size=image_path.stat().st_size,
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


def prepare_image_for_modeling(image_path: Path, target_width: int) -> PreparedImage:
    with Image.open(image_path) as source:
        image = ImageOps.exif_transpose(source).convert("RGB")

    if image.width <= 0:
        raise ValueError(f"Invalid image width for {image_path}")

    if image.width != target_width:
        target_height = max(1, round(image.height * target_width / image.width))
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        image = image.resize((target_width, target_height), resampling)

    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=90)

    return PreparedImage(
        source_name=image_path.stem,
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


def _sha256(image_path: Path) -> str:
    digest = hashlib.sha256()
    with image_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
