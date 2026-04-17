from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class VisionMetadata:
    tags: list[str]
    description: str
    location_hint: str | None = None


@dataclass
class GeoMetadata:
    place_name: str | None = None
    country: str | None = None


@dataclass
class LocalImageMetadata:
    id: str
    sha256: str
    filename: str
    relative_path: str
    mime_type: str
    file_size: int
    width: int | None
    height: int | None
    taken_at: str | None
    lat: float | None
    lon: float | None
    altitude: float | None


@dataclass
class StoredImageRecord:
    id: str
    sha256: str
    filename: str
    relative_path: str
    mime_type: str
    file_size: int
    width: int | None
    height: int | None
    taken_at: str | None
    lat: float | None
    lon: float | None
    altitude: float | None
    place_name: str | None
    country: str | None
    description: str
    tags: list[str]
    combined_text: str
    text_embedding_model: str | None
    combined_text_embedding_blob: bytes | None
    embedding_backend: str
    embedding_blob: bytes
    created_at: str
    updated_at: str

    def to_transport_dict(self) -> dict[str, object]:
        return {
            "object": "stored_image_record",
            "id": self.id,
            "sha256": self.sha256,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "width": self.width,
            "height": self.height,
            "taken_at": self.taken_at,
            "lat": self.lat,
            "lon": self.lon,
            "altitude": self.altitude,
            "place_name": self.place_name,
            "country": self.country,
            "description": self.description,
            "tags": self.tags,
            "combined_text": self.combined_text,
            "text_embedding_model": self.text_embedding_model,
            "combined_text_embedding_b64": (
                base64.b64encode(self.combined_text_embedding_blob).decode("utf-8")
                if self.combined_text_embedding_blob is not None
                else None
            ),
            "embedding_backend": self.embedding_backend,
            "embedding_b64": base64.b64encode(self.embedding_blob).decode("utf-8"),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class UploadedImageInput:
    filename: str
    b64: str
    relative_path: str | None = None
    mime_type: str | None = None


@dataclass
class IndexingInput:
    image_dir: str | None = None
    files: list[str] = field(default_factory=list)
    recursive: bool = True
    image: UploadedImageInput | None = None


@dataclass
class IndexingRequest:
    model: str | None
    input: IndexingInput
    db_path: str | None = None
    reindex: bool = False
    limit: int | None = None
    persist_to_server: bool = False


@dataclass
class IndexedImageSummary:
    id: str
    filename: str
    relative_path: str
    status: str
    tags: list[str] = field(default_factory=list)
    description: str | None = None
    taken_at: str | None = None
    place_name: str | None = None
    country: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "object": "image_index_record",
            "id": self.id,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "status": self.status,
            "tags": self.tags,
            "description": self.description,
            "taken_at": self.taken_at,
            "place_name": self.place_name,
            "country": self.country,
            "message": self.message,
        }


@dataclass
class IndexingJobResult:
    id: str
    model: str | None
    embedding_backend: str
    image_dir: str
    db_path: str
    indexed: list[IndexedImageSummary]
    skipped: list[IndexedImageSummary]
    failed: list[IndexedImageSummary]
    records: list[StoredImageRecord]
    created: int
    status: str = "completed"

    def to_response(self, *, include_records: bool = False) -> dict[str, object]:
        response: dict[str, object] = {
            "id": self.id,
            "object": "image_index.job",
            "created": self.created,
            "status": self.status,
            "model": self.model,
            "data": [item.to_dict() for item in self.indexed],
            "skipped": [item.to_dict() for item in self.skipped],
            "errors": [item.to_dict() for item in self.failed],
            "meta": {
                "image_dir": self.image_dir,
                "db_path": self.db_path,
                "embedding_backend": self.embedding_backend,
                "indexed_count": len(self.indexed),
                "skipped_count": len(self.skipped),
                "error_count": len(self.failed),
            },
        }
        if include_records:
            response["records"] = [item.to_transport_dict() for item in self.records]
        return response


@dataclass
class RetrievalRequest:
    text: str
    top_k: int | None = None


@dataclass
class StructuredRetrievalQuery:
    top_k: int
    date_from: str | None
    date_to: str | None
    location_text: str | None
    descriptive_query: str | None
    required_terms: list[str]
    optional_terms: list[str]
    excluded_terms: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "top_k": self.top_k,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "location_text": self.location_text,
            "descriptive_query": self.descriptive_query,
            "required_terms": self.required_terms,
            "optional_terms": self.optional_terms,
            "excluded_terms": self.excluded_terms,
        }


@dataclass
class RetrievalPlan:
    can_fulfill: bool
    reason: str | None
    query: StructuredRetrievalQuery | None


