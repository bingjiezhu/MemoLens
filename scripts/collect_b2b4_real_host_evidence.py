#!/usr/bin/env python3
"""Cold-read a persistent B2B4 fixture into a path-free evidence summary.

The collector verifies the canonical Blueprint, Coverage, and Timeline ledgers
with production repository code before projecting operations and receipts.  It
does not infer that a UI was visible, that a model initiated a tool call, or
that a human performed the required native gestures.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.media_db import SCHEMA_VERSION, MediaRepository, canonical_json  # noqa: E402
from scripts.prepare_b2b4_real_host_fixture import (  # noqa: E402
    DIRECTIONS,
    FIXTURE_DATABASE_SCHEMA_VERSION,
    FIXTURE_OBJECT,
    FIXTURE_SCHEMA_VERSION,
    MANIFEST_NAME,
)


EVIDENCE_OBJECT = "memolens.b2b4_real_host_ledger_evidence"
EVIDENCE_SCHEMA_VERSION = "1"
_HEAD_FIELDS = {
    "blueprint": ("revision", "content_sha256", "semantic_sha256", "operation_id"),
    "coverage": (
        "revision",
        "content_sha256",
        "evidence_manifest_sha256",
        "operation_id",
    ),
    "timeline": (
        "revision",
        "revision_sha256",
        "timeline_id",
        "timeline_content_sha256",
        "blueprint_binding",
        "coverage_binding",
        "operation_id",
    ),
}
_FORBIDDEN_SUMMARY_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "credential",
    "proof",
    "nonce",
    "path",
    "file",
    "directory",
)


class EvidenceCollectionError(RuntimeError):
    pass


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _closed_mapping(
    value: object,
    *,
    required: set[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != required:
        raise EvidenceCollectionError(f"{label} has an unsupported shape")
    return value


def _load_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / MANIFEST_NAME
    try:
        observed = manifest_path.lstat()
    except OSError as exc:
        raise EvidenceCollectionError("fixture manifest is unavailable") from exc
    if not stat.S_ISREG(observed.st_mode) or stat.S_ISLNK(observed.st_mode):
        raise EvidenceCollectionError("fixture manifest must be a regular file")
    if observed.st_size > 1_048_576:
        raise EvidenceCollectionError("fixture manifest is too large")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceCollectionError("fixture manifest is invalid JSON") from exc
    manifest = _closed_mapping(
        value,
        required={
            "object",
            "schema_version",
            "fixture_id",
            "created_at",
            "evidence_scope",
            "acceptance_claimed",
            "database",
            "library",
            "host_state_dirs",
            "projects",
        },
        label="fixture manifest",
    )
    if (
        manifest["object"] != FIXTURE_OBJECT
        or manifest["schema_version"] != FIXTURE_SCHEMA_VERSION
        or manifest["fixture_id"] != root.name
        or manifest["evidence_scope"] != "fixture_only_not_real_host_acceptance"
        or manifest["acceptance_claimed"] is not False
    ):
        raise EvidenceCollectionError("fixture manifest identity is invalid")
    return manifest


def _resolve_member(root: Path, relative: object, *, kind: str) -> Path:
    if type(relative) is not str or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise EvidenceCollectionError(f"fixture {kind} locator is invalid")
    candidate = root.joinpath(*Path(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvidenceCollectionError(f"fixture {kind} is unavailable") from exc
    if not resolved.is_relative_to(root):
        raise EvidenceCollectionError(f"fixture {kind} escapes the fixture root")
    try:
        mode = candidate.lstat().st_mode
    except OSError as exc:
        raise EvidenceCollectionError(f"fixture {kind} is unavailable") from exc
    if stat.S_ISLNK(mode):
        raise EvidenceCollectionError(f"fixture {kind} cannot be a symlink")
    return resolved


def _head_projection(kind: str, head: Mapping[str, object]) -> dict[str, object]:
    try:
        return {field: head[field] for field in _HEAD_FIELDS[kind]}
    except KeyError as exc:
        raise EvidenceCollectionError(f"canonical {kind} head is incomplete") from exc


def _operation_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    fields: tuple[str, ...],
    project_id: str,
) -> list[dict[str, object]]:
    rows = connection.execute(
        f"SELECT {','.join(fields)} FROM {table} WHERE project_id=? ORDER BY sequence",
        (project_id,),
    ).fetchall()
    return [{field: row[field] for field in fields} for row in rows]


def _receipt_rows(
    connection: sqlite3.Connection,
    *,
    table: str,
    operation_table: str,
    fields: tuple[str, ...],
    project_id: str,
    condition: str = "",
) -> list[dict[str, object]]:
    selected = ",".join(f"receipt.{field}" for field in fields)
    rows = connection.execute(
        f"SELECT {selected} FROM {table} receipt "
        f"JOIN {operation_table} operation "
        "ON operation.project_id=receipt.project_id "
        "AND operation.id=receipt.operation_id "
        f"WHERE receipt.project_id=?{condition} ORDER BY operation.sequence",
        (project_id,),
    ).fetchall()
    return [{field: row[field] for field in fields} for row in rows]


def _timeline_revision_one(
    repository: MediaRepository,
    connection: sqlite3.Connection,
    project_id: str,
) -> dict[str, object]:
    selected = repository.get_trusted_canonical_timeline_revision_in_transaction(
        connection,
        project_id,
        1,
    )
    if selected is None:
        raise EvidenceCollectionError("canonical Timeline revision one is missing")
    return _head_projection("timeline", selected)


def _current_image_clips(timeline_head: Mapping[str, object]) -> list[dict[str, object]]:
    timeline = timeline_head.get("timeline")
    tracks = timeline.get("tracks") if isinstance(timeline, dict) else None
    clips = (
        tracks[0].get("clips")
        if isinstance(tracks, list) and len(tracks) == 1 and isinstance(tracks[0], dict)
        else None
    )
    if not isinstance(clips, list):
        raise EvidenceCollectionError("canonical Timeline clip projection is invalid")
    result: list[dict[str, object]] = []
    for clip in clips:
        if not isinstance(clip, dict) or clip.get("media_kind") != "image":
            raise EvidenceCollectionError("fixture Timeline no longer has an image-only track")
        result.append(
            {
                field: clip[field]
                for field in (
                    "clip_id",
                    "ordinal",
                    "beat_id",
                    "assignment_id",
                    "asset_id",
                    "start_ms",
                    "end_ms",
                    "media_kind",
                )
            }
        )
    return result


def _progress_state(*, revision: int, paired_receipts: int) -> str:
    if revision == 1 and paired_receipts == 0:
        return "baseline_ready"
    if revision == 2 and paired_receipts == 1:
        return "one_paired_save_observed"
    if revision >= 3 and paired_receipts >= 2:
        return "two_paired_saves_observed_not_ui_accepted"
    return "unexpected_ledger_shape"


def _assert_path_and_secret_free(
    value: object,
    *,
    forbidden_roots: tuple[Path, ...],
    at: str = "$",
) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_SUMMARY_KEY_FRAGMENTS):
                raise EvidenceCollectionError(f"evidence summary contains a forbidden key at {at}")
            _assert_path_and_secret_free(
                nested,
                forbidden_roots=forbidden_roots,
                at=f"{at}.{key}",
            )
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_path_and_secret_free(
                nested,
                forbidden_roots=forbidden_roots,
                at=f"{at}[{index}]",
            )
    elif isinstance(value, str):
        if value.startswith(("/", "file:")) or any(str(root) in value for root in forbidden_roots):
            raise EvidenceCollectionError(f"evidence summary contains a filesystem locator at {at}")


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (canonical_json(summary) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o644)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def collect_evidence(
    fixture_root: Path,
    *,
    collected_at: str | None = None,
    output: Path | None = None,
) -> dict[str, object]:
    """Cold-audit a fixture and return a commit-safe ledger projection."""

    supplied_root = fixture_root.expanduser()
    try:
        supplied_mode = supplied_root.lstat().st_mode
    except OSError as exc:
        raise EvidenceCollectionError("fixture root is unavailable") from exc
    if not stat.S_ISDIR(supplied_mode) or stat.S_ISLNK(supplied_mode):
        raise EvidenceCollectionError("fixture root must be a real directory")
    root = supplied_root.resolve(strict=True)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise EvidenceCollectionError("fixture root permissions are not 0700")
    manifest = _load_manifest(root)
    database = _closed_mapping(
        manifest["database"],
        required={"relative_file", "database_uuid", "schema_version", "journal_mode"},
        label="fixture database",
    )
    if SCHEMA_VERSION != FIXTURE_DATABASE_SCHEMA_VERSION:
        raise EvidenceCollectionError("collector requires the current V20 schema")
    if (
        database["schema_version"] != FIXTURE_DATABASE_SCHEMA_VERSION
        or database["journal_mode"] != "wal"
    ):
        raise EvidenceCollectionError(
            f"fixture manifest does not require V{FIXTURE_DATABASE_SCHEMA_VERSION}/WAL"
        )
    db_path = _resolve_member(root, database["relative_file"], kind="database")
    if not db_path.is_file():
        raise EvidenceCollectionError("fixture database is not a regular file")

    host_rows = _closed_mapping(
        manifest["host_state_dirs"],
        required={"codex", "deepseek"},
        label="host state directories",
    )
    host_permissions: dict[str, str] = {}
    for host in ("codex", "deepseek"):
        row = _closed_mapping(
            host_rows[host],
            required={"relative_dir", "mode"},
            label=f"{host} state directory",
        )
        host_dir = _resolve_member(root, row["relative_dir"], kind=f"{host} state")
        observed_mode = stat.S_IMODE(host_dir.stat().st_mode)
        if not host_dir.is_dir() or row["mode"] != "0700" or observed_mode != 0o700:
            raise EvidenceCollectionError(f"{host} state directory permissions changed")
        host_permissions[host] = "0700"

    project_values = manifest["projects"]
    if not isinstance(project_values, list) or len(project_values) != len(DIRECTIONS):
        raise EvidenceCollectionError("fixture project inventory is invalid")
    project_by_direction: dict[str, dict[str, object]] = {}
    for raw in project_values:
        project = _closed_mapping(
            raw,
            required={
                "direction",
                "project_id",
                "title",
                "initial_heads",
                "editable_image_clips",
            },
            label="fixture project",
        )
        direction = project["direction"]
        if type(direction) is not str or direction not in DIRECTIONS or direction in project_by_direction:
            raise EvidenceCollectionError("fixture project direction is invalid")
        if type(project["project_id"]) is not str or type(project["title"]) is not str:
            raise EvidenceCollectionError("fixture project identity is invalid")
        project_by_direction[direction] = project
    if tuple(project_by_direction) != DIRECTIONS:
        raise EvidenceCollectionError("fixture projects are not in canonical direction order")

    repository = MediaRepository(db_path)
    project_summaries: list[dict[str, object]] = []
    try:
        with repository.transaction() as connection:
            meta = connection.execute(
                "SELECT database_uuid,schema_version FROM database_meta WHERE singleton=1"
            ).fetchone()
            if (
                meta is None
                or str(meta["database_uuid"]) != database["database_uuid"]
                or int(meta["schema_version"]) != FIXTURE_DATABASE_SCHEMA_VERSION
            ):
                raise EvidenceCollectionError("fixture database identity changed")
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
            if journal_mode != "wal":
                raise EvidenceCollectionError("fixture database is not in WAL mode")
            observed_projects = {
                str(row["id"]): str(row["title"])
                for row in connection.execute("SELECT id,title FROM creative_projects ORDER BY id").fetchall()
            }
            expected_projects = {
                str(project["project_id"]): str(project["title"]) for project in project_by_direction.values()
            }
            if observed_projects != expected_projects:
                raise EvidenceCollectionError("fixture project inventory changed")

            for direction in DIRECTIONS:
                fixture_project = project_by_direction[direction]
                project_id = str(fixture_project["project_id"])
                initial_heads = _closed_mapping(
                    fixture_project["initial_heads"],
                    required={"blueprint", "coverage", "timeline"},
                    label=f"{direction} initial heads",
                )
                blueprint_ledger = repository.read_blueprint_workspace_ledger_in_transaction(
                    connection,
                    project_id,
                    limit=500,
                )
                if blueprint_ledger is None:
                    raise EvidenceCollectionError("fixture Blueprint ledger is missing")
                blueprint_head = blueprint_ledger.head
                coverage_head = repository.get_coverage_head_in_transaction(
                    connection,
                    project_id,
                    validated_blueprint_ledger=blueprint_ledger,
                )
                timeline_head = repository.get_canonical_timeline_head_in_transaction(
                    connection,
                    project_id,
                    validated_blueprint_ledger=blueprint_ledger,
                )
                if coverage_head is None or timeline_head is None:
                    raise EvidenceCollectionError("fixture canonical heads are missing")
                blueprint_projection = _head_projection("blueprint", blueprint_head)
                coverage_projection = _head_projection("coverage", coverage_head)
                timeline_projection = _head_projection("timeline", timeline_head)
                if blueprint_projection != initial_heads["blueprint"]:
                    raise EvidenceCollectionError("fixture Blueprint head changed")
                if coverage_projection != initial_heads["coverage"]:
                    raise EvidenceCollectionError("fixture Coverage head changed")
                if _timeline_revision_one(repository, connection, project_id) != initial_heads["timeline"]:
                    raise EvidenceCollectionError("fixture Timeline baseline changed")

                blueprint_operations = _operation_rows(
                    connection,
                    table="creative_blueprint_operations",
                    fields=(
                        "id",
                        "sequence",
                        "parent_operation_id",
                        "command_type",
                        "command_version",
                        "intent_code",
                        "input_sha256",
                        "output_sha256",
                        "result_kind",
                        "result_revision",
                        "result_content_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                blueprint_receipts = _receipt_rows(
                    connection,
                    table="blueprint_command_receipts",
                    operation_table="creative_blueprint_operations",
                    fields=(
                        "authenticated_principal",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "operation_id",
                        "response_status",
                        "response_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                coverage_operations = _operation_rows(
                    connection,
                    table="coverage_plan_operations",
                    fields=(
                        "id",
                        "sequence",
                        "parent_operation_id",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "result_revision",
                        "result_content_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                coverage_receipts = _receipt_rows(
                    connection,
                    table="coverage_plan_receipts",
                    operation_table="coverage_plan_operations",
                    fields=(
                        "authenticated_principal",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "operation_id",
                        "response_status",
                        "response_sha256",
                        "receipt_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                timeline_operations = _operation_rows(
                    connection,
                    table="canonical_timeline_operations",
                    fields=(
                        "id",
                        "sequence",
                        "parent_operation_id",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "result_revision",
                        "result_revision_sha256",
                        "result_timeline_content_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                timeline_desktop_receipts = _receipt_rows(
                    connection,
                    table="canonical_timeline_receipts",
                    operation_table="canonical_timeline_operations",
                    fields=(
                        "authenticated_principal",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "operation_id",
                        "response_status",
                        "response_sha256",
                        "receipt_sha256",
                        "created_at",
                    ),
                    project_id=project_id,
                )
                timeline_paired_receipts = _receipt_rows(
                    connection,
                    table="agent_project_command_receipts",
                    operation_table="canonical_timeline_operations",
                    fields=(
                        "id",
                        "capability_id",
                        "claimed_client_label",
                        "command_type",
                        "command_version",
                        "request_sha256",
                        "operation_id",
                        "response_status",
                        "response_sha256",
                        "receipt_sha256",
                        "resource_type",
                        "receipt_schema_version",
                        "request_capture",
                        "created_at",
                    ),
                    project_id=project_id,
                    condition=(
                        " AND receipt.resource_type='canonical_timeline' AND receipt.receipt_schema_version='2'"
                    ),
                )
                project_summaries.append(
                    {
                        "direction": direction,
                        "project_id": project_id,
                        "title": fixture_project["title"],
                        "ledger_progress": _progress_state(
                            revision=int(timeline_projection["revision"]),
                            paired_receipts=len(timeline_paired_receipts),
                        ),
                        "blueprint": {
                            "head": blueprint_projection,
                            "operations": blueprint_operations,
                            "desktop_receipts": blueprint_receipts,
                        },
                        "coverage": {
                            "head": coverage_projection,
                            "operations": coverage_operations,
                            "desktop_receipts": coverage_receipts,
                        },
                        "timeline": {
                            "initial_head": initial_heads["timeline"],
                            "current_head": timeline_projection,
                            "current_image_clips": _current_image_clips(timeline_head),
                            "operations": timeline_operations,
                            "desktop_receipts": timeline_desktop_receipts,
                            "paired_receipts": timeline_paired_receipts,
                        },
                    }
                )
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_rows:
                raise EvidenceCollectionError("fixture foreign-key check failed")
    except EvidenceCollectionError:
        raise
    except (KeyError, RuntimeError, sqlite3.Error, TypeError, ValueError) as exc:
        raise EvidenceCollectionError("canonical production audit failed") from exc
    finally:
        repository.close()

    summary: dict[str, object] = {
        "object": EVIDENCE_OBJECT,
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "fixture_id": manifest["fixture_id"],
        "collected_at": collected_at or _utc_timestamp(),
        "database_uuid": database["database_uuid"],
        "database_schema_version": FIXTURE_DATABASE_SCHEMA_VERSION,
        "journal_mode": "wal",
        "host_state_modes": host_permissions,
        "foreign_key_check": {"ok": True, "violation_count": 0},
        "evidence_scope": "cold_canonical_ledger_projection_only",
        "manual_acceptance": {
            "claimed": False,
            "reason": "native UI visibility, model initiation, and human gestures require separate evidence",
        },
        "projects": project_summaries,
    }
    _assert_path_and_secret_free(summary, forbidden_roots=(root, db_path))
    if output is not None:
        _write_summary(output.expanduser().resolve(), summary)
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Cold-audit a B2B4 real-host fixture without claiming UI acceptance.")
    parser.add_argument("fixture_root", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path for the path-free, secret-free JSON summary.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    summary = collect_evidence(args.fixture_root, output=args.output)
    print(json.dumps(summary, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
