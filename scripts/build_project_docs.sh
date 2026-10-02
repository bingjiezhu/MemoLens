#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${ROOT_DIR}/output/pdf"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
TEXT_RENDERER="${ROOT_DIR}/scripts/render_memolens_doc.py"
IMAGE_RENDERER="${ROOT_DIR}/scripts/render_project_image_pdf.py"
PROJECT_DOC="${ROOT_DIR}/docs/project/MemoLens_项目说明书.md"
PROJECT_IMAGE_DIR="${ROOT_DIR}/docs/project/assets/project-book/pages"

mkdir -p "${OUTPUT_DIR}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing Python runtime in .venv: ${PYTHON_BIN}" >&2
  exit 1
fi

"${ROOT_DIR}/scripts/build_memolens_doc.sh"
"${PYTHON_BIN}" "${TEXT_RENDERER}" "${PROJECT_DOC}" "${OUTPUT_DIR}/MemoLens_项目说明书.pdf"
"${PYTHON_BIN}" "${IMAGE_RENDERER}" "${PROJECT_IMAGE_DIR}" "${OUTPUT_DIR}/MemoLens_项目说明书_图像版.pdf"

echo "Project documentation PDFs:"
echo "${OUTPUT_DIR}/MemoLens_说明文档.pdf"
echo "${OUTPUT_DIR}/MemoLens_项目说明书.pdf"
echo "${OUTPUT_DIR}/MemoLens_项目说明书_图像版.pdf"
