from __future__ import annotations

import time
from pathlib import Path

from core.config import Settings
from core.db import ImageIndexRepository
from core.schemas import (
    IndexedImageSummary,
    IndexingJobResult,
    IndexingRequest,
    StoredImageRecord,
    utc_now_iso,
)
from core.text_embeddings import TextEmbeddingService, build_combined_text
from .embeddings import EmbeddingService
from .files import (
    decode_base64_image,
    extract_local_image_metadata,
    extract_uploaded_image_metadata,
    is_supported_image,
    prepare_image_for_modeling,
    prepare_uploaded_image_for_modeling,
)
from .geocoder import ReverseGeocoder
from .vision import OpenAICompatibleVisionClient


class IndexingService:
    def __init__(
        self,
        settings: Settings,
        repository: ImageIndexRepository,
        vision_client: OpenAICompatibleVisionClient,
        embedding_service: EmbeddingService,
        text_embedding_service: TextEmbeddingService,
        geocoder: ReverseGeocoder,
    ):
        self.settings = settings
        self.repository = repository
        self.vision_client = vision_client
        self.embedding_service = embedding_service
        self.text_embedding_service = text_embedding_service
        self.geocoder = geocoder

    def run(self, indexing_request: IndexingRequest) -> IndexingJobResult:
        library_root = Path(
            indexing_request.input.image_dir or self.settings.image_library_dir
        ).expanduser().resolve()
        if indexing_request.input.image is not None:
            return self._run_uploaded_image(indexing_request, library_root)

        candidates = self._collect_candidates(library_root, indexing_request)

        indexed: list[IndexedImageSummary] = []
        skipped: list[IndexedImageSummary] = []
        failed: list[IndexedImageSummary] = []
        records: list[StoredImageRecord] = []

        for image_path in candidates:
            try:
                local_metadata = extract_local_image_metadata(image_path, library_root)
            except Exception as exc:  # pragma: no cover - defensive guard
                failed.append(
                    IndexedImageSummary(
                        id=f"failed_{image_path.stem}",
                        filename=image_path.name,
                        relative_path=str(image_path),
                        status="failed",
                        message=f"metadata extraction failed: {exc}",
                    )
                )
                continue

            existing_record = None
            if indexing_request.persist_to_server and not indexing_request.reindex:
                existing_record = self.repository.get_by_sha256(local_metadata.sha256)

            if existing_record is not None:
                skip_message = "already indexed"
                if (
                    str(existing_record["relative_path"] or "") != local_metadata.relative_path
                    or str(existing_record["filename"] or "") != local_metadata.filename
                ):
                    self.repository.refresh_existing_file_metadata(
                        sha256=local_metadata.sha256,
                        filename=local_metadata.filename,
                        relative_path=local_metadata.relative_path,
                        mime_type=local_metadata.mime_type,
                        file_size=local_metadata.file_size,
                        width=local_metadata.width,
                        height=local_metadata.height,
                        taken_at=local_metadata.taken_at,
                        lat=local_metadata.lat,
                        lon=local_metadata.lon,
                        altitude=local_metadata.altitude,
                        updated_at=utc_now_iso(),
                    )
                    skip_message = "already indexed (path updated)"

                skipped.append(
                    IndexedImageSummary(
                        id=str(existing_record["id"] or local_metadata.id),
                        filename=local_metadata.filename,
                        relative_path=local_metadata.relative_path,
                        status="skipped",
                        message=skip_message,
                    )
                )
                continue

            try:
                geo_metadata = self.geocoder.reverse(local_metadata.lat, local_metadata.lon)
                prepared_image = prepare_image_for_modeling(
                    image_path=image_path,
                    target_width=self.settings.process_image_width,
                )
                vision_metadata = self.vision_client.describe_image(
                    prepared_image=prepared_image,
                    model=indexing_request.model or self.settings.vision_model,
                )
                combined_text = build_combined_text(
                    description=vision_metadata.description,
                    tags=vision_metadata.tags,
                    place_name=geo_metadata.place_name,
                    country=geo_metadata.country,
                    location_hint=vision_metadata.location_hint,
                    semantic_hints=self.settings.semantic_hints,
                )
                embedding = self.embedding_service.encode_image(
                    prepared_image.image,
                    semantic_text=combined_text,
                    source_name=prepared_image.source_name,
                )
                combined_text_embedding_blob = self._encode_combined_text(combined_text)

                now_iso = utc_now_iso()
                record = StoredImageRecord(
                    id=local_metadata.id,
                    sha256=local_metadata.sha256,
                    filename=local_metadata.filename,
                    relative_path=local_metadata.relative_path,
                    mime_type=local_metadata.mime_type,
                    file_size=local_metadata.file_size,
                    width=local_metadata.width,
                    height=local_metadata.height,
                    taken_at=local_metadata.taken_at,
                    lat=local_metadata.lat,
                    lon=local_metadata.lon,
                    altitude=local_metadata.altitude,
                    place_name=geo_metadata.place_name,
                    country=geo_metadata.country,
                    description=vision_metadata.description,
                    tags=vision_metadata.tags,
                    combined_text=combined_text,
                    text_embedding_model=(
                        self.settings.text_embedding_model_id
                        if combined_text_embedding_blob is not None
                        else None
                    ),
                    combined_text_embedding_blob=combined_text_embedding_blob,
                    embedding_backend=self.settings.embedding_backend,
                    embedding_blob=embedding.astype("float32").tobytes(),
                    created_at=now_iso,
                    updated_at=now_iso,
                )
                if indexing_request.persist_to_server:
                    self.repository.upsert(record)
                records.append(record)
                summary_status = "indexed" if indexing_request.persist_to_server else "processed"

                indexed.append(
                    IndexedImageSummary(
                        id=record.id,
                        filename=record.filename,
                        relative_path=record.relative_path,
                        status=summary_status,
                        tags=record.tags,
                        description=record.description,
                        taken_at=record.taken_at,
                        place_name=record.place_name,
                        country=record.country,
                    )
                )
            except Exception as exc:  # pragma: no cover - keeps one bad image from killing the job
                failed.append(
                    IndexedImageSummary(
                        id=local_metadata.id,
                        filename=local_metadata.filename,
                        relative_path=local_metadata.relative_path,
                        status="failed",
                        message=str(exc),
                    )
                )

        return IndexingJobResult(
            id=f"idxjob_{int(time.time())}",
            model=indexing_request.model,
            embedding_backend=self.settings.embedding_backend,
            image_dir=str(library_root),
            db_path=str(self.repository.db_path),
            indexed=indexed,
            skipped=skipped,
            failed=failed,
            records=records,
            created=int(time.time()),
        )

    def _run_uploaded_image(
        self,
        indexing_request: IndexingRequest,
        library_root: Path,
    ) -> IndexingJobResult:
        indexed: list[IndexedImageSummary] = []
        skipped: list[IndexedImageSummary] = []
        failed: list[IndexedImageSummary] = []
        records: list[StoredImageRecord] = []

        assert indexing_request.input.image is not None
        uploaded = indexing_request.input.image

        try:
            raw_bytes, mime_type_from_data_url = decode_base64_image(uploaded.b64)
            local_metadata = extract_uploaded_image_metadata(
                content_bytes=raw_bytes,
                filename=uploaded.filename,
                relative_path=uploaded.relative_path,
                mime_type=uploaded.mime_type or mime_type_from_data_url,
            )
        except Exception as exc:
            failed.append(
                IndexedImageSummary(
                    id=f"failed_{Path(uploaded.filename).stem}",
                    filename=uploaded.filename,
                    relative_path=uploaded.relative_path or uploaded.filename,
                    status="failed",
                    message=f"metadata extraction failed: {exc}",
                )
            )
            return IndexingJobResult(
                id=f"idxjob_{int(time.time())}",
                model=indexing_request.model,
                embedding_backend=self.settings.embedding_backend,
                image_dir=str(library_root),
                db_path=str(self.repository.db_path),
                indexed=indexed,
                skipped=skipped,
                failed=failed,
                records=records,
                created=int(time.time()),
            )

        if (
            indexing_request.persist_to_server
            and self.repository.has_sha256(local_metadata.sha256)
            and not indexing_request.reindex
        ):
            skipped.append(
                IndexedImageSummary(
                    id=local_metadata.id,
                    filename=local_metadata.filename,
                    relative_path=local_metadata.relative_path,
                    status="skipped",
                    message="already indexed",
                )
            )
        else:
            try:
                geo_metadata = self.geocoder.reverse(local_metadata.lat, local_metadata.lon)
                prepared_image = prepare_uploaded_image_for_modeling(
                    content_bytes=raw_bytes,
                    filename=uploaded.filename,
                    target_width=self.settings.process_image_width,
                )
                vision_metadata = self.vision_client.describe_image(
                    prepared_image=prepared_image,
                    model=indexing_request.model or self.settings.vision_model,
                )
                combined_text = build_combined_text(
                    description=vision_metadata.description,
                    tags=vision_metadata.tags,
                    place_name=geo_metadata.place_name,
                    country=geo_metadata.country,
                    location_hint=vision_metadata.location_hint,
                    semantic_hints=self.settings.semantic_hints,
                )
                embedding = self.embedding_service.encode_image(
                    prepared_image.image,
                    semantic_text=combined_text,
                    source_name=prepared_image.source_name,
                )
                combined_text_embedding_blob = self._encode_combined_text(combined_text)

                now_iso = utc_now_iso()
                record = StoredImageRecord(
                    id=local_metadata.id,
                    sha256=local_metadata.sha256,
                    filename=local_metadata.filename,
                    relative_path=local_metadata.relative_path,
                    mime_type=local_metadata.mime_type,
                    file_size=local_metadata.file_size,
                    width=local_metadata.width,
                    height=local_metadata.height,
                    taken_at=local_metadata.taken_at,
                    lat=local_metadata.lat,
                    lon=local_metadata.lon,
                    altitude=local_metadata.altitude,
                    place_name=geo_metadata.place_name,
                    country=geo_metadata.country,
                    description=vision_metadata.description,
                    tags=vision_metadata.tags,
                    combined_text=combined_text,
                    text_embedding_model=(
                        self.settings.text_embedding_model_id
                        if combined_text_embedding_blob is not None
                        else None
                    ),
                    combined_text_embedding_blob=combined_text_embedding_blob,
                    embedding_backend=self.settings.embedding_backend,
                    embedding_blob=embedding.astype("float32").tobytes(),
                    created_at=now_iso,
                    updated_at=now_iso,
                )
                if indexing_request.persist_to_server:
                    self.repository.upsert(record)
                records.append(record)
                summary_status = "indexed" if indexing_request.persist_to_server else "processed"
                indexed.append(
                    IndexedImageSummary(
                        id=record.id,
                        filename=record.filename,
                        relative_path=record.relative_path,
                        status=summary_status,
                        tags=record.tags,
                        description=record.description,
                        taken_at=record.taken_at,
                        place_name=record.place_name,
                        country=record.country,
                    )
                )
            except Exception as exc:
                failed.append(
                    IndexedImageSummary(
                        id=local_metadata.id,
                        filename=local_metadata.filename,
                        relative_path=local_metadata.relative_path,
                        status="failed",
                        message=str(exc),
                    )
                )

        return IndexingJobResult(
            id=f"idxjob_{int(time.time())}",
            model=indexing_request.model,
            embedding_backend=self.settings.embedding_backend,
            image_dir=str(library_root),
            db_path=str(self.repository.db_path),
            indexed=indexed,
            skipped=skipped,
            failed=failed,
            records=records,
            created=int(time.time()),
        )

    def _collect_candidates(
        self,
        library_root: Path,
        indexing_request: IndexingRequest,
    ) -> list[Path]:
        explicit_files = indexing_request.input.files
        if explicit_files:
            candidates = [self._resolve_candidate(library_root, Path(file_path)) for file_path in explicit_files]
        else:
            if not library_root.exists():
                raise FileNotFoundError(f"Image directory does not exist: {library_root}")
            pattern = "**/*" if indexing_request.input.recursive else "*"
            candidates = [path for path in library_root.glob(pattern) if path.is_file()]

        filtered = []
        for candidate in sorted(candidates):
            resolved = candidate.resolve()
            if resolved == self.settings.db_path.resolve():
                continue
            if is_supported_image(candidate):
                filtered.append(candidate)

        if indexing_request.limit is not None:
            filtered = filtered[: indexing_request.limit]
        return filtered

    @staticmethod
    def _resolve_candidate(library_root: Path, candidate: Path) -> Path:
        if candidate.is_absolute():
            return candidate.resolve()
        return (library_root / candidate).resolve()

    def _encode_combined_text(self, combined_text: str) -> bytes | None:
        try:
            embedding = self.text_embedding_service.encode_document(combined_text)
        except Exception:
            return None
        return embedding.astype("float32").tobytes()
