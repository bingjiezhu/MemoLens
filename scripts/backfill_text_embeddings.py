from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if not sys.flags.isolated:
    print(
        "MemoLens SQLite writers require isolated Python (-I); use "
        "scripts/run_python.sh scripts/backfill_text_embeddings.py.",
        file=sys.stderr,
    )
    raise SystemExit(78)

from core.config import Settings  # noqa: E402
from core.db import (  # noqa: E402
    CANONICAL_IMAGE_REANALYSIS_GUIDANCE,
    ImageIndexRepository,
)
from core.sqlite_runtime import require_safe_sqlite_runtime  # noqa: E402
from core.text_embeddings import TextEmbeddingService, build_combined_text  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill combined_text and combined_text_embedding for existing indexed rows."
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite DB path.")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional row limit.",
    )
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="Recompute rows even if they already have combined_text_embedding.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_safe_sqlite_runtime()
    settings = Settings.from_env()
    db_path = (args.db_path or settings.db_path).expanduser().resolve()
    repository = ImageIndexRepository(db_path)
    try:
        repository.require_legacy_mutation_authority()
    except RuntimeError as exc:
        print(
            json.dumps(
                {
                    "error": str(exc),
                    "guidance": CANONICAL_IMAGE_REANALYSIS_GUIDANCE,
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 78
    repository.ensure_schema()
    encoder = TextEmbeddingService(settings)

    rows = repository.fetch_text_embedding_backfill_candidates(
        recompute=args.recompute,
        limit=args.limit,
    )
    updated = 0

    for row in rows:
        tags = _parse_tags(row["tags_json"])
        combined_text = str(row["combined_text"] or "").strip() or build_combined_text(
            description=str(row["description"] or ""),
            tags=tags,
            place_name=row["place_name"],
            country=row["country"],
            semantic_hints=settings.semantic_hints,
        )
        embedding = encoder.encode_document(combined_text).astype("float32").tobytes()
        repository.update_text_embedding(
            image_id=str(row["id"]),
            combined_text=combined_text,
            text_embedding_model=settings.text_embedding_model_id,
            combined_text_embedding=embedding,
        )
        updated += 1

    print(json.dumps({"updated_rows": updated, "db_path": str(db_path)}, indent=2))
    return 0


def _parse_tags(tags_json: object) -> list[str]:
    try:
        parsed = json.loads(str(tags_json or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(tag) for tag in parsed]


if __name__ == "__main__":
    raise SystemExit(main())
