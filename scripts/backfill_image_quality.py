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
from core.db import ImageIndexRepository  # noqa: E402
from core.image_quality import QUALITY_MODEL_ID, score_image_file  # noqa: E402
from core.schemas import utc_now_iso  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill image aesthetic scores into SQLite. The default local scorer "
            "is dependency-light; external JSON can import LAION/NIMA scores."
        )
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Override SQLite DB path.")
    parser.add_argument("--image-dir", type=Path, default=None, help="Override image library root.")
    parser.add_argument("--limit", type=int, default=None, help="Optional row limit.")
    parser.add_argument("--force", action="store_true", help="Recompute rows that already have scores.")
    parser.add_argument(
        "--scorer",
        choices=("local", "external-json"),
        default="local",
        help="Use the local scorer, or import precomputed scores from JSON.",
    )
    parser.add_argument(
        "--scores-json",
        type=Path,
        default=None,
        help="JSON file for --scorer external-json.",
    )
    parser.add_argument(
        "--model-label",
        default=None,
        help="Model label to store for imported external scores, e.g. laion-aesthetic or nima.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    settings = Settings.from_env()
    db_path = (args.db_path or settings.db_path).expanduser().resolve()
    image_dir = (args.image_dir or settings.image_library_dir).expanduser().resolve()

    repository = ImageIndexRepository(db_path)
    repository.ensure_schema()

    external_scores = {}
    if args.scorer == "external-json":
        if args.scores_json is None:
            raise SystemExit("--scores-json is required when --scorer external-json.")
        external_scores = _load_external_scores(args.scores_json)

    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row

    sql = """
        SELECT
            id,
            filename,
            relative_path,
            description,
            tags_json,
            combined_text,
            aesthetic_score
        FROM image_index
    """
    where = []
    params: list[object] = []
    if not args.force:
        where.append("aesthetic_score IS NULL")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at ASC"
    if args.limit is not None:
        sql += " LIMIT ?"
        params.append(args.limit)

    rows = connection.execute(sql, params).fetchall()
    updated = 0
    skipped_missing_file = 0
    skipped_missing_external_score = 0
    model_label = args.model_label or (
        QUALITY_MODEL_ID if args.scorer == "local" else "external-aesthetic"
    )

    for row in rows:
        now_iso = utc_now_iso()
        technical_quality_score = None
        if args.scorer == "external-json":
            score = _lookup_external_score(external_scores, row)
            if score is None:
                skipped_missing_external_score += 1
                continue
        else:
            image_path = (image_dir / str(row["relative_path"] or "")).resolve()
            try:
                image_path.relative_to(image_dir)
            except ValueError:
                skipped_missing_file += 1
                continue
            if not image_path.exists() or not image_path.is_file():
                skipped_missing_file += 1
                continue
            quality_scores = score_image_file(image_path, text=_row_quality_text(row))
            score = quality_scores.aesthetic_score
            technical_quality_score = quality_scores.technical_quality_score

        connection.execute(
            """
            UPDATE image_index
            SET
                aesthetic_score = ?,
                aesthetic_model = ?,
                technical_quality_score = ?,
                aesthetic_updated_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                max(0.0, min(1.0, float(score))),
                model_label,
                technical_quality_score,
                now_iso,
                now_iso,
                row["id"],
            ),
        )
        updated += 1

    connection.commit()
    print(
        json.dumps(
            {
                "updated_rows": updated,
                "scorer": args.scorer,
                "model": model_label,
                "db_path": str(db_path),
                "image_dir": str(image_dir),
                "skipped_missing_file": skipped_missing_file,
                "skipped_missing_external_score": skipped_missing_external_score,
            },
            indent=2,
        )
    )
    return 0


def _row_quality_text(row: sqlite3.Row) -> str:
    tags = []
    try:
        parsed_tags = json.loads(str(row["tags_json"] or "[]"))
    except json.JSONDecodeError:
        parsed_tags = []
    if isinstance(parsed_tags, list):
        tags = [str(tag) for tag in parsed_tags]
    return " ".join(
        [
            str(row["filename"] or ""),
            str(row["description"] or ""),
            str(row["combined_text"] or ""),
            " ".join(tags),
        ]
    )


def _load_external_scores(path: Path) -> dict[str, float]:
    with path.expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    scores: dict[str, float] = {}
    if isinstance(payload, dict):
        records = payload.get("data") if isinstance(payload.get("data"), list) else None
        if records is None:
            for key, value in payload.items():
                score = _coerce_score(value)
                if score is not None:
                    scores[str(key)] = score
            return scores
    elif isinstance(payload, list):
        records = payload
    else:
        return scores

    for item in records or []:
        if not isinstance(item, dict):
            continue
        score = _coerce_score(
            item.get("aesthetic_score")
            if item.get("aesthetic_score") is not None
            else item.get("score")
        )
        if score is None:
            continue
        for key_name in ("id", "relative_path", "filename"):
            key = item.get(key_name)
            if isinstance(key, str) and key.strip():
                scores[key.strip()] = score
    return scores


def _lookup_external_score(scores: dict[str, float], row: sqlite3.Row) -> float | None:
    for key in (row["id"], row["relative_path"], row["filename"]):
        if key in scores:
            return scores[str(key)]
    return None


def _coerce_score(value: object) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return None
    if score > 1.0:
        score = score / 10.0 if score <= 10.0 else score / 100.0
    return max(0.0, min(1.0, score))


if __name__ == "__main__":
    raise SystemExit(main())
