"""Production execution for exact-bound canonical image jobs.

Local stages run without additional authority. Provider-backed vision runs
only through the exact one-shot egress service; otherwise it remains an honest
typed ``disabled`` outcome.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import os
import stat
import warnings

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from core.image_analysis_contract import (
    build_canonical_image_combined_text,
    canonical_json,
    seal_image_analysis_result,
)
from core.image_analysis_persistence import (
    ExactImageSource,
    ImageAnalysisPersistenceError,
)
from core.image_analysis_job_contract import (
    ImageAnalysisJobContractError,
    canonical_sha256 as canonical_job_sha256,
)
from core.media_db import MediaRepository
from core.semantic_vectors import encode_semantic_text, is_semantic_hash_backend
from .provider_egress import ProviderImageVisionEgress


MAX_IMAGE_DIMENSION = 16_384
MAX_IMAGE_PIXELS = 67_108_864
SOURCE_READ_CHUNK_BYTES = 1024 * 1024
_NON_MUTATING_ADMISSION_ERRORS = frozenset(
    {
        "image_analysis_attempt_changed",
        "image_analysis_attempt_claim_conflict",
        "image_analysis_binding_corrupt",
        "image_analysis_job_missing",
        "image_database_identity_invalid",
        "image_database_scope_changed",
        "image_runtime_generation_changed",
    }
)
_SOURCE_ERRORS = frozenset(
    {
        "image_source_authority_changed",
        "image_source_changed",
        "image_source_unavailable",
    }
)
_IMAGE_MIME_BY_FORMAT = {
    "BMP": "image/bmp",
    "GIF": "image/gif",
    "HEIC": "image/heic",
    "HEIF": "image/heif",
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "TIFF": "image/tiff",
    "WEBP": "image/webp",
}


class ImageAnalysisCapabilityError(RuntimeError):
    """Stable local capability failure suitable for a typed attempt error."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        stage: str = "metadata",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.retryable = retryable


class ImageAnalysisCancelled(RuntimeError):
    pass


def _provenance(
    *,
    rule: str,
    model_id: str | None = None,
    model_version: str | None = None,
) -> dict[str, object]:
    return {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": model_id,
        "model_version": model_version,
        "rule_id": rule,
        "rule_version": "1",
    }


def _disabled_stage(*, rule: str, reason: str) -> dict[str, object]:
    return {
        "status": "disabled",
        "provenance": _provenance(rule=rule),
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason,
    }


def _probe_image_metadata(source: ExactImageSource) -> dict[str, object]:
    """Probe one held descriptor without reopening its mutable display path."""

    try:
        with os.fdopen(os.dup(source.handle.fileno()), "rb") as handle:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(handle) as image:
                    width, height = image.size
                    image_format = str(image.format or "").upper()
                    if (
                        type(width) is not int
                        or type(height) is not int
                        or width <= 0
                        or height <= 0
                        or width > MAX_IMAGE_DIMENSION
                        or height > MAX_IMAGE_DIMENSION
                        or width * height > MAX_IMAGE_PIXELS
                    ):
                        raise ImageAnalysisCapabilityError(
                            "image_dimensions_unsupported",
                            "Image dimensions exceed the bounded local metadata capability.",
                        )
                    mime_type = Image.MIME.get(image_format) or _IMAGE_MIME_BY_FORMAT.get(
                        image_format
                    )
                    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
                        raise ImageAnalysisCapabilityError(
                            "image_format_unsupported",
                            "The image format is not registered for canonical metadata publication.",
                        )
                    image.verify()
    except ImageAnalysisCapabilityError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageAnalysisCapabilityError(
            "image_dimensions_unsupported",
            "Image dimensions exceed the bounded local metadata capability.",
        ) from exc
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise ImageAnalysisCapabilityError(
            "image_decode_failed",
            "The held source is not a valid supported image.",
        ) from exc

    return {
        "mime_type": mime_type,
        "file_size": int(source.binding["observed_size"]),
        "width": width,
        "height": height,
        # A0.1 does not normalize EXIF/GPS yet.  Null is evidence-preserving;
        # guessing from filesystem clocks would turn transport metadata into
        # capture truth.
        "taken_at": None,
        "latitude": None,
        "longitude": None,
        "altitude": None,
    }


