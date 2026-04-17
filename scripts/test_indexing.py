from __future__ import annotations

import argparse
import base64
import mimetypes
import json
import os
import sys
from pathlib import Path

from tqdm import tqdm


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from src import create_app  # noqa: E402
from indexing.files import is_supported_image  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke test the Flask indexing endpoint against a local image folder."
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Optional path to config.yaml.",
    )
    parser.add_argument(
        "--vision-profile",
        default=None,
        help="Override the vision VLM profile from config.yaml.",
    )
    parser.add_argument(
        "--vlm-profile",
        dest="legacy_vlm_profile",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--image-dir",
        default=None,
        help="Image directory to index. Defaults to the resolved IMAGE_LIBRARY_DIR.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=3,
        help="Maximum number of images to index for the test run.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Override the VLM model name.",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Recompute entries even if a matching SHA256 already exists in the DB.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="Recursively scan the image directory.",
    )
    parser.add_argument(
        "files",
        nargs="*",
        help="Optional explicit file names or absolute paths to index.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.config:
        os.environ["APP_CONFIG_PATH"] = args.config
    selected_profile = args.vision_profile or args.legacy_vlm_profile
    if selected_profile:
        os.environ["VISION_VLM_PROFILE"] = selected_profile

    app = create_app()
    settings = app.config["SETTINGS"]
    image_dir = Path(args.image_dir or settings.image_library_dir).expanduser().resolve()
    candidates = _collect_candidates(
        image_dir=image_dir,
        files=args.files,
        recursive=args.recursive,
        limit=args.limit,
    )

    aggregated = {
        "object": "image_index.batch",
        "model": args.model or settings.vision_model,
        "image_dir": str(image_dir),
        "indexed": [],
        "skipped": [],
        "errors": [],
    }

    exit_code = 0
    with app.test_client() as client:
        for image_path in tqdm(candidates, desc="Indexing images"):
            payload = _build_single_image_payload(
                image_path=image_path,
                image_dir=image_dir,
                model=args.model or settings.vision_model,
                reindex=args.reindex,
            )
            response = client.post("/v1/indexing/jobs", json=payload)
            body = response.get_json() or {}
            if response.status_code != 200:
                exit_code = 1
            aggregated["indexed"].extend(body.get("data", []))
            aggregated["skipped"].extend(body.get("skipped", []))
            aggregated["errors"].extend(body.get("errors", []))

    aggregated["meta"] = {
        "indexed_count": len(aggregated["indexed"]),
        "skipped_count": len(aggregated["skipped"]),
        "error_count": len(aggregated["errors"]),
    }
    print(json.dumps(aggregated, indent=2, ensure_ascii=False))
    return exit_code


def _collect_candidates(
    *,
    image_dir: Path,
    files: list[str],
    recursive: bool,
    limit: int,
) -> list[Path]:
    if files:
        candidates = []
        for file_path in files:
            candidate = Path(file_path)
            if not candidate.is_absolute():
                candidate = image_dir / candidate
            candidates.append(candidate.resolve())
    else:
        pattern = "**/*" if recursive else "*"
        candidates = [path for path in image_dir.glob(pattern) if path.is_file()]

    filtered = [path for path in sorted(candidates) if is_supported_image(path)]
    return filtered[:limit]


def _build_single_image_payload(
    *,
    image_path: Path,
    image_dir: Path,
    model: str,
    reindex: bool,
) -> dict[str, object]:
    mime_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    try:
        relative_path = str(image_path.resolve().relative_to(image_dir))
    except ValueError:
        relative_path = image_path.name

    return {
        "model": model,
        "reindex": reindex,
        "input": {
            "image": {
                "filename": image_path.name,
                "relative_path": relative_path,
                "mime_type": mime_type,
                "b64": b64,
            }
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
