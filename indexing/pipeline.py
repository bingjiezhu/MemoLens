from __future__ import annotations

import os
import time
from pathlib import Path

from core.config import Settings
from core.db import ImageIndexRepository
from core.image_quality import score_image_bytes
from core.media_db import MediaRepository
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
    PinnedLibraryRoot,
    collect_pinned_library_candidates,
    decode_base64_image,
    extract_local_image_metadata,
    extract_uploaded_image_metadata,
    is_supported_image,
    open_pinned_library_root,
    open_pinned_local_image,
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
        media_repository: MediaRepository | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.vision_client = vision_client
        self.embedding_service = embedding_service
        self.text_embedding_service = text_embedding_service
        self.geocoder = geocoder
        self.media_repository = media_repository

    def run(
        self,
        indexing_request: IndexingRequest,
        *,
        pinned_root: PinnedLibraryRoot | None = None,
    ) -> IndexingJobResult:
        requested_root = Path(
            os.path.realpath(
                os.path.abspath(
                Path(
                    indexing_request.input.image_dir
                    or self.settings.image_library_dir
                ).expanduser()
                )
            )
        )
        expected_identity = (
            None
            if indexing_request.library_root_identity is None
            else (
                indexing_request.library_root_identity.device,
                indexing_request.library_root_identity.inode,
            )
        )
        owns_root = pinned_root is None
        root = pinned_root or open_pinned_library_root(
            requested_root,
            expected_identity=expected_identity,
        )
        try:
            if root.root_path != requested_root:
                raise PermissionError(
                    "Indexing root does not match the pinned library root."
                )
            root.assert_expected_identity(expected_identity)
            return self._run_with_pinned_root(indexing_request, root)
        finally:
            if owns_root:
                root.close()

    def _run_with_pinned_root(
        self,
        indexing_request: IndexingRequest,
        pinned_root: PinnedLibraryRoot,
    ) -> IndexingJobResult:
        library_root = pinned_root.root_path
        if indexing_request.persist_to_server:
            raise ValueError(
                "legacy_direct_image_persistence_disabled: use the durable image import adapter"
            )
        if indexing_request.input.image is not None:
            return self._run_uploaded_image(indexing_request, library_root)

        candidates = self._collect_candidates(pinned_root, indexing_request)

        indexed: list[IndexedImageSummary] = []
        skipped: list[IndexedImageSummary] = []
        failed: list[IndexedImageSummary] = []
        records: list[StoredImageRecord] = []

        for relative_path in candidates:
            try:
                with open_pinned_local_image(
                    pinned_root=pinned_root,
                    relative_path=relative_path,
                ) as source:
                    local_metadata = extract_local_image_metadata(source)
                    geo_metadata = self.geocoder.reverse(local_metadata.lat, local_metadata.lon)
                    prepared_image = prepare_image_for_modeling(
                        source=source,
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
                    quality_scores = score_image_bytes(
                        source.content_bytes,
                        text=combined_text,
                    )

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
                        aesthetic_score=quality_scores.aesthetic_score,
                        aesthetic_model=quality_scores.model,
                        technical_quality_score=quality_scores.technical_quality_score,
                        aesthetic_updated_at=now_iso,
                    )
                    records.append(record)

                    indexed.append(
                        IndexedImageSummary(
                            id=record.id,
                            filename=record.filename,
                            relative_path=record.relative_path,
                            status="processed",
                            tags=record.tags,
                            description=record.description,
                            taken_at=record.taken_at,
                            place_name=record.place_name,
                            country=record.country,
                        )
                    )
            except Exception as exc:  # pragma: no cover - isolate one unsafe/bad source
                failed.append(
                    IndexedImageSummary(
                        id=f"failed_{relative_path.stem}",
                        filename=relative_path.name,
                        relative_path=str(relative_path),
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
            quality_scores = score_image_bytes(raw_bytes, text=combined_text)

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
                aesthetic_score=quality_scores.aesthetic_score,
                aesthetic_model=quality_scores.model,
                technical_quality_score=quality_scores.technical_quality_score,
                aesthetic_updated_at=now_iso,
            )
            records.append(record)
            indexed.append(
                IndexedImageSummary(
                    id=record.id,
                    filename=record.filename,
                    relative_path=record.relative_path,
                    status="processed",
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
        pinned_root: PinnedLibraryRoot,
        indexing_request: IndexingRequest,
    ) -> list[Path]:
        library_root = pinned_root.root_path
        explicit_files = indexing_request.input.files
        if explicit_files:
            candidates = [
                self._relative_candidate(library_root, Path(file_path))
                for file_path in explicit_files
            ]
        else:
            candidates = collect_pinned_library_candidates(
                pinned_root,
                recursive=indexing_request.input.recursive,
            )

        filtered: list[Path] = []
        database_path = Path(
            os.path.abspath(self.settings.db_path.expanduser())
        )
        try:
            database_relative_path: Path | None = database_path.relative_to(
                library_root
            )
        except ValueError:
            database_relative_path = None
        for candidate in sorted(candidates):
            if database_relative_path is not None and candidate == database_relative_path:
                continue
            if is_supported_image(candidate):
                filtered.append(candidate)

        if indexing_request.limit is not None:
            filtered = filtered[: indexing_request.limit]
        return filtered

    @staticmethod
    def _relative_candidate(library_root: Path, candidate: Path) -> Path:
        lexical_path = candidate.expanduser()
        if not lexical_path.is_absolute():
            lexical_path = library_root / lexical_path
        normalized = Path(os.path.abspath(lexical_path))
        try:
            relative_path = normalized.relative_to(library_root)
        except ValueError as exc:
            raise ValueError("Indexing files must stay inside the active library root.") from exc
        if not relative_path.parts or any(
            part in {"", ".", ".."} for part in relative_path.parts
        ):
            raise ValueError("Indexing file path is not a normalized library source.")
        return relative_path

    def _encode_combined_text(self, combined_text: str) -> bytes | None:
        try:
            embedding = self.text_embedding_service.encode_document(combined_text)
        except Exception:
            return None
        return embedding.astype("float32").tobytes()