def _verify_held_source(
    source: ExactImageSource,
    binding: Mapping[str, object],
) -> None:
    """Rehash and restat the same held inode after the local file read."""

    before = os.fstat(source.handle.fileno())
    if not stat.S_ISREG(before.st_mode):
        raise ImageAnalysisPersistenceError("image_source_changed")
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(
        source.handle.fileno(),
        SOURCE_READ_CHUNK_BYTES,
        offset,
    ):
        digest.update(chunk)
        offset += len(chunk)
    after = os.fstat(source.handle.fileno())
    observed = (
        int(after.st_dev),
        int(after.st_ino),
        int(after.st_size),
        int(after.st_mtime_ns),
        int(after.st_ctime_ns),
    )
    if (
        observed
        != (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        or observed
        != (
            int(binding["source_device"]),
            int(binding["source_inode"]),
            int(binding["observed_size"]),
            int(binding["observed_mtime_ns"]),
            int(binding["observed_ctime_ns"]),
        )
        or digest.hexdigest() != binding["input_asset_sha256"]
    ):
        raise ImageAnalysisPersistenceError("image_source_changed")


def _require_exact_held_binding(
    source: ExactImageSource,
    binding: Mapping[str, object],
) -> None:
    for key in (
        "asset_id",
        "file_identity_sha256",
        "input_asset_sha256",
        "library_root_id",
        "observed_ctime_ns",
        "observed_mtime_ns",
        "observed_size",
        "relative_path",
        "root_permission_fingerprint",
        "source_device",
        "source_id",
        "source_inode",
    ):
        if source.binding.get(key) != binding.get(key):
            raise ImageAnalysisPersistenceError("image_source_changed")


def _visual_embedding_image(
    source: ExactImageSource,
    *,
    target_width: int,
) -> Image.Image:
    """Decode a bounded RGB copy from the already-held source descriptor."""

    try:
        with os.fdopen(os.dup(source.handle.fileno()), "rb") as handle:
            with Image.open(handle) as opened:
                opened.load()
                image = ImageOps.exif_transpose(opened).convert("RGB")
    except (OSError, SyntaxError, UnidentifiedImageError, ValueError) as exc:
        raise ImageAnalysisCapabilityError(
            "image_embedding_decode_failed",
            "The held image could not be decoded for the configured local embedding backend.",
            stage="embedding",
        ) from exc
    if target_width > 0 and image.width > target_width:
        target_height = max(1, round(image.height * target_width / image.width))
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)
    return image


