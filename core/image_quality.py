from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
import math
from pathlib import Path
import re

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


QUALITY_MODEL_ID = "local_technical_aesthetic_v1"

WIDE_SCENE_TERMS = [
    "wide shot",
    "panoramic",
    "panorama",
    "vista",
    "landscape",
    "vast",
    "range",
    "valley",
    "meadow",
    "sky",
]
PRIMARY_WIDE_SCENE_TERMS = [
    "wide shot",
    "panoramic",
    "panorama",
    "vista",
    "vast",
]
CLOSE_FOCUS_TERMS = [
    "close up",
    "closeup",
    "macro",
    "detail",
    "tree trunk",
    "path",
    "road",
]
QUALITY_POSITIVE_TERMS = [
    "wide shot",
    "panoramic",
    "panorama",
    "vista",
    "scenic",
    "majestic",
    "clear sky",
    "natural light",
    "golden light",
    "soft light",
    "dramatic",
    "foreground",
    "background",
    "symmetry",
    "reflection",
    "waterfall",
]
QUALITY_NEGATIVE_TERMS = [
    "blurry",
    "blurred",
    "motion blur",
    "dark",
    "low light",
    "overexposed",
    "underexposed",
    "obstructed",
    "cluttered",
    "close up",
    "closeup",
    "tree trunk",
]


@dataclass(frozen=True)
class ImageQualityScores:
    aesthetic_score: float
    technical_quality_score: float | None
    model: str = QUALITY_MODEL_ID


def normalize_quality_text(value: object) -> str:
    lowered = re.sub(r"[^a-z0-9]+", " ", str(value or "").strip().lower())
    return " ".join(token for token in lowered.split() if token)


def scene_composition_score(normalized_blob: str) -> float:
    score = 0.0
    primary_wide_hit = any(term in normalized_blob for term in PRIMARY_WIDE_SCENE_TERMS)
    close_focus_hit = any(term in normalized_blob for term in CLOSE_FOCUS_TERMS)
    for term in WIDE_SCENE_TERMS:
        if term in normalized_blob:
            score += 0.35
    for term in CLOSE_FOCUS_TERMS:
        if term in normalized_blob:
            score -= 0.45
    if close_focus_hit and not primary_wide_hit:
        score = min(score, -0.75)
    return max(-1.0, min(1.0, score))


def metadata_quality_score(normalized_blob: str) -> float:
    score = 0.5
    for term in QUALITY_POSITIVE_TERMS:
        if term in normalized_blob:
            score += 0.055
    for term in QUALITY_NEGATIVE_TERMS:
        if term in normalized_blob:
            score -= 0.075
    score += 0.08 * scene_composition_score(normalized_blob)
    return max(0.0, min(1.0, score))


def combine_quality_scores(
    *,
    metadata_score: float,
    technical_score: float | None,
) -> float:
    bounded_metadata = max(0.0, min(1.0, metadata_score))
    if technical_score is None:
        return bounded_metadata
    bounded_technical = max(0.0, min(1.0, technical_score))
    return max(0.0, min(1.0, 0.45 * bounded_metadata + 0.55 * bounded_technical))


def score_image_file(image_path: Path, *, text: object = "") -> ImageQualityScores:
    try:
        stat = image_path.stat()
    except OSError:
        technical_score = None
    else:
        technical_score = estimate_image_technical_quality_cached(
            str(image_path.resolve()),
            stat.st_mtime_ns,
            stat.st_size,
        )
    metadata_score = metadata_quality_score(normalize_quality_text(text))
    return ImageQualityScores(
        aesthetic_score=combine_quality_scores(
            metadata_score=metadata_score,
            technical_score=technical_score,
        ),
        technical_quality_score=technical_score,
    )


def score_image_bytes(content_bytes: bytes, *, text: object = "") -> ImageQualityScores:
    technical_score = estimate_image_technical_quality_from_bytes(content_bytes)
    metadata_score = metadata_quality_score(normalize_quality_text(text))
    return ImageQualityScores(
        aesthetic_score=combine_quality_scores(
            metadata_score=metadata_score,
            technical_score=technical_score,
        ),
        technical_quality_score=technical_score,
    )


@lru_cache(maxsize=4096)
def estimate_image_technical_quality_cached(
    path: str,
    mtime_ns: int,
    file_size: int,
) -> float | None:
    del mtime_ns, file_size
    try:
        with Image.open(Path(path)) as source:
            try:
                source.seek(0)
            except EOFError:
                pass
            return estimate_image_technical_quality(source)
    except (OSError, UnidentifiedImageError):
        return None


def estimate_image_technical_quality_from_bytes(content_bytes: bytes) -> float | None:
    try:
        with Image.open(BytesIO(content_bytes)) as source:
            try:
                source.seek(0)
            except EOFError:
                pass
            return estimate_image_technical_quality(source)
    except (OSError, UnidentifiedImageError):
        return None


def estimate_image_technical_quality(source: Image.Image) -> float | None:
    image = ImageOps.exif_transpose(source).convert("RGB")
    source_width, source_height = image.size
    image.thumbnail((512, 512))
    gray = ImageOps.grayscale(image)

    pixels = np.asarray(gray, dtype=np.float32) / 255.0
    if pixels.size == 0:
        return None

    mean_luminance = float(np.mean(pixels))
    contrast = float(np.std(pixels))
    clipping_ratio = float(np.mean((pixels <= 0.03) | (pixels >= 0.97)))
    dx = np.diff(pixels, axis=1)
    dy = np.diff(pixels, axis=0)
    edge_strength = float(np.mean(np.abs(dx))) + float(np.mean(np.abs(dy)))

    exposure_score = 1.0 - min(1.0, abs(mean_luminance - 0.5) / 0.5)
    exposure_score = max(0.0, exposure_score - 1.8 * clipping_ratio)
    contrast_score = min(1.0, contrast / 0.24)
    sharpness_score = min(1.0, edge_strength / 0.09)
    resolution_score = min(1.0, math.sqrt(max(1, source_width * source_height)) / 3000.0)

    quality = (
        0.34 * sharpness_score
        + 0.28 * exposure_score
        + 0.22 * contrast_score
        + 0.16 * resolution_score
    )
    return max(0.0, min(1.0, quality))
