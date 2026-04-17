from __future__ import annotations

from typing import Any

import numpy as np

from .config import Settings
from .semantic_hints import expand_text_with_hints
from .semantic_vectors import encode_semantic_text, is_semantic_hash_backend


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


def build_combined_text(
    *,
    description: str,
    tags: list[str],
    place_name: str | None = None,
    country: str | None = None,
    location_hint: str | None = None,
    semantic_hints: dict[str, list[str]] | None = None,
) -> str:
    parts: list[str] = []

    normalized_description = str(description or "").strip()
    if normalized_description:
        parts.append(normalized_description)

    normalized_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
    if normalized_tags:
        parts.append(f"Tags: {', '.join(normalized_tags)}.")

    location_parts = [
        str(part).strip()
        for part in [place_name, country]
        if isinstance(part, str) and str(part).strip()
    ]
    if location_parts:
        parts.append(f"Location: {', '.join(location_parts)}.")
    elif isinstance(location_hint, str) and location_hint.strip():
        normalized_hint = location_hint.strip()
        parts.append(f"Possible location or landmark: {normalized_hint}.")
        parts.append(f"Location hint keywords: {normalized_hint}.")

    combined = " ".join(parts).strip()
    if semantic_hints:
        return expand_text_with_hints(combined, semantic_hints)
    return combined


class TextEmbeddingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.model_id = settings.text_embedding_model_id
        self.query_prefix = settings.text_embedding_query_prefix
        self.document_prefix = settings.text_embedding_document_prefix
        self.max_length = settings.text_embedding_max_length
        self.semantic_vector_dimensions = settings.semantic_vector_dimensions
        self.use_semantic_hash = is_semantic_hash_backend(settings.embedding_backend)
        self._device: str | None = settings.text_embedding_device
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode(self.query_prefix, text)

    def encode_document(self, text: str) -> np.ndarray:
        return self._encode(self.document_prefix, text)

    def _encode(self, prefix: str, text: str) -> np.ndarray:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            raise ValueError("Text embedding input must be non-empty.")
        normalized_text = expand_text_with_hints(
            normalized_text,
            self.settings.semantic_hints,
        )
        if self.use_semantic_hash:
            content = f"{prefix}{normalized_text}" if prefix else normalized_text
            return encode_semantic_text(
                content,
                dimensions=self.semantic_vector_dimensions,
            )

        self._ensure_loaded()
        assert self._torch is not None
        content = f"{prefix}{normalized_text}" if prefix else normalized_text
        encoded = self._tokenizer(
            content,
            padding=False,
            truncation=True,
            max_length=self.max_length,
            return_tensors="pt",
        )
        encoded = {key: value.to(self._device) for key, value in encoded.items()}

        with self._torch.inference_mode():
            outputs = self._model(**encoded)

        token_embeddings = outputs[0] if isinstance(outputs, tuple) else outputs.last_hidden_state
        attention_mask = encoded["attention_mask"].unsqueeze(-1).expand(token_embeddings.size()).float()
        pooled = (token_embeddings * attention_mask).sum(1) / attention_mask.sum(1).clamp(min=1e-9)
        return _normalize(pooled.detach().cpu().numpy()[0])

    def _ensure_loaded(self) -> None:
        if self._tokenizer is not None and self._model is not None:
            return

        torch, auto_model, auto_tokenizer = self._import_dependencies()
        self._torch = torch
        self._device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")
        try:
            self._tokenizer = auto_tokenizer.from_pretrained(self.model_id, trust_remote_code=True)
            self._model = auto_model.from_pretrained(self.model_id, trust_remote_code=True)
        except Exception as exc:
            if "einops" in str(exc).lower():
                raise RuntimeError(
                    "The configured text embedding model requires `einops`. Install it with "
                    "`pip install einops` or reinstall from backend/requirements.txt."
                ) from exc
            raise
        self._model.eval()
        self._model.to(self._device)

    @staticmethod
    def _import_dependencies():
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError(
                "Missing text embedding dependencies. Install torch and transformers first."
            ) from exc
        return torch, AutoModel, AutoTokenizer