def _embedding_stage(
    source: ExactImageSource,
    embedding_service: object | None,
    *,
    vision_output: Mapping[str, object] | None = None,
    geocode_output: Mapping[str, object] | None = None,
) -> tuple[dict[str, object], tuple[dict[str, object], ...]]:
    """Produce one explicit image-search vector or a typed disabled stage.

    ``semantic_hash`` remains text-derived and is bound to the frozen empty
    combined-text rendering when no vision/geocode stage ran.  It is never
    relabelled as visual evidence.  CLIP/DINO use the held image bytes and are
    labelled visual.  A runtime without an explicitly configured service keeps
    the existing honest disabled outcome.
    """

    if embedding_service is None:
        return (
            _disabled_stage(
                rule="embedding-policy",
                reason="embedding_not_configured",
            ),
            (),
        )

    backend = str(getattr(embedding_service, "backend", "") or "").strip().casefold()
    combined_text = build_canonical_image_combined_text(
        vision_output=vision_output,
        geocode_output=geocode_output,
    )
    combined_text_sha256: str | None = None
    try:
        if is_semantic_hash_backend(backend):
            dimensions = int(getattr(embedding_service, "semantic_vector_dimensions", 0))
            vector = encode_semantic_text(combined_text, dimensions=dimensions)
            signal = "text_derived"
            model_id = "semantic_hash"
            combined_text_sha256 = hashlib.sha256(combined_text.encode("utf-8")).hexdigest()
        elif backend in {"clip", "dino"}:
            settings = getattr(embedding_service, "settings", None)
            target_width = int(getattr(settings, "process_image_width", 0) or 0)
            image = _visual_embedding_image(source, target_width=target_width)
            vector = embedding_service.encode_image(image)
            signal = "visual"
            model_id = str(
                getattr(
                    settings,
                    "clip_model_id" if backend == "clip" else "dino_model_id",
                    backend,
                )
            )
        else:
            raise ValueError("unsupported embedding backend")
        normalized = np.asarray(vector, dtype="<f4")
        if (
            normalized.ndim != 1
            or not 1 <= int(normalized.size) <= 65_536
            or not bool(np.isfinite(normalized).all())
        ):
            raise ValueError("invalid embedding vector")
    except ImageAnalysisCapabilityError:
        raise
    except (AttributeError, ImportError, OSError, RuntimeError, TypeError, ValueError):
        return (
            _disabled_stage(
                rule="embedding-policy",
                reason="embedding_backend_unavailable",
            ),
            (),
        )

    vector_bytes = normalized.tobytes(order="C")
    vector_sha256 = hashlib.sha256(vector_bytes).hexdigest()
    output = {
        "combined_text_sha256": combined_text_sha256,
        "vectors": [
            {
                "purpose": "image_search",
                "signal": signal,
                "model_id": model_id,
                "dimensions": int(normalized.size),
                "artifact_sha256": vector_sha256,
            }
        ],
    }
    output_bytes = canonical_json(output).encode("utf-8")
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    return (
        {
            "status": "succeeded",
            "provenance": _provenance(
                rule="configured-embedding-backend",
            ),
            "output": output,
            "artifact_sha256": output_sha256,
            "reason_code": None,
        },
        (
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": output_sha256,
                "bytes": output_bytes,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/vnd.memolens.float32",
                "signal": signal,
                "model_id": model_id,
                "dimensions": int(normalized.size),
                "artifact_sha256": vector_sha256,
                "bytes": vector_bytes,
            },
        ),
    )


