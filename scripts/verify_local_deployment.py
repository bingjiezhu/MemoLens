from __future__ import annotations

from io import BytesIO
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
    Image.new("RGB", (256, 128), color=(210, 180, 140)).save(image_path, format="JPEG")
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
    stored_candidate = stored_candidates[0] if stored_candidates else None
    image_file_response = client.get(
        f"/v1/library/files/renamed/{relocated_image_path.name}",
        query_string={"root_path": str(photos_dir)},
    )
    image_preview_response = client.get(
        f"/v1/library/previews/renamed/{relocated_image_path.name}",
        query_string={"root_path": str(photos_dir), "width": "128"},
    )
    preview_format = None
    preview_size: tuple[int, int] | None = None
    if image_preview_response.status_code == 200:
        with Image.open(BytesIO(image_preview_response.data)) as preview_image:
            preview_format = preview_image.format
            preview_size = preview_image.size
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
    expanded_people_exclusions = RetrievalService._expand_excluded_terms(["people"])
    prepared_people_exclusions = [
        normalized
        for _term, normalized in RetrievalService._prepare_query_terms(expanded_people_exclusions)
    ]
    child_filter_active = RetrievalService._should_exclude_candidate(
        excluded_terms=prepared_people_exclusions,
        normalized_tag_terms=RetrievalService._normalize_candidate_terms(["children"]),
        normalized_candidate_terms=RetrievalService._normalize_candidate_terms(
            ["two children walking"]
        ),
    )
    exclusion_only_plan = app.extensions["query_planner"]._fallback_plan(
        text="不要人",
        current_datetime="2026-04-30T12:00:00-07:00",
        top_k_override=3,
    )
    exclusion_only_query = exclusion_only_plan.query
    mountain_no_people_plan = app.extensions["query_planner"]._fallback_plan(
        text="9张有山景的图，但是不要有人",
        current_datetime="2026-04-30T12:00:00-07:00",
        top_k_override=9,
    )
    mountain_no_people_query = mountain_no_people_plan.query
    no_people_terms = RetrievalService._filter_absence_terms(
        normalized_candidate_terms=["no person", "person", "human", "mountain"],
        normalized_blob="no person mountain",
    )
    no_person_similarity = RetrievalService._pair_term_similarity("person", "no person")
    strict_driver_river_match = RetrievalService._strict_term_presence_normalized(
        normalized_term="driver",
        normalized_term_candidates=["river"],
    )
    mountain_required_groups = RetrievalService._build_hard_required_groups(["mountain"])
    mountain_group_accepts_mountain = RetrievalService._satisfies_hard_required_groups(
        hard_required_groups=mountain_required_groups,
        normalized_tag_terms=["landscape"],
        normalized_candidate_terms=["distant mountain", "forest"],
    )
    mountain_group_rejects_food = RetrievalService._satisfies_hard_required_groups(
        hard_required_groups=mountain_required_groups,
        normalized_tag_terms=["restaurant"],
        normalized_candidate_terms=["plate", "coffee"],
    )
    landscape_intent_active = RetrievalService._has_landscape_intent(["mountain", "landscape"])
    landscape_scene_score = RetrievalService._scene_composition_score(
        "wide shot mountain landscape sky"
    )
    close_scene_score = RetrievalService._scene_composition_score(
        "close up tree trunk mountain road"
    )
    disfavored_landscape_context = RetrievalService._is_disfavored_landscape_context(
        normalized_blob="classical landscape painting displayed in a frame with mountains",
        query_terms=["mountain", "landscape"],
    )
    allowed_landscape_context = RetrievalService._is_disfavored_landscape_context(
        normalized_blob="classical landscape painting displayed in a frame with mountains",
        query_terms=["mountain", "landscape", "painting"],
    )
    high_quality_metadata_score = RetrievalService._metadata_quality_score(
        "wide shot panoramic scenic mountain landscape clear sky natural light"
    )
    low_quality_metadata_score = RetrievalService._metadata_quality_score(
        "blurry dark close up obstructed tree trunk"
    )
    repeated_scene_similarity = RetrievalService._candidate_similarity(
        {
            "similarity_terms": ["meadow", "fence", "waterfall", "valley"],
            "embedding": None,
            "embedding_backend": "test",
        },
        {
            "similarity_terms": ["meadow", "fence", "waterfall", "valley"],
            "embedding": None,
            "embedding_backend": "test",
        },
    )
    distinct_scene_similarity = RetrievalService._candidate_similarity(
        {
            "similarity_terms": ["meadow", "fence", "waterfall", "valley"],
            "embedding": None,
            "embedding_backend": "test",
        },
        {
            "similarity_terms": ["coffee", "table", "restaurant", "plate"],
            "embedding": None,
            "embedding_backend": "test",
        },
    )
    high_quality_selection_score = RetrievalService._selection_score(
        relevance_score=0.7,
        quality_score=0.9,
    )
    low_quality_selection_score = RetrievalService._selection_score(
        relevance_score=0.7,
        quality_score=0.2,
    )
    deduped_quality_candidates = RetrievalService._dedupe_near_duplicate_candidates(
        [
            {
                "id": "lower_quality",
                "selection_score": low_quality_selection_score,
                "quality_score": 0.2,
                "normalized_score": 0.7,
                "similarity_terms": ["meadow", "fence", "waterfall", "valley"],
                "embedding": None,
                "embedding_backend": "test",
            },
            {
                "id": "higher_quality",
                "selection_score": high_quality_selection_score,
                "quality_score": 0.9,
                "normalized_score": 0.7,
                "similarity_terms": ["meadow", "fence", "waterfall", "valley"],
                "embedding": None,
                "embedding_backend": "test",
            },
        ]
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
        "stored_relative_path": stored_candidate["relative_path"] if stored_candidate else None,
        "stored_aesthetic_score": (
            stored_candidate["aesthetic_score"] if stored_candidate else None
        ),
        "stored_aesthetic_model": (
            stored_candidate["aesthetic_model"] if stored_candidate else None
        ),
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
        "image_preview_status": image_preview_response.status_code,
        "image_preview_content_type": image_preview_response.headers.get("Content-Type"),
        "image_preview_format": preview_format,
        "image_preview_width": preview_size[0] if preview_size else None,
        "note_file_status": note_file_response.status_code,
        "generated_copy_model": (copy_response.json.get("generated_copy") or {}).get("model"),
        "cors_methods": cors_options_response.headers.get("Access-Control-Allow-Methods"),
        "cors_origin": cors_options_response.headers.get("Access-Control-Allow-Origin"),
        "blocked_origin_allowed": blocked_origin_response.headers.get("Access-Control-Allow-Origin"),
        "local_remote_addr_check": _is_local_remote_addr("127.0.0.1"),
        "remote_remote_addr_check": _is_local_remote_addr("203.0.113.5"),
        "excluded_filter_active": excluded_filter_active,
        "child_filter_active": child_filter_active,
        "exclusion_only_can_fulfill": exclusion_only_plan.can_fulfill,
        "exclusion_only_required_terms": (
            exclusion_only_query.required_terms if exclusion_only_query else []
        ),
        "exclusion_only_excluded_terms": (
            exclusion_only_query.excluded_terms if exclusion_only_query else []
        ),
        "mountain_no_people_can_fulfill": mountain_no_people_plan.can_fulfill,
        "mountain_no_people_required_terms": (
            mountain_no_people_query.required_terms if mountain_no_people_query else []
        ),
        "mountain_no_people_excluded_terms": (
            mountain_no_people_query.excluded_terms if mountain_no_people_query else []
        ),
        "no_people_terms": no_people_terms,
        "no_person_similarity": no_person_similarity,
        "strict_driver_river_match": strict_driver_river_match,
        "mountain_group_accepts_mountain": mountain_group_accepts_mountain,
        "mountain_group_rejects_food": mountain_group_rejects_food,
        "landscape_intent_active": landscape_intent_active,
        "landscape_scene_score": landscape_scene_score,
        "close_scene_score": close_scene_score,
        "disfavored_landscape_context": disfavored_landscape_context,
        "allowed_landscape_context": allowed_landscape_context,
        "high_quality_metadata_score": high_quality_metadata_score,
        "low_quality_metadata_score": low_quality_metadata_score,
        "repeated_scene_similarity": repeated_scene_similarity,
        "distinct_scene_similarity": distinct_scene_similarity,
        "high_quality_selection_score": high_quality_selection_score,
        "low_quality_selection_score": low_quality_selection_score,
        "deduped_quality_candidate_ids": [
            item.get("id") for item in deduped_quality_candidates
        ],
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
    if result["stored_aesthetic_score"] is None:
        return 1
    if result["stored_aesthetic_model"] != "local_technical_aesthetic_v1":
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
    if (
        result["image_preview_status"] != 200
        or result["image_preview_format"] != "JPEG"
        or result["image_preview_width"] != 128
    ):
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
    if result["child_filter_active"] is not True:
        return 1
    if result["exclusion_only_can_fulfill"] is not True:
        return 1
    if "no people" not in result["exclusion_only_required_terms"]:
        return 1
    if "child" not in result["exclusion_only_excluded_terms"]:
        return 1
    if result["mountain_no_people_can_fulfill"] is not True:
        return 1
    if "mountain" not in result["mountain_no_people_required_terms"]:
        return 1
    if "landscape" not in result["mountain_no_people_required_terms"]:
        return 1
    if "person" not in result["mountain_no_people_excluded_terms"]:
        return 1
    if "person" in result["no_people_terms"] or "human" in result["no_people_terms"]:
        return 1
    if result["no_person_similarity"] != 0.0:
        return 1
    if result["strict_driver_river_match"] != 0.0:
        return 1
    if result["mountain_group_accepts_mountain"] is not True:
        return 1
    if result["mountain_group_rejects_food"] is not False:
        return 1
    if result["landscape_intent_active"] is not True:
        return 1
    if result["landscape_scene_score"] <= result["close_scene_score"]:
        return 1
    if result["disfavored_landscape_context"] is not True:
        return 1
    if result["allowed_landscape_context"] is not False:
        return 1
    if result["high_quality_metadata_score"] <= result["low_quality_metadata_score"]:
        return 1
    if result["repeated_scene_similarity"] < 0.99:
        return 1
    if result["distinct_scene_similarity"] != 0.0:
        return 1
    if result["high_quality_selection_score"] <= result["low_quality_selection_score"]:
        return 1
    if result["deduped_quality_candidate_ids"] != ["higher_quality"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
