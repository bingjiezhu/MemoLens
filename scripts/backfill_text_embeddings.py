from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.config import Settings  # noqa: E402
from core.text_embeddings import TextEmbeddingService, build_combined_text  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill combined_text and combined_text_embedding for existing indexed rows."
    )
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
    settings = Settings.from_env()
    encoder = TextEmbeddingService(settings)

    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row

    sql = """
        SELECT
            id,
            description,
            tags_json,
            place_name,
            country,
            combined_text,
            combined_text_embedding
        FROM image_index
    """
    params: list[object] = []
    where = []
    if not args.recompute:
        where.append("(combined_text_embedding IS NULL OR length(combined_text_embedding) = 0)")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at ASC"
    if args.limit is not None:
        sql += " LIMIT ?"
        params.append(args.limit)

    rows = connection.execute(sql, params).fetchall()
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
        connection.execute(
            """
            UPDATE image_index
            SET combined_text = ?, text_embedding_model = ?, combined_text_embedding = ?
            WHERE id = ?
            """,
            (
                combined_text,
                settings.text_embedding_model_id,
                embedding,
                row["id"],
            ),
        )
        updated += 1

    connection.commit()
    print(json.dumps({"updated_rows": updated, "db_path": str(settings.db_path)}, indent=2))
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