class ImageAnalysisJobProcessor:
    """Run one generation-bound image attempt through canonical publication."""

    def __init__(
        self,
        repository: MediaRepository,
        *,
        embedding_service: object | None = None,
        provider_vision_egress: ProviderImageVisionEgress | None = None,
    ) -> None:
        self.repository = repository
        self.embedding_service = embedding_service
        self.provider_vision_egress = provider_vision_egress

    def run(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        cancel_requested: Callable[[], bool],
    ) -> None:
        claimed = False
        published = False
        current_stage = "source_admission"
        try:
            binding = self.repository.claim_image_analysis_attempt(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
            )
            claimed = True
            heartbeat_sequence = int(binding["heartbeat_sequence"])
            if cancel_requested():
                raise ImageAnalysisCancelled("Image analysis was cancelled.")

            with self.repository._open_admitted_image_source(
                source_id=str(binding["source_id"]),
                expected_asset_id=str(binding["asset_id"]),
                expected_input_sha256=str(binding["input_asset_sha256"]),
            ) as source:
                _require_exact_held_binding(source, binding)
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="source_admission",
                    outcome="succeeded",
                    outcome_sha256=canonical_job_sha256(
                        {
                            "source_binding_sha256": str(
                                binding["source_binding_sha256"]
                            )
                        }
                    ),
                    reason_code=None,
                    artifact_digests=(
                        {
                            "stage": "source_admission",
                            "name": "file_identity",
                            "sha256": str(binding["file_identity_sha256"]),
                        },
                    ),
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                current_stage = "metadata"
                metadata = _probe_image_metadata(source)
                _verify_held_source(source, binding)
                metadata_bytes = canonical_json(metadata).encode("utf-8")
                metadata_sha256 = hashlib.sha256(metadata_bytes).hexdigest()
                metadata_stage = {
                    "status": "succeeded",
                    "provenance": _provenance(rule="pillow-verify"),
                    "output": metadata,
                    "artifact_sha256": metadata_sha256,
                    "reason_code": None,
                }
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="metadata",
                    outcome="succeeded",
                    outcome_sha256=canonical_job_sha256(metadata_stage),
                    reason_code=None,
                    artifact_digests=(
                        {
                            "stage": "metadata",
                            "name": "stage_output",
                            "sha256": metadata_sha256,
                        },
                    ),
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                current_stage = "geocode"
                geocode = _disabled_stage(
                    rule="network-policy",
                    reason="network_not_authorized",
                )
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="geocode",
                    outcome="disabled",
                    outcome_sha256=canonical_job_sha256(geocode),
                    reason_code="network_not_authorized",
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                current_stage = "vision"
                vision_artifacts: tuple[dict[str, object], ...] = ()
                if self.provider_vision_egress is None:
                    vision = _disabled_stage(
                        rule="provider-policy",
                        reason="provider_not_authorized",
                    )
                else:
                    vision, vision_artifacts = self.provider_vision_egress.run(
                        source,
                        binding,
                        runtime_generation,
                        expected_attempt,
                    )
                    _verify_held_source(source, binding)
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="vision",
                    outcome=str(vision["status"]),
                    outcome_sha256=canonical_job_sha256(vision),
                    reason_code=(
                        str(vision["reason_code"])
                        if vision["reason_code"] is not None
                        else None
                    ),
                    artifact_digests=tuple(
                        {
                            "stage": str(artifact["stage"]),
                            "name": str(artifact["name"]),
                            "sha256": str(artifact["artifact_sha256"]),
                        }
                        for artifact in vision_artifacts
                    ),
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                current_stage = "embedding"
                embedding, embedding_artifacts = _embedding_stage(
                    source,
                    self.embedding_service,
                    vision_output=(
                        vision["output"]
                        if vision["status"] == "succeeded"
                        and isinstance(vision["output"], Mapping)
                        else None
                    ),
                    geocode_output=(
                        geocode["output"]
                        if geocode["status"] == "succeeded"
                        and isinstance(geocode["output"], Mapping)
                        else None
                    ),
                )
                _verify_held_source(source, binding)
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="embedding",
                    outcome=str(embedding["status"]),
                    outcome_sha256=canonical_job_sha256(embedding),
                    reason_code=(
                        str(embedding["reason_code"])
                        if embedding["reason_code"] is not None
                        else None
                    ),
                    artifact_digests=tuple(
                        {
                            "stage": str(artifact["stage"]),
                            "name": str(artifact["name"]),
                            "sha256": str(artifact["artifact_sha256"]),
                        }
                        for artifact in embedding_artifacts
                    ),
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                current_stage = "quality"
                quality = _disabled_stage(
                    rule="quality-policy",
                    reason="quality_not_configured",
                )
                heartbeat_sequence = self.repository.checkpoint_image_analysis_stage(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    expected_sequence=heartbeat_sequence,
                    stage="quality",
                    outcome="disabled",
                    outcome_sha256=canonical_job_sha256(quality),
                    reason_code="quality_not_configured",
                )
                if cancel_requested():
                    raise ImageAnalysisCancelled("Image analysis was cancelled.")

                request_document = binding["request"]
                if type(request_document) is not dict:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_binding_corrupt"
                    )
                profile = request_document["analysis_profile"]
                if type(profile) is not dict:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_binding_corrupt"
                    )
                result = seal_image_analysis_result(
                    {
                        "object": "memolens.image_analysis_result",
                        "schema_version": "1",
                        "asset_id": str(binding["asset_id"]),
                        "asset_sha256": str(binding["input_asset_sha256"]),
                        "analysis_run_id": str(binding["analysis_run_id"]),
                        "revision": int(binding["intended_revision"]),
                        "source_binding": {
                            "source_id": str(binding["source_id"]),
                            "library_root_id": str(binding["library_root_id"]),
                            "observed_size": int(binding["observed_size"]),
                            "observed_mtime_ns": int(binding["observed_mtime_ns"]),
                            "file_identity_sha256": str(binding["file_identity_sha256"]),
                        },
                        "analysis_profile": {
                            "id": str(profile["profile_id"]),
                            "version": str(profile["profile_version"]),
                            "content_sha256": str(profile["profile_sha256"]),
                        },
                        "stages": {
                            "metadata": metadata_stage,
                            "geocode": geocode,
                            "vision": vision,
                            "embedding": embedding,
                            "quality": quality,
                        },
                    }
                )
                current_stage = "publish"
                self.repository.publish_image_analysis(
                    job_id=job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    result=result,
                    artifacts=(
                        {
                            "stage": "metadata",
                            "name": "stage_output",
                            "media_type": "application/json",
                            "signal": None,
                            "model_id": None,
                            "dimensions": None,
                            "artifact_sha256": metadata_sha256,
                            "bytes": metadata_bytes,
                        },
                        *vision_artifacts,
                        *embedding_artifacts,
                    ),
                )
                published = True

            current_stage = "shadow_projection"
            self.repository.project_image_analysis_change(
                job_id=job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
            )
        except ImageAnalysisCancelled as exc:
            self._cancel(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                detail=str(exc),
            )
        except ImageAnalysisCapabilityError as exc:
            self._fail(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                code="stage_failed",
                stage=exc.stage,
                retryable=exc.retryable,
                detail=f"{exc.code}:{exc}",
            )
        except ImageAnalysisPersistenceError as exc:
            if exc.code in _NON_MUTATING_ADMISSION_ERRORS:
                return
            if exc.code == "image_analysis_cancel_requested":
                self._cancel(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    detail="cancel_requested",
                )
                return
            if exc.code in _SOURCE_ERRORS:
                self._fail(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    code="source_changed",
                    stage=current_stage,
                    retryable=False,
                    detail=exc.code,
                )
                return
            # Projection already owns typed blocked/superseded terminal writes.
            # Other post-publication failures remain resumable without changing
            # the successfully published canonical run.
            if claimed and published:
                self._interrupt(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    detail=exc.code,
                )
                return
            if claimed:
                self._fail(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    code="stage_failed",
                    stage=current_stage,
                    retryable=True,
                    detail=exc.code,
                )
        except Exception as exc:
            if claimed:
                self._fail(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    code="internal_error",
                    stage=current_stage,
                    retryable=True,
                    detail=type(exc).__name__,
                )

    def _fail(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        code: str,
        stage: str,
        retryable: bool,
        detail: str,
    ) -> None:
        try:
            observed = self.repository.get_media_job(job_id)
            persisted_stage = (
                str(observed.get("stage") or stage)
                if observed is not None
                else stage
            )
            self.repository.fail_image_analysis_attempt(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                code=code,
                stage=persisted_stage,
                retryable=retryable,
                detail=detail,
            )
        except ImageAnalysisJobContractError:
            # A caller-supplied mapping bug must not strand a claimed attempt.
            # Retry once with the closed generic code and the same exact
            # authority/stage; contract validation happens before any write.
            if code == "internal_error":
                return
            try:
                self.repository.fail_image_analysis_attempt(
                    job_id,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                    code="internal_error",
                    stage=persisted_stage,
                    retryable=True,
                    detail="worker_error_code_mapping_invalid",
                )
            except (ImageAnalysisJobContractError, ImageAnalysisPersistenceError):
                return
        except ImageAnalysisPersistenceError:
            return

    def _cancel(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        detail: str,
    ) -> None:
        try:
            self.repository.cancel_image_analysis_attempt(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                detail=detail,
            )
        except (ImageAnalysisJobContractError, ImageAnalysisPersistenceError):
            return

    def _interrupt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        detail: str,
    ) -> None:
        try:
            self.repository.interrupt_image_analysis_attempt(
                job_id,
                runtime_generation=runtime_generation,
                expected_attempt=expected_attempt,
                detail=detail,
            )
        except (ImageAnalysisJobContractError, ImageAnalysisPersistenceError):
            return


__all__ = [
    "ImageAnalysisCapabilityError",
    "ImageAnalysisJobProcessor",
]
