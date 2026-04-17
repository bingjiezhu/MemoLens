from __future__ import annotations

from typing import Any

import numpy as np

from core.config import Settings
from core.semantic_vectors import encode_semantic_text, is_semantic_hash_backend


def _normalize(vector: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(vector)
    if norm == 0:
        return vector.astype(np.float32)
    return (vector / norm).astype(np.float32)


class EmbeddingService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.backend = settings.embedding_backend
        self.semantic_vector_dimensions = settings.semantic_vector_dimensions
        self._device: str | None = settings.embedding_device
        self._clip_model: Any | None = None
        self._clip_processor: Any | None = None
        self._dino_model: Any | None = None
        self._dino_processor: Any | None = None
        self._torch: Any | None = None

        if self.backend not in {"clip", "dino"} and not is_semantic_hash_backend(self.backend):
            raise ValueError(
                f"Unsupported EMBEDDING_BACKEND `{self.backend}`. Use `clip`, `dino`, or `semantic_hash`."
            )

    def encode_image(self, image, *, semantic_text: str | None = None, source_name: str | None = None):
        if is_semantic_hash_backend(self.backend):
            content = str(semantic_text or source_name or "").strip()
            if not content:
                raise ValueError(
                    "semantic_hash image embedding requires semantic_text or source_name."
                )
            return encode_semantic_text(
                content,
                dimensions=self.semantic_vector_dimensions,
            )

        if self.backend == "clip":
            self._ensure_clip_loaded()
            clip_inputs = self._clip_processor(images=image, return_tensors="pt")
            clip_inputs = {key: value.to(self._device) for key, value in clip_inputs.items()}

            with self._torch.inference_mode():
                clip_features = self._clip_model.get_image_features(**clip_inputs)

            clip_vector = clip_features.detach().cpu().numpy()[0]
            return _normalize(clip_vector)

        self._ensure_dino_loaded()
        dino_inputs = self._dino_processor(images=image, return_tensors="pt")
        dino_inputs = {key: value.to(self._device) for key, value in dino_inputs.items()}

        with self._torch.inference_mode():
            dino_outputs = self._dino_model(**dino_inputs)

        dino_vector = dino_outputs.last_hidden_state[:, 0, :].detach().cpu().numpy()[0]
        return _normalize(dino_vector)

    def encode_text(self, text: str) -> np.ndarray:
        if is_semantic_hash_backend(self.backend):
            return encode_semantic_text(
                text,
                dimensions=self.semantic_vector_dimensions,
            )
        if self.backend != "clip":
            raise RuntimeError("Text embeddings are only available when EMBEDDING_BACKEND=clip.")

        self._ensure_clip_loaded()
        inputs = self._clip_processor(text=[text], return_tensors="pt", padding=True, truncation=True)
        inputs = {key: value.to(self._device) for key, value in inputs.items()}

        with self._torch.inference_mode():
            text_features = self._clip_model.get_text_features(**inputs)

        return _normalize(text_features.detach().cpu().numpy()[0])

    def _ensure_clip_loaded(self) -> None:
        if self._clip_model is not None and self._clip_processor is not None:
            return

        torch, auto_processor, clip_model = self._import_clip_dependencies()
        self._torch = torch
        self._device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")

        self._clip_processor = auto_processor.from_pretrained(self.settings.clip_model_id)
        self._clip_model = clip_model.from_pretrained(self.settings.clip_model_id)
        self._clip_model.eval()
        self._clip_model.to(self._device)

    def _ensure_dino_loaded(self) -> None:
        if self._dino_model is not None and self._dino_processor is not None:
            return

        torch, auto_image_processor, auto_model = self._import_dino_dependencies()
        self._torch = self._torch or torch
        self._device = self._device or ("cuda" if torch.cuda.is_available() else "cpu")

        self._dino_processor = auto_image_processor.from_pretrained(self.settings.dino_model_id)
        self._dino_model = auto_model.from_pretrained(self.settings.dino_model_id)
        self._dino_model.eval()
        self._dino_model.to(self._device)

    @staticmethod
    def _import_clip_dependencies():
        try:
            import torch
            from transformers import AutoProcessor, CLIPModel
        except ImportError as exc:
            raise RuntimeError(
                "Missing embedding dependencies. Install torch and transformers first."
            ) from exc
        return torch, AutoProcessor, CLIPModel

    @staticmethod
    def _import_dino_dependencies():
        try:
            import torch
            from transformers import AutoImageProcessor, AutoModel
        except ImportError as exc:
            raise RuntimeError(
                "Missing DINO dependencies. Install torch and transformers first."
            ) from exc
        return torch, AutoImageProcessor, AutoModel
