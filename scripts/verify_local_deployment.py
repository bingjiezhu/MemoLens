from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    state_dir = Path(tempfile.mkdtemp(prefix="memolens-verify-state-"))
    photos_dir = state_dir / "photos"
    photos_dir.mkdir(parents=True, exist_ok=True)
    image_path = photos_dir / "quiet_beach_sunset.jpg"
    Image.new("RGB", (48, 32), color=(210, 180, 140)).save(image_path, format="JPEG")
    note_path = photos_dir / "notes.txt"
    note_path.write_text("not an image", encoding="utf-8")

    os.environ["MEMOLENS_APP_STATE_DIR"] = str(state_dir)
    for env_name in (
        "MINIMAX_KEY",
        "OPENAI_API_KEY",
        "DASHSCOPE_API_KEY",
        "VERTEX_ACCESS_TOKEN",
        "GOOGLE_OAUTH_ACCESS_TOKEN",
        "VERTEX_PROJECT",
        "GOOGLE_CLOUD_PROJECT",
        "GCP_PROJECT",
    ):
        os.environ[env_name] = ""
    os.environ.pop("IMAGE_LIBRARY_DIR", None)
    os.environ.pop("SQLITE_DB_PATH", None)

    from backend.src import create_app
    from frontend.querying.retrieval import RetrievalService

    app = create_app()
    client = app.test_client()

    settings_response = client.put(
        "/v1/settings",
        json={
            "image_library_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "photo_index.db"),
            "vision_profile_name": "minimax_vl01",
            "query_profile_name": "minimax_m27",
        },
    )
    invalid_settings_response = client.put(
        "/v1/settings",
        json={
            "image_library_dir": str(state_dir / "missing-photo-dir"),
        },
    )
    index_response = client.post(
        "/v1/indexing/jobs",
        json={
            "image_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "photo_index.db"),
            "persist_to_server": True,
            "reindex": True,
        },
    )
    renamed_dir = photos_dir / "renamed"
    renamed_dir.mkdir(parents=True, exist_ok=True)
    relocated_image_path = renamed_dir / "quiet_beach_sunset_renamed.jpg"
    image_path.rename(relocated_image_path)
    relocated_index_response = client.post(
        "/v1/indexing/jobs",
        json={
            "image_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "photo_index.db"),
            "persist_to_server": True,
            "reindex": False,
        },
    )
    invalid_index_db_response = client.post(
        "/v1/indexing/jobs",
        json={
            "image_dir": str(photos_dir),
            "db_path": str(photos_dir),
            "persist_to_server": True,
            "reindex": True,
        },
    )
    query_response = client.post(
        "/v1/retrieval/query",
        json={
            "text": "quiet beach sunset",
            "top_k": 3,
            "include_copy": False,
            "image_library_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "photo_index.db"),
        },
    )
    chinese_query_response = client.post(
        "/v1/retrieval/query",
        json={
            "text": "不要人像的海边照片",
            "top_k": 3,
            "include_copy": False,
            "image_library_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "photo_index.db"),
        },
    )
    missing_db_query_response = client.post(
        "/v1/retrieval/query",
        json={
            "text": "quiet beach sunset",
            "top_k": 3,
            "include_copy": False,
            "image_library_dir": str(photos_dir),
            "db_path": str(state_dir / "storage" / "missing.db"),
        },
    )
    copy_response = client.post(
        "/v1/retrieval/copy",
        json={
            "query_text": "quiet beach sunset",
            "image_library_dir": str(photos_dir),
            "images": query_response.json["data"][:3],
        },
    )
    from core.db import ImageIndexRepository
    repository = ImageIndexRepository(state_dir / "storage" / "photo_index.db")
    repository.ensure_schema()
    stored_candidates = repository.fetch_candidates()
    image_file_response = client.get(
        f"/v1/library/files/renamed/{relocated_image_path.name}",
        query_string={"root_path": str(photos_dir)},
    )
    note_file_response = client.get(
        f"/v1/library/files/{note_path.name}",
        query_string={"root_path": str(photos_dir)},
    )
    cors_options_response = client.open(
        "/v1/settings",
        method="OPTIONS",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    blocked_origin_response = client.get(
        "/healthz",
        headers={
            "Origin": "https://example.com",
        },
    )
    from backend.src.api.routes import _is_local_remote_addr
    excluded_filter_active = RetrievalService._should_exclude_candidate(
        excluded_terms=["people", "portrait"],
        normalized_tag_terms=["portrait"],
        normalized_candidate_terms=["portrait", "face", "person"],
    )

    result = {
        "settings_status": settings_response.status_code,
        "invalid_settings_status": invalid_settings_response.status_code,
        "index_status": index_response.status_code,
        "relocated_index_status": relocated_index_response.status_code,
        "invalid_index_db_status": invalid_index_db_response.status_code,
        "indexed_count": index_response.json["meta"]["indexed_count"],
        "index_has_records": "records" in (index_response.json or {}),
        "relocated_skip_message": ((relocated_index_response.json or {}).get("skipped") or [{}])[0].get("message"),
        "stored_relative_path": stored_candidates[0]["relative_path"] if stored_candidates else None,
        "query_status": query_response.status_code,
        "query_result_status": query_response.json["status"],
        "query_candidate_count": len(query_response.json["data"]),
        "query_has_generated_copy": query_response.json.get("generated_copy") is not None,
        "chinese_query_status": chinese_query_response.status_code,
        "chinese_query_result_status": chinese_query_response.json["status"],
        "chinese_query_candidate_count": len(chinese_query_response.json["data"]),
        "missing_db_query_status": missing_db_query_response.status_code,
        "copy_status": copy_response.status_code,
        "copy_title_present": isinstance(copy_response.json.get("title"), str)
        or copy_response.json.get("title") is None,
        "copy_caption_present": isinstance(copy_response.json.get("caption"), str),
        "image_file_status": image_file_response.status_code,
        "note_file_status": note_file_response.status_code,
        "generated_copy_model": (copy_response.json.get("generated_copy") or {}).get("model"),
        "cors_methods": cors_options_response.headers.get("Access-Control-Allow-Methods"),
        "cors_origin": cors_options_response.headers.get("Access-Control-Allow-Origin"),
        "blocked_origin_allowed": blocked_origin_response.headers.get("Access-Control-Allow-Origin"),
        "local_remote_addr_check": _is_local_remote_addr("127.0.0.1"),
        "remote_remote_addr_check": _is_local_remote_addr("203.0.113.5"),
        "excluded_filter_active": excluded_filter_active,
    }

    print(json.dumps(result, indent=2))

    if result["settings_status"] != 200:
        return 1
    if result["invalid_settings_status"] != 400:
        return 1
    if result["index_status"] != 200 or result["indexed_count"] < 1:
        return 1
    if result["relocated_index_status"] != 200:
        return 1
    if result["invalid_index_db_status"] != 400:
        return 1
    if result["index_has_records"] is not False:
        return 1
    if result["relocated_skip_message"] != "already indexed (path updated)":
        return 1
    if result["stored_relative_path"] != "renamed/quiet_beach_sunset_renamed.jpg":
        return 1
    if result["query_status"] != 200 or result["query_candidate_count"] < 1:
        return 1
    if result["query_has_generated_copy"] is not False:
        return 1
    if result["chinese_query_status"] != 200 or result["chinese_query_candidate_count"] < 1:
        return 1
    if result["missing_db_query_status"] != 400:
        return 1
    if result["copy_status"] != 200 or result["copy_caption_present"] is not True:
        return 1
    if result["image_file_status"] != 200 or result["note_file_status"] != 404:
        return 1
    if result["cors_origin"] != "http://127.0.0.1:5173":
        return 1
    if "PUT" not in str(result["cors_methods"] or ""):
        return 1
    if result["blocked_origin_allowed"] is not None:
        return 1
    if result["local_remote_addr_check"] is not True:
        return 1
    if result["remote_remote_addr_check"] is not False:
        return 1
    if result["excluded_filter_active"] is not True:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
