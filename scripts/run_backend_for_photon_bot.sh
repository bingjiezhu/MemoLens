#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_LIBRARY_DIR="${IMAGE_LIBRARY_DIR:-${PROJECT_ROOT}/local-photo-library}"
SQLITE_DB_PATH="${SQLITE_DB_PATH:-${IMAGE_LIBRARY_DIR}/photo_index.db}"
VISION_VLM_PROFILE="${VISION_VLM_PROFILE:-minimax_vl01}"
QUERY_VLM_PROFILE="${QUERY_VLM_PROFILE:-minimax_m27}"
EMBEDDING_BACKEND="${EMBEDDING_BACKEND:-semantic_hash}"

export IMAGE_LIBRARY_DIR
export SQLITE_DB_PATH
export VISION_VLM_PROFILE
export QUERY_VLM_PROFILE
export EMBEDDING_BACKEND

cd "${PROJECT_ROOT}"
python3 backend/app.py
