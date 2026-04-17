from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.src.retrieval import (  # noqa: E402
    OpenAICompatibleQueryPlanner,
    RetrievalCopywriter,
    RetrievalService,
)
from core.config import Settings  # noqa: E402
from core.db import ImageIndexRepository  # noqa: E402
from core.schemas import RetrievalRequest  # noqa: E402
from core.text_embeddings import TextEmbeddingService  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a natural-language retrieval query against the local image index."
    )
    parser.add_argument(
        "text",
        help="Natural-language retrieval request.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to config.yaml.",
    )
    parser.add_argument(
        "--query-profile",
        default=None,
        help="Override the query-planner model profile from config.yaml.",
    )
    parser.add_argument(
        "--vlm-profile",
        dest="legacy_vlm_profile",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="Optional top-k override.",
    )
    parser.add_argument(
        "--copy-limit",
        type=int,
        default=9,
        help="How many retrieved images to copy into the current working directory.",
    )
    parser.add_argument(
        "--copywriter-image-limit",
        type=int,
        default=6,
        help="How many top retrieved images to send into the VLM copywriter.",
    )
    parser.add_argument(
        "--skip-copywriting",
        action="store_true",
        help="Skip the post-retrieval VLM copywriting step.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.config:
        os.environ["APP_CONFIG_PATH"] = args.config
    selected_profile = args.query_profile or args.legacy_vlm_profile
    if selected_profile:
        os.environ["QUERY_VLM_PROFILE"] = selected_profile

    settings = Settings.from_env()
    settings.ensure_directories()
    repository = ImageIndexRepository(settings.db_path)
    repository.ensure_schema()
    retrieval_service = RetrievalService(
        settings=settings,
        repository=repository,
        planner=OpenAICompatibleQueryPlanner(settings),
        text_embedding_service=TextEmbeddingService(settings),
    )
    copywriter = RetrievalCopywriter(settings)
    result = retrieval_service.run(
        RetrievalRequest(
            text=args.text,
            top_k=args.top_k,
        )
    )
    body = result.to_response()
    copied_files = _copy_top_results(
        body=body,
        image_library_dir=settings.image_library_dir,
        destination_dir=Path.cwd(),
        copy_limit=args.copy_limit,
    )
    if isinstance(body, dict):
        body["copied_files"] = copied_files
        body["generated_copy"] = None
        if (
            not args.skip_copywriting
            and result.status == "completed"
            and result.data
            and args.copywriter_image_limit > 0
        ):
            try:
                generated_copy = copywriter.generate(
                    query_text=args.text,
                    retrieved_images=result.data,
                    image_library_dir=settings.image_library_dir,
                    image_limit=args.copywriter_image_limit,
                )
                body["generated_copy"] = generated_copy.to_dict()
            except Exception as exc:
                body["copywriting_error"] = str(exc)
    print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0 if body.get("status") in {"completed", "cannot_fulfill"} else 1


def _copy_top_results(
    *,
    body: dict | None,
    image_library_dir: Path,
    destination_dir: Path,
    copy_limit: int,
) -> list[str]:
    if not isinstance(body, dict):
        return []
    if body.get("status") != "completed":
        return []
    if copy_limit <= 0:
        return []

    data = body.get("data")
    if not isinstance(data, list):
        return []

    copied_files: list[str] = []
    for index, item in enumerate(data[:copy_limit], start=1):
        if not isinstance(item, dict):
            continue

        relative_path = item.get("relative_path")
        filename = item.get("filename")
        if not isinstance(relative_path, str) or not relative_path.strip():
            continue
        if not isinstance(filename, str) or not filename.strip():
            filename = Path(relative_path).name

        source_path = (image_library_dir / relative_path).resolve()
        if not source_path.exists() or not source_path.is_file():
            continue

        destination_name = f"retrieval_{index:02d}_{Path(filename).name}"
        destination_path = _unique_destination(destination_dir / destination_name)
        shutil.copy2(source_path, destination_path)
        copied_files.append(str(destination_path))

    return copied_files


def _unique_destination(path: Path) -> Path:
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


if __name__ == "__main__":
    raise SystemExit(main())
