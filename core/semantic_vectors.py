from __future__ import annotations

import hashlib
import re

import numpy as np


SEMANTIC_HASH_BACKEND = "semantic_hash"
DEFAULT_SEMANTIC_VECTOR_DIMENSIONS = 512
TOKEN_PATTERN = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]+", re.UNICODE)


def is_semantic_hash_backend(value: str | None) -> bool:
    normalized = str(value or "").strip().lower().replace("-", "_")
    return normalized == SEMANTIC_HASH_BACKEND


def normalize_semantic_dimensions(value: int | None) -> int:
    if value is None or value <= 0:
        return DEFAULT_SEMANTIC_VECTOR_DIMENSIONS
    return value


def encode_semantic_text(
    text: str,
    *,
    dimensions: int,
) -> np.ndarray:
    resolved_dimensions = normalize_semantic_dimensions(dimensions)
    vector = np.zeros(resolved_dimensions, dtype=np.float32)
    tokens = _tokenize(text)
    if not tokens:
        return vector

    for index, token in enumerate(tokens):
        _accumulate_weight(vector, token, 1.0)
        if index + 1 < len(tokens):
            _accumulate_weight(vector, f"{token}::{tokens[index + 1]}", 0.5)

    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return (vector / norm).astype(np.float32, copy=False)


def _tokenize(text: str) -> list[str]:
    normalized = str(text or "").lower()
    return TOKEN_PATTERN.findall(normalized)


def _accumulate_weight(vector: np.ndarray, token: str, weight: float) -> None:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    index = int.from_bytes(digest[:4], "big") % vector.size
    sign = 1.0 if digest[4] % 2 == 0 else -1.0
    vector[index] += weight * sign