@dataclass
class RetrievedImageSummary:
    id: str
    filename: str
    relative_path: str
    taken_at: str | None
    place_name: str | None
    country: str | None
    description: str
    tags: list[str]
    score: float
    matched_terms: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "object": "retrieved_image",
            "id": self.id,
            "filename": self.filename,
            "relative_path": self.relative_path,
            "taken_at": self.taken_at,
            "place_name": self.place_name,
            "country": self.country,
            "description": self.description,
            "tags": self.tags,
            "score": round(self.score, 4),
            "matched_terms": self.matched_terms,
        }


@dataclass
class RetrievalResponse:
    id: str
    query_text: str
    current_datetime: str
    parsed_query: StructuredRetrievalQuery | None
    data: list[RetrievedImageSummary]
    status: str
    message: str | None = None

    def to_response(self) -> dict[str, object]:
        return {
            "id": self.id,
            "object": "retrieval.query",
            "status": self.status,
            "message": self.message,
            "query_text": self.query_text,
            "current_datetime": self.current_datetime,
            "parsed_query": self.parsed_query.to_dict() if self.parsed_query else None,
            "data": [item.to_dict() for item in self.data],
        }


@dataclass
class GeneratedCopy:
    model: str
    title: str | None
    body: str
    highlights: list[str]
    image_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "object": "generated_copy",
            "model": self.model,
            "title": self.title,
            "body": self.body,
            "highlights": self.highlights,
            "image_count": self.image_count,
        }


def parse_indexing_request(
    payload: dict[str, object],
    default_image_dir: str,
    default_model: str,
) -> IndexingRequest:
    input_payload = payload.get("input") if isinstance(payload.get("input"), dict) else {}
    image_payload = input_payload.get("image") if isinstance(input_payload.get("image"), dict) else None
    image_dir = input_payload.get("image_dir") or payload.get("image_dir") or default_image_dir
    files = input_payload.get("files") or payload.get("files") or []
    recursive = input_payload.get("recursive")
    db_path = payload.get("db_path")

    if recursive is None:
        recursive = payload.get("recursive", True)

    if not isinstance(image_dir, str) or not image_dir.strip():
        raise ValueError("`image_dir` must be a non-empty string.")
    if not isinstance(files, list):
        raise ValueError("`files` must be a list of file paths.")
    if db_path is not None and (not isinstance(db_path, str) or not db_path.strip()):
        raise ValueError("`db_path` must be a non-empty string when set.")

    normalized_files = []
    for file_path in files:
        if not isinstance(file_path, str) or not file_path.strip():
            raise ValueError("Every `files` entry must be a non-empty string.")
        normalized_files.append(file_path)

    uploaded_image = None
    if image_payload is not None:
        filename = image_payload.get("filename")
        b64 = image_payload.get("b64")
        relative_path = image_payload.get("relative_path")
        mime_type = image_payload.get("mime_type")

        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("`input.image.filename` must be a non-empty string.")
        if not isinstance(b64, str) or not b64.strip():
            raise ValueError("`input.image.b64` must be a non-empty string.")
        if relative_path is not None and (
            not isinstance(relative_path, str) or not relative_path.strip()
        ):
            raise ValueError("`input.image.relative_path` must be a non-empty string when set.")
        if mime_type is not None and (not isinstance(mime_type, str) or not mime_type.strip()):
            raise ValueError("`input.image.mime_type` must be a non-empty string when set.")

        uploaded_image = UploadedImageInput(
            filename=filename.strip(),
            b64=b64.strip(),
            relative_path=relative_path.strip() if isinstance(relative_path, str) else None,
            mime_type=mime_type.strip() if isinstance(mime_type, str) else None,
        )

    limit = payload.get("limit")
    if limit is not None:
        if not isinstance(limit, int):
            raise ValueError("`limit` must be an integer.")
        if limit <= 0:
            raise ValueError("`limit` must be greater than 0.")

    reindex = payload.get("reindex", False)
    if not isinstance(reindex, bool):
        raise ValueError("`reindex` must be a boolean.")
    if not isinstance(recursive, bool):
        raise ValueError("`recursive` must be a boolean.")
    persist_to_server = payload.get("persist_to_server")
    if persist_to_server is None:
        persist_to_server = image_payload is None
    if not isinstance(persist_to_server, bool):
        raise ValueError("`persist_to_server` must be a boolean.")

    return IndexingRequest(
        model=payload.get("model") if isinstance(payload.get("model"), str) else default_model,
        input=IndexingInput(
            image_dir=image_dir,
            files=normalized_files,
            recursive=recursive,
            image=uploaded_image,
        ),
        db_path=db_path.strip() if isinstance(db_path, str) else None,
        reindex=reindex,
        limit=limit,
        persist_to_server=persist_to_server,
    )


def parse_retrieval_request(payload: dict[str, object]) -> RetrievalRequest:
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("`text` must be a non-empty string.")

    top_k = payload.get("top_k")
    if top_k is not None:
        if not isinstance(top_k, int):
            raise ValueError("`top_k` must be an integer.")
        if top_k <= 0:
            raise ValueError("`top_k` must be greater than 0.")

    return RetrievalRequest(text=text.strip(), top_k=top_k)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
