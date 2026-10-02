#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INPUT_MD="${ROOT_DIR}/docs/project/MemoLens_说明文档.md"
OUTPUT_DIR="${ROOT_DIR}/output/pdf"
HTML_OUT="${OUTPUT_DIR}/MemoLens_说明文档.html"
PDF_OUT="${OUTPUT_DIR}/MemoLens_说明文档.pdf"
CSS_FILE="${ROOT_DIR}/docs/project/styles/memolens-doc.css"
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
RENDER_SCRIPT="${ROOT_DIR}/scripts/render_memolens_doc.py"

mkdir -p "${OUTPUT_DIR}"

if [[ ! -f "${INPUT_MD}" ]]; then
  echo "Missing Markdown source: ${INPUT_MD}" >&2
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Missing Python runtime in .venv: ${PYTHON_BIN}" >&2
  exit 1
fi

if [[ ! -f "${RENDER_SCRIPT}" ]]; then
  echo "Missing renderer script: ${RENDER_SCRIPT}" >&2
  exit 1
fi

if command -v pandoc >/dev/null 2>&1 && [[ -f "${CSS_FILE}" ]]; then
  pandoc "${INPUT_MD}" \
    --from=markdown+pipe_tables+backtick_code_blocks \
    --to=html5 \
    --standalone \
    --self-contained \
    --toc \
    --toc-depth=2 \
    --metadata=pagetitle:"MemoLens 产品说明文档" \
    --metadata=toc-title:"目录" \
    --css="${CSS_FILE}" \
    --resource-path="${ROOT_DIR}:${ROOT_DIR}/docs/project" \
    --output="${HTML_OUT}"
fi

"${PYTHON_BIN}" "${RENDER_SCRIPT}" "${INPUT_MD}" "${PDF_OUT}"

echo "HTML preview: ${HTML_OUT}"
echo "PDF: ${PDF_OUT}"
