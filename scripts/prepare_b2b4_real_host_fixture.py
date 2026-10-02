#!/usr/bin/env python3
"""Prepare a persistent B2B4 fixture for real Codex/DeepSeek host journeys.

This script only prepares canonical production data.  It does not pair an
agent, approve a native confirmation, drive either host, or claim T053/T054
acceptance.  The resulting directory is intentionally under the repository's
gitignored ``tmp/`` tree and can be reopened by the normal MemoLens runtime.
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import struct
import sys
from types import SimpleNamespace
import zlib


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from backend.src.media.coverage import CoverageService  # noqa: E402
from backend.src.media.image_analysis import ImageAnalysisJobProcessor  # noqa: E402
from backend.src.media.timeline_lowering import TimelineLoweringService  # noqa: E402
from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import (  # noqa: E402
    SCHEMA_VERSION,
    MediaRepository,
    canonical_json,
)


DIRECTIONS = ("codex_then_deepseek", "deepseek_then_codex")
MANIFEST_NAME = "fixture-manifest.json"
FIXTURE_OBJECT = "memolens.b2b4_real_host_fixture"
FIXTURE_SCHEMA_VERSION = "1"
FIXTURE_DATABASE_SCHEMA_VERSION = 20
IMAGE_RUNTIME_GENERATION = f"runtime_generation_{'f' * 64}"
IMAGE_ANALYSIS_PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}
IMAGE_EMBEDDING_DIMENSIONS = 8
_FIXTURE_DIRECTORY = re.compile(r"^b2b4-real-host-[0-9]{8}T[0-9]{6}Z$")
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
_FORBIDDEN_MANIFEST_KEY_FRAGMENTS = (
    "secret",
    "token",
    "password",
    "proof",
    "nonce",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def default_fixture_root(*, now: datetime | None = None) -> Path:
    observed = (now or _utc_now()).astimezone(timezone.utc)
    slug = observed.strftime("%Y%m%dT%H%M%SZ")
    return (PROJECT_ROOT / "tmp" / f"b2b4-real-host-{slug}").resolve()


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _solid_png(red: int, green: int, blue: int, *, size: int = 64) -> bytes:
    """Return one deterministic, dependency-free RGB PNG."""

    row = b"\x00" + bytes((red, green, blue)) * size
    pixels = row * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(pixels, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _head_projection(kind: str, head: dict[str, object]) -> dict[str, object]:
    return {field: head[field] for field in _HEAD_FIELDS[kind]}


def _semantic(
    direction: str,
    *,
    opening_ref: str,
    ending_ref: str,
) -> dict[str, object]:
    """Fixture content for the existing Blueprint contract, not a new schema."""

    return {
        "intent": {
            "goal": f"verify the canonical editor handoff {direction}",
            "stance": "the same project must survive a fresh host handoff",
            "audience": "MemoLens acceptance reviewers",
            "platform": "short-video",
        },
        "script": {
            "blocks": [
                {
                    "block_id": "opening",
                    "text": f"Open the shared first cut for {direction}.",
                },
                {
                    "block_id": "ending",
                    "text": "The next fresh host continues from the saved head.",
                },
            ]
        },
        "direction": {
            "theme": "cross-host continuity",
            "narrative_arc": "first host to second host",
            "emotion": "calm",
            "tone": "precise",
            "pace": "measured",
        },
        "output": {"duration_target_ms": 8_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [
            {
                "hint_id": "opening_image",
                "evidence_ref": opening_ref,
                "script_block_ids": ["opening"],
                "reason": "Use the exact synthetic opening image.",
            },
            {
                "hint_id": "ending_image",
                "evidence_ref": ending_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact synthetic ending image.",
            },
        ],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def _register_image(
    repository: MediaRepository,
    *,
    library: Path,
    root_id: str,
    filename: str,
    content: bytes,
) -> dict[str, object]:
    image_path = library / filename
    image_path.write_bytes(content)
    image_path.chmod(0o600)
    observed = image_path.stat()
    digest = hashlib.sha256(content).hexdigest()
    asset = repository.upsert_asset_source(
        root_id=root_id,
        relative_path=filename,
        filename=filename,
        kind="image",
        sha256=digest,
        mime_type="image/png",
        file_size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        source_file_id=str(observed.st_ino),
    )
    asset_id = str(asset["id"])
    source_id = str(asset["asset_source_id"])
    database = os.stat(repository.db_path, follow_symlinks=False)
    job = repository.enqueue_image_analysis(
        runtime_generation=IMAGE_RUNTIME_GENERATION,
        database_file_identity=(int(database.st_dev), int(database.st_ino)),
        asset_id=asset_id,
        source_id=source_id,
        analysis_profile=IMAGE_ANALYSIS_PROFILE,
        expected_head=None,
        enqueue_scope="b2b4-real-host-fixture/image-analysis",
        idempotency_key=f"b2b4-real-host-image-{asset_id}",
    )
    ImageAnalysisJobProcessor(
        repository,
        embedding_service=SimpleNamespace(
            backend="semantic_hash",
            semantic_vector_dimensions=IMAGE_EMBEDDING_DIMENSIONS,
        ),
    ).run(
        str(job["id"]),
        runtime_generation=IMAGE_RUNTIME_GENERATION,
        expected_attempt=int(job["attempt"]),
        cancel_requested=lambda: False,
    )
    current = repository.get_current_image_analysis(asset_id)
    if (
        current is None
        or current.get("source") != {"source_id": source_id, "availability": "available"}
        or not isinstance(current.get("analysis_binding"), dict)
        or not isinstance(current.get("projection"), dict)
        or current["projection"].get("status") != "current"
        or current["projection"].get("outcome") != "applied"
    ):
        raise RuntimeError("fixture image did not reach the current canonical projection")
    return asset


def _assert_no_manifest_secrets(value: object, *, at: str = "$") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            folded = str(key).casefold()
            if any(fragment in folded for fragment in _FORBIDDEN_MANIFEST_KEY_FRAGMENTS):
                raise ValueError(f"fixture manifest contains a credential-shaped key at {at}")
            _assert_no_manifest_secrets(nested, at=f"{at}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _assert_no_manifest_secrets(nested, at=f"{at}[{index}]")


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    _assert_no_manifest_secrets(manifest)
    encoded = (canonical_json(manifest) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.partial")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def prepare_fixture(
    fixture_root: Path,
    *,
    created_at: datetime | None = None,
) -> dict[str, object]:
    """Create one persistent canonical fixture at a previously absent path."""

    root = fixture_root.expanduser().resolve()
    if _FIXTURE_DIRECTORY.fullmatch(root.name) is None:
        raise ValueError("fixture directory must be named b2b4-real-host-<UTC>")
    if root.exists():
        raise FileExistsError(f"fixture root already exists: {root}")
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private_directory(root)
    authority = root / "authority"
    library = root / "library"
    hosts = root / "hosts"
    _private_directory(authority)
    _private_directory(library)
    _private_directory(hosts)
    host_dirs: dict[str, Path] = {}
    for host in ("codex", "deepseek"):
        state_dir = hosts / host
        _private_directory(state_dir)
        host_dirs[host] = state_dir

    db_path = authority / "media.db"
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    projects: list[dict[str, object]] = []
    try:
        repository.ensure_schema(library)
        if SCHEMA_VERSION != FIXTURE_DATABASE_SCHEMA_VERSION:
            raise RuntimeError("B2B4 real-host fixture requires the current V20 schema")
        root_id = str(repository.register_library_root(library)["id"])
        blueprints = BlueprintService(repository)
        coverage = CoverageService(repository)
        timelines = TimelineLoweringService(repository)
        palette = {
            "codex_then_deepseek": ((44, 110, 210), (56, 178, 136)),
            "deepseek_then_codex": ((125, 83, 190), (226, 143, 62)),
        }
        for direction in DIRECTIONS:
            assets: list[dict[str, object]] = []
            for beat, color in zip(("opening", "ending"), palette[direction], strict=True):
                assets.append(
                    _register_image(
                        repository,
                        library=library,
                        root_id=root_id,
                        filename=f"{direction}-{beat}.png",
                        content=_solid_png(*color),
                    )
                )
            evidence_refs = [f"memolens://evidence/asset/{asset['id']}" for asset in assets]
            title = f"B2B4 real host: {direction}"
            project = repository.create_project(
                title,
                {
                    "goal": f"real host canonical editor handoff {direction}",
                    "duration_ms": 8_000,
                    "aspect_ratio": "9:16",
                    "candidate_refs": [
                        {
                            "result_type": "image",
                            "id": asset["id"],
                            "asset_id": asset["id"],
                        }
                        for asset in assets
                    ],
                },
                {
                    "created_by": "prepare_b2b4_real_host_fixture",
                    "acceptance_claimed": False,
                },
            )
            project_id = str(project["id"])
            brief = repository.get_brief(project_id, 1)
            if brief is None:
                raise RuntimeError("fixture project brief was not persisted")
            blueprint_result = blueprints.commit_proposal(
                project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": _semantic(
                        direction,
                        opening_ref=evidence_refs[0],
                        ending_ref=evidence_refs[1],
                    ),
                },
                idempotency_key=f"b2b4-real-blueprint-{direction}",
            )
            if blueprint_result.response_status != 201:
                raise RuntimeError("fixture Blueprint did not create revision one")
            blueprint_head = repository.get_blueprint_head(project_id)
            if blueprint_head is None or blueprint_head["revision"] != 1:
                raise RuntimeError("fixture Blueprint head is not revision one")
            coverage_result = coverage.materialize_baseline(
                project_id,
                {
                    "expected_blueprint": {
                        field: blueprint_head[field]
                        for field in (
                            "revision",
                            "content_sha256",
                            "semantic_sha256",
                        )
                    },
                    "expected_plan_head": None,
                },
                idempotency_key=f"b2b4-real-coverage-{direction}",
                expected_database_uuid=repository.database_uuid,
            )
            if coverage_result.response_status != 201:
                raise RuntimeError("fixture Coverage did not create revision one")
            coverage_head = repository.get_coverage_head(project_id)
            if coverage_head is None or coverage_head["revision"] != 1:
                raise RuntimeError("fixture Coverage head is not revision one")
            timeline_result = timelines.materialize_first_cut(
                project_id,
                {
                    "expected_blueprint": {
                        field: blueprint_head[field]
                        for field in (
                            "revision",
                            "content_sha256",
                            "semantic_sha256",
                            "operation_id",
                        )
                    },
                    "expected_coverage": {
                        field: coverage_head[field]
                        for field in (
                            "revision",
                            "content_sha256",
                            "evidence_manifest_sha256",
                            "operation_id",
                        )
                    },
                    "expected_timeline_head": None,
                },
                idempotency_key=f"b2b4-real-timeline-{direction}",
                expected_database_uuid=repository.database_uuid,
            )
            if timeline_result.response_status != 201:
                raise RuntimeError("fixture Timeline did not create revision one")
            workspace = timelines.read(project_id)
            timeline_head = workspace.get("head")
            timeline = workspace.get("timeline")
            if (
                not isinstance(timeline_head, dict)
                or timeline_head.get("revision") != 1
                or workspace.get("freshness") != {"state": "current", "reasons": []}
                or not isinstance(timeline, dict)
            ):
                raise RuntimeError("fixture Timeline workspace is not a current revision one")
            tracks = timeline.get("tracks")
            clips = (
                tracks[0].get("clips")
                if isinstance(tracks, list) and len(tracks) == 1 and isinstance(tracks[0], dict)
                else None
            )
            if (
                not isinstance(clips, list)
                or len(clips) != 2
                or any(not isinstance(clip, dict) or clip.get("media_kind") != "image" for clip in clips)
            ):
                raise RuntimeError("fixture Timeline must expose two editable image clips")
            projects.append(
                {
                    "direction": direction,
                    "project_id": project_id,
                    "title": title,
                    "initial_heads": {
                        "blueprint": _head_projection("blueprint", blueprint_head),
                        "coverage": _head_projection("coverage", coverage_head),
                        "timeline": _head_projection("timeline", timeline_head),
                    },
                    "editable_image_clips": [
                        {
                            "clip_id": clip["clip_id"],
                            "beat_id": clip["beat_id"],
                            "assignment_id": clip["assignment_id"],
                            "asset_id": clip["asset_id"],
                        }
                        for clip in clips
                    ],
                }
            )
        database_uuid = repository.database_uuid
    finally:
        repository.close()

    db_path.chmod(0o600)
    with closing(sqlite3.connect(db_path)) as connection:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        meta = connection.execute("SELECT database_uuid,schema_version FROM database_meta WHERE singleton=1").fetchone()
    if journal_mode != "wal" or foreign_keys or meta is None:
        raise RuntimeError("fixture database did not close as a valid WAL database")
    if (
        str(meta[0]) != database_uuid
        or int(meta[1]) != FIXTURE_DATABASE_SCHEMA_VERSION
    ):
        raise RuntimeError("fixture database identity changed before publication")

    observed_at = (created_at or _utc_now()).astimezone(timezone.utc)
    manifest: dict[str, object] = {
        "object": FIXTURE_OBJECT,
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "fixture_id": root.name,
        "created_at": observed_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "evidence_scope": "fixture_only_not_real_host_acceptance",
        "acceptance_claimed": False,
        "database": {
            "relative_file": "authority/media.db",
            "database_uuid": database_uuid,
            "schema_version": FIXTURE_DATABASE_SCHEMA_VERSION,
            "journal_mode": "wal",
        },
        "library": {"relative_dir": "library", "synthetic_media_only": True},
        "host_state_dirs": {
            host: {
                "relative_dir": state_dir.relative_to(root).as_posix(),
                "mode": "0700",
            }
            for host, state_dir in host_dirs.items()
        },
        "projects": projects,
    }
    _write_manifest(root / MANIFEST_NAME, manifest)
    if stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RuntimeError("fixture root is not private")
    return manifest


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare a persistent V20/WAL fixture for manual real Codex and DeepSeek canonical-editor journeys."
        )
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Fresh directory under ./tmp named b2b4-real-host-<UTC>.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    root = (args.output_root or default_fixture_root()).expanduser().resolve()
    tmp_root = (PROJECT_ROOT / "tmp").resolve()
    if root.parent != tmp_root or _FIXTURE_DIRECTORY.fullmatch(root.name) is None:
        raise SystemExit("--output-root must be a direct ./tmp/b2b4-real-host-<UTC> directory")
    manifest = prepare_fixture(root)
    print(
        json.dumps(
            {
                "fixture_root": str(root),
                "manifest": str(root / MANIFEST_NAME),
                "database_uuid": manifest["database"]["database_uuid"],
                "acceptance_claimed": False,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
