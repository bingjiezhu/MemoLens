from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from core.timeline_lowering_contract import (
    TimelineLoweringContractError,
    canonical_json,
    canonical_sha256,
    canonical_timeline_content_sha256,
    require_valid_canonical_timeline,
    require_valid_timeline_source_bindings,
    timeline_source_bindings_sha256,
)
from core.residual_identity_contract import (
    ResidualIdentityContractError,
    require_valid_residual_binding,
)

from .render_plan import (
    ESTIMATED_RENDER_BYTES_PER_SECOND_720P,
    MIN_RENDER_FREE_BYTES,
    RenderPlan,
    build_assemble_command,
    build_clip_command,
    concat_manifest,
    validate_render_duration,
)
from .render_sources import freeze_still_image
from .video import MediaCancelled, MediaCapabilityError, ffprobe, resolve_binary, terminate_process


CANONICAL_EXPORT_ARTIFACTS_VERSION = "memolens.canonical-export-artifacts/v1"
CANONICAL_RENDER_PROFILE = "export-1080p"
PACKAGE_MANIFEST_OBJECT = "canonical_export.package_manifest"
PACKAGE_MARKER_OBJECT = "memolens.lightweight_export_package_completion"
RUNTIME_MANIFEST_OBJECT = "memolens.canonical_export_runtime_manifest"
ACTUAL_READ_MANIFEST_OBJECT = "memolens.canonical_actual_read_manifest"
PACKAGE_SCHEMA_VERSION = "1"

VIDEO_FILENAME = "video.mp4"
SCRIPT_FILENAME = "script.txt"
MANIFEST_FILENAME = "manifest.json"
USAGE_FILENAME = "使用清单.txt"
COMPLETION_MARKER_FILENAME = ".memolens-complete.json"
REQUIRED_PACKAGE_FILES = (
    VIDEO_FILENAME,
    SCRIPT_FILENAME,
    MANIFEST_FILENAME,
    USAGE_FILENAME,
    COMPLETION_MARKER_FILENAME,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID = re.compile(r"^(?:asset|img)_[0-9a-f]{24}$")
_ASSET_SOURCE_ID = re.compile(r"^src_[0-9a-f]{24}$")
_CLIP_ID = re.compile(r"^clip_[0-9a-f]{24}$")
_ANALYSIS_RUN_ID = re.compile(r"^arun_[0-9a-f]{32}$")
_SPAN_ID = re.compile(
    r"^seg_(?P<asset_digest>[0-9a-f]{24})_"
    r"(?P<analysis_revision>[1-9][0-9]{0,6})_"
    r"(?P<ordinal>0|[1-9][0-9]{0,6})$"
)
_RESIDUAL_EVIDENCE_REF = re.compile(
    r"^memolens://evidence/span/(?P<residual_id>rseg_[0-9a-f]{64})$"
)
_SAFE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_WINDOWS_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_PATH_KEYS = {
    "absolute_path",
    "canonical_path",
    "directory",
    "file_path",
    "path",
    "relative_path",
    "root_path",
}
_MAX_JSON_BYTES = 4 * 1024 * 1024
_MAX_MARKER_BYTES = 512 * 1024
_MAX_USAGE_BYTES = 8 * 1024 * 1024
_MAX_SCRIPT_BYTES = 4 * 1024 * 1024
_MAX_PROBE_BYTES = 8 * 1024 * 1024
_SOURCE_COPY_CHUNK = 1024 * 1024
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
PACKAGE_ROLES = (
    "final_video",
    "script",
    "package_manifest",
    "human_usage_list",
    "completion_marker",
)
_PACKAGE_MANIFEST_FIELDS = {
    "object",
    "schema_version",
    "export_id",
    "job_id",
    "project_id",
    "timeline_binding",
    "profile",
    "package_basename",
    "package_roles",
    "required_files",
    "video",
    "script",
    "usage_occurrences",
    "usage_sha256",
    "runtime_manifest",
    "runtime_manifest_sha256",
    "source_policy",
    "optional_roles",
}
_RUNTIME_MANIFEST_FIELDS = {
    "object",
    "schema_version",
    "renderer",
    "profile",
    "timeline_content_sha256",
    "timeline_source_bindings_sha256",
    "width",
    "height",
    "fps",
    "audio",
    "fit",
    "transitions",
    "actual_read_manifest",
    "actual_read_manifest_sha256",
    "runtime_identity",
}
_ACTUAL_READ_MANIFEST_FIELDS = {
    "object",
    "schema_version",
    "timeline_content_sha256",
    "source_bindings",
    "source_bindings_sha256",
    "snapshots",
}
_MASTER_PROBE_FIELDS = {
    "duration_ms",
    "width",
    "height",
    "rotation_degrees",
    "captured_at",
    "codec",
}
_MASTER_CODEC_FIELDS = {
    "format_name",
    "video_codec",
    "pixel_format",
    "avg_frame_rate",
    "time_base",
    "video_stream_index",
    "video_stream_indices",
    "audio_stream_index",
    "audio_stream_indices",
    "stream_selection",
    "audio_streams",
}
_SNAPSHOT_PROOF_FIELDS = {
    "ordinal",
    "clip_id",
    "asset_source_id",
    "asset_id",
    "asset_sha256",
    "size_bytes",
    "media_kind",
    "source_label",
    "source_label_role",
}
_SOURCE_BINDING_COMMON_FIELDS = {
    "clip_id",
    "evidence_ref",
    "asset_id",
    "asset_sha256",
    "asset_source_id",
    "coverage_proof_sha256",
    "media_kind",
}
_SOURCE_BINDING_IMAGE_FIELDS = _SOURCE_BINDING_COMMON_FIELDS | {
    "analysis_run_id",
    "analysis_revision",
    "analysis_content_sha256",
    "source_binding_sha256",
}
_SOURCE_BINDING_VIDEO_FIELDS = _SOURCE_BINDING_COMMON_FIELDS | {
    "span_id",
    "analysis_run_id",
    "analysis_revision",
    "input_asset_sha256",
    "source_in_ms",
    "source_out_ms",
}
_SOURCE_BINDING_RESIDUAL_VIDEO_FIELDS = _SOURCE_BINDING_VIDEO_FIELDS | {
    "residual_id",
    "parent_segment_id",
    "residual_binding",
}

Cancelled = Callable[[], bool]
FaultInjector = Callable[[str], None]
SourceResolver = Callable[[str], Mapping[str, object] | None]
CommandRunner = Callable[[Sequence[str], float, Cancelled], None]
MasterProbe = Callable[[Path], Mapping[str, object]]
ImageFreezer = Callable[[Path, Path], None]


class CanonicalExportArtifactError(RuntimeError):
    """Stable, non-reflective failure raised by the pure artifact executor."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CanonicalRenderPlan:
    """Path-free adapter from the canonical Timeline to the existing renderer."""

    render_plan: RenderPlan
    timeline_content_sha256: str
    source_bindings_sha256: str
    source_bindings: tuple[dict[str, object], ...]
    usage_occurrences: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class _FrozenSource:
    snapshot: Path
    record: dict[str, object]
    binding: dict[str, object]
    size_bytes: int
    sha256: str


def _fail(code: str, message: str) -> None:
    raise CanonicalExportArtifactError(code, message)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_fd(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, _SOURCE_COPY_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return digest.hexdigest(), total


def _open_master_artifact_no_follow(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise CanonicalExportArtifactError(
            "master_artifact_missing",
            "Canonical renderer did not produce a master artifact.",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("master_artifact_invalid", "Canonical export master is not a regular file.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _master_file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _require_held_master_unchanged(
    path: Path,
    descriptor: int,
    *,
    expected_identity: tuple[int, int, int, int, int],
) -> None:
    """Close a path-based probe against one no-follow held master inode."""

    try:
        held = os.fstat(descriptor)
        linked = os.stat(path, follow_symlinks=False)
    except (FileNotFoundError, OSError) as exc:
        raise CanonicalExportArtifactError(
            "master_artifact_changed",
            "Canonical export master changed during verification.",
        ) from exc
    if (
        not stat.S_ISREG(held.st_mode)
        or not stat.S_ISREG(linked.st_mode)
        or _master_file_identity(held) != expected_identity
        or _master_file_identity(linked) != expected_identity
    ):
        _fail("master_artifact_changed", "Canonical export master changed during verification.")


def _cancel_if_requested(cancelled: Cancelled) -> None:
    if cancelled():
        raise MediaCancelled("Canonical export was cancelled.")


def _inject(fault_inject: FaultInjector | None, stage: str) -> None:
    if fault_inject is not None:
        fault_inject(stage)


def _validate_safe_basename(value: object) -> str:
    if type(value) is not str or _SAFE_BASENAME.fullmatch(value) is None:
        _fail("invalid_package_basename", "Package name must be one safe basename.")
    if value in {".", ".."} or value.startswith("."):
        _fail("invalid_package_basename", "Package name must be one safe basename.")
    return value


def _looks_absolute(value: str) -> bool:
    if value.startswith("memolens://"):
        return False
    return value.startswith("/") or _WINDOWS_ABSOLUTE.match(value) is not None or value.startswith("file:")


def _require_path_free_json(value: object, *, code: str = "absolute_path_forbidden") -> None:
    """Reject filesystem authority from serializable manifests and proofs."""

    def visit(item: object) -> None:
        if item is None or type(item) in {bool, int, float}:
            return
        if type(item) is str:
            if _looks_absolute(item):
                _fail(code, "Serializable export evidence must not contain an absolute path.")
            return
        if type(item) is list:
            for child in item:
                visit(child)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    _fail("invalid_json_contract", "Serializable export evidence requires string keys.")
                if key.casefold() in _PATH_KEYS:
                    _fail(code, "Serializable export evidence must not contain filesystem paths.")
                visit(child)
            return
        _fail("invalid_json_contract", "Serializable export evidence must be canonical JSON.")

    visit(value)
    try:
        encoded = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CanonicalExportArtifactError(
            "invalid_json_contract",
            "Serializable export evidence must be canonical JSON.",
        ) from exc
    if len(encoded) > _MAX_JSON_BYTES:
        _fail("artifact_manifest_too_large", "Serializable export evidence exceeds the size limit.")


def _require_sha256(value: object, *, code: str) -> str:
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail(code, "A required SHA-256 digest is invalid.")
    return value


def _require_identifier(value: object, *, code: str) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        _fail(code, "A required export identity is invalid.")
    return value


def _validate_timeline_binding(value: object) -> dict[str, object]:
    fields = {
        "revision",
        "revision_sha256",
        "timeline_id",
        "timeline_content_sha256",
        "source_bindings_sha256",
        "blueprint_binding",
        "coverage_binding",
    }
    if type(value) is not dict or set(value) != fields:
        _fail("invalid_export_timeline_binding", "Export Timeline binding has an invalid closed shape.")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 1_000_000:
        _fail("invalid_export_timeline_binding", "Export Timeline revision is invalid.")
    for key in ("revision_sha256", "timeline_content_sha256", "source_bindings_sha256"):
        _require_sha256(value[key], code="invalid_export_timeline_binding")
    _require_identifier(value["timeline_id"], code="invalid_export_timeline_binding")
    nested_fields = {
        "blueprint_binding": {"revision", "content_sha256", "semantic_sha256", "operation_id"},
        "coverage_binding": {
            "revision",
            "content_sha256",
            "evidence_manifest_sha256",
            "operation_id",
        },
    }
    for key, expected_fields in nested_fields.items():
        nested = value[key]
        if type(nested) is not dict or set(nested) != expected_fields:
            _fail("invalid_export_timeline_binding", "Export upstream binding is invalid.")
        if type(nested["revision"]) is not int or not 1 <= nested["revision"] <= 1_000_000:
            _fail("invalid_export_timeline_binding", "Export upstream revision is invalid.")
        _require_sha256(nested["content_sha256"], code="invalid_export_timeline_binding")
        _require_identifier(nested["operation_id"], code="invalid_export_timeline_binding")
    _require_sha256(
        value["blueprint_binding"]["semantic_sha256"],
        code="invalid_export_timeline_binding",
    )
    _require_sha256(
        value["coverage_binding"]["evidence_manifest_sha256"],
        code="invalid_export_timeline_binding",
    )
    return deepcopy(value)


def _validate_export_context(value: object) -> dict[str, object]:
    fields = {"export_id", "job_id", "project_id", "timeline_binding", "profile"}
    if type(value) is not dict or set(value) != fields:
        _fail("invalid_export_context", "Export context has an invalid closed shape.")
    result = {
        "export_id": _require_identifier(value["export_id"], code="invalid_export_context"),
        "job_id": _require_identifier(value["job_id"], code="invalid_export_context"),
        "project_id": _require_identifier(value["project_id"], code="invalid_export_context"),
        "timeline_binding": _validate_timeline_binding(value["timeline_binding"]),
        "profile": value["profile"],
    }
    if result["profile"] != CANONICAL_RENDER_PROFILE:
        _fail("invalid_export_context", "Export profile must be export-1080p.")
    _require_path_free_json(result)
    return result


def _validate_script_text(value: object) -> bytes:
    if (
        type(value) is not str
        or not value
        or value.startswith("\ufeff")
        or "\x00" in value
        or "\r" in value
        or not value.endswith("\n")
        or value.endswith("\n\n")
    ):
        _fail("invalid_script_projection", "Script projection must be non-empty normalized UTF-8 text.")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise CanonicalExportArtifactError(
            "invalid_script_projection",
            "Script projection must be non-empty normalized UTF-8 text.",
        ) from exc
    if len(encoded) > _MAX_SCRIPT_BYTES:
        _fail("script_projection_too_large", "Script projection exceeds the size limit.")
    return encoded


def script_artifact_proof(script_text: str) -> dict[str, object]:
    """Return the exact Core manifest binding for a deterministic script projection."""

    encoded = _validate_script_text(script_text)
    return {"sha256": _sha256_bytes(encoded), "size_bytes": len(encoded)}


def project_script_text(blocks: Sequence[Mapping[str, object]]) -> str:
    """Project exact Blueprint script blocks into the single script.txt format."""

    if type(blocks) not in {list, tuple} or not blocks or len(blocks) > 256:
        _fail("invalid_script_blocks", "Script blocks must be one bounded non-empty array.")
    projected: list[str] = []
    for block in blocks:
        if type(block) is not dict or set(block) != {"block_id", "text"}:
            _fail("invalid_script_block", "A script block has an invalid closed shape.")
        _require_identifier(block["block_id"], code="invalid_script_block")
        text = block["text"]
        if type(text) is not str or not text or text.startswith("\ufeff") or "\x00" in text:
            _fail("invalid_script_block", "A script block has invalid text.")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        if not normalized.strip():
            _fail("invalid_script_block", "A script block must contain visible text.")
        projected.append(normalized)
    result = "\n\n".join(projected) + "\n"
    _validate_script_text(result)
    return result


def derive_usage_occurrences(
    timeline: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Derive exact path-free occurrence rows; never union repeated uses."""

    try:
        document = require_valid_canonical_timeline(timeline)
        bindings = require_valid_timeline_source_bindings(
            list(source_bindings),
            timeline=document,
        )
    except (TimelineLoweringContractError, TypeError, ValueError) as exc:
        raise CanonicalExportArtifactError(
            "canonical_timeline_contract_invalid",
            "Canonical Timeline or source bindings are invalid.",
        ) from exc
    clips = document["tracks"][0]["clips"]
    occurrences: list[dict[str, object]] = []
    for ordinal, (clip, binding) in enumerate(zip(clips, bindings, strict=True)):
        media_kind = str(clip["media_kind"])
        occurrence: dict[str, object] = {
            "ordinal": ordinal,
            "clip_id": clip["clip_id"],
            "asset_id": clip["asset_id"],
            "asset_sha256": clip["asset_sha256"],
            "asset_source_id": binding["asset_source_id"],
            "media_kind": media_kind,
            "timeline_start_ms": clip["start_ms"],
            "timeline_end_ms": clip["end_ms"],
            "source_start_ms": clip["source_in_ms"] if media_kind == "video" else None,
            "source_end_ms": clip["source_out_ms"] if media_kind == "video" else None,
            "source_binding_sha256": canonical_sha256(binding),
        }
        occurrences.append(occurrence)
    return occurrences


def _validate_occurrences(value: object) -> list[dict[str, object]]:
    if type(value) is not list or not value or len(value) > 256:
        _fail("invalid_usage_occurrences", "Usage occurrences must be one bounded non-empty array.")
    required = {
        "ordinal",
        "clip_id",
        "asset_id",
        "asset_sha256",
        "asset_source_id",
        "media_kind",
        "timeline_start_ms",
        "timeline_end_ms",
        "source_start_ms",
        "source_end_ms",
        "source_binding_sha256",
    }
    normalized: list[dict[str, object]] = []
    expected_timeline_start = 0
    for index, raw in enumerate(value):
        if type(raw) is not dict or set(raw) != required:
            _fail("invalid_usage_occurrence", "A usage occurrence has an invalid closed shape.")
        if raw["ordinal"] != index:
            _fail("invalid_usage_occurrence_order", "Usage occurrence order must be contiguous.")
        if type(raw["timeline_start_ms"]) is not int or type(raw["timeline_end_ms"]) is not int:
            _fail("invalid_usage_timeline_interval", "Timeline intervals must use integer milliseconds.")
        if int(raw["timeline_start_ms"]) < 0 or int(raw["timeline_end_ms"]) <= int(raw["timeline_start_ms"]):
            _fail("invalid_usage_timeline_interval", "Timeline intervals must be positive and half-open.")
        if raw["timeline_start_ms"] != expected_timeline_start:
            _fail("invalid_usage_timeline_interval", "Usage Timeline intervals must be contiguous.")
        expected_timeline_start = int(raw["timeline_end_ms"])
        if type(raw["clip_id"]) is not str or _CLIP_ID.fullmatch(raw["clip_id"]) is None:
            _fail("invalid_usage_occurrence", "Usage occurrence clip identity is invalid.")
        if type(raw["asset_id"]) is not str or _ASSET_ID.fullmatch(raw["asset_id"]) is None:
            _fail("invalid_usage_occurrence", "Usage occurrence asset identity is invalid.")
        if (
            type(raw["asset_source_id"]) is not str
            or _ASSET_SOURCE_ID.fullmatch(raw["asset_source_id"]) is None
        ):
            _fail("invalid_usage_occurrence", "Usage occurrence source identity is invalid.")
        media_kind = raw["media_kind"]
        if media_kind == "image":
            if raw["source_start_ms"] is not None or raw["source_end_ms"] is not None:
                _fail("image_source_interval_forbidden", "Image occurrences cannot have source intervals.")
        elif media_kind == "video":
            source_start = raw["source_start_ms"]
            source_end = raw["source_end_ms"]
            if (
                type(source_start) is not int
                or type(source_end) is not int
                or source_start < 0
                or source_end <= source_start
            ):
                _fail("invalid_video_source_interval", "Video occurrences require a strict half-open interval.")
            if source_end - source_start != int(raw["timeline_end_ms"]) - int(raw["timeline_start_ms"]):
                _fail("usage_interval_duration_mismatch", "Video source and Timeline intervals must match exactly.")
        else:
            _fail("unsupported_usage_media_kind", "Usage supports image and video occurrences only.")
        asset_sha256 = _require_sha256(raw["asset_sha256"], code="invalid_usage_asset_sha256")
        if raw["asset_id"] != f"{str(raw['asset_id']).split('_', 1)[0]}_{asset_sha256[:24]}":
            _fail("invalid_usage_occurrence", "Usage occurrence asset identity is not content addressed.")
        _require_sha256(raw["source_binding_sha256"], code="invalid_usage_source_binding_sha256")
        normalized.append(deepcopy(raw))
    _require_path_free_json(normalized)
    return normalized


def build_canonical_render_plan(
    timeline: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
) -> CanonicalRenderPlan:
    """Lower the closed B2B document into the existing FFmpeg clip contract."""

    try:
        document = require_valid_canonical_timeline(timeline)
        bindings = require_valid_timeline_source_bindings(
            list(source_bindings),
            timeline=document,
        )
        timeline_digest = canonical_timeline_content_sha256(document)
        bindings_digest = timeline_source_bindings_sha256(bindings, timeline=document)
    except (TimelineLoweringContractError, TypeError, ValueError) as exc:
        raise CanonicalExportArtifactError(
            "canonical_timeline_contract_invalid",
            "Canonical Timeline or source bindings are invalid.",
        ) from exc

    compiler = document.get("compiler")
    if compiler != {
        "id": "memolens.timeline-lowerer/v1",
        "media": "image_and_video_span",
        "transitions": "hard_cut",
        "audio": "silent",
        "subtitles": "none",
    }:
        _fail("unsupported_canonical_timeline", "Canonical Timeline capabilities are unsupported.")
    dimensions = {
        "16:9": (1920, 1080),
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
    }
    aspect_ratio = document["output"]["aspect_ratio"]
    if aspect_ratio not in dimensions:
        _fail("unsupported_aspect_ratio", "Canonical export aspect ratio is unsupported.")
    width, height = dimensions[str(aspect_ratio)]
    duration_ms = int(document["output"]["duration_ms"])
    clips: list[dict[str, object]] = []
    for clip, binding in zip(document["tracks"][0]["clips"], bindings, strict=True):
        media_kind = str(clip["media_kind"])
        if clip.get("fit") != "cover" or clip.get("audio_enabled") is not False:
            _fail("unsupported_canonical_clip", "Canonical export clip features are unsupported.")
        if media_kind not in {"image", "video"}:
            _fail("unsupported_canonical_clip", "Canonical export supports images and exact video spans only.")
        render_clip: dict[str, object] = {
            "kind": media_kind,
            "clip_id": clip["clip_id"],
            "asset_id": clip["asset_id"],
            "asset_source_id": binding["asset_source_id"],
            "source_in_ms": int(clip["source_in_ms"]) if media_kind == "video" else 0,
            "timeline_duration_ms": int(clip["end_ms"]) - int(clip["start_ms"]),
            "audio_enabled": False,
            "volume_db": 0.0,
            "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "fit": "cover",
        }
        if media_kind == "video" and (
            int(clip["source_out_ms"]) - int(clip["source_in_ms"])
            != render_clip["timeline_duration_ms"]
        ):
            _fail("invalid_video_source_interval", "Canonical video interval is not exact.")
        clips.append(render_clip)
    seconds = duration_ms / 1000
    pixel_ratio = max(0.25, width * height / (1280 * 720))
    estimated_bytes = int(seconds * ESTIMATED_RENDER_BYTES_PER_SECOND_720P * pixel_ratio * 2)
    plan = RenderPlan(
        profile=CANONICAL_RENDER_PROFILE,
        width=width,
        height=height,
        fps=30,
        background_color="0x000000",
        duration_ms=duration_ms,
        required_free_bytes=MIN_RENDER_FREE_BYTES + estimated_bytes,
        clips=tuple(clips),
        has_transitions=False,
    )
    occurrences = derive_usage_occurrences(document, bindings)
    return CanonicalRenderPlan(
        render_plan=plan,
        timeline_content_sha256=timeline_digest,
        source_bindings_sha256=bindings_digest,
        source_bindings=tuple(deepcopy(bindings)),
        usage_occurrences=tuple(occurrences),
    )


def _absolute_parts(path: Path) -> tuple[str, ...]:
    if not path.is_absolute():
        _fail("untrusted_root", "A trusted root must be absolute.")
    parts = path.parts
    if not parts or parts[0] != os.sep:
        _fail("untrusted_root", "A trusted root is invalid.")
    if any(part in {"", ".", ".."} for part in parts[1:]):
        _fail("untrusted_root", "A trusted root is invalid.")
    return tuple(parts[1:])


def _open_absolute_directory_no_follow(path: Path) -> int:
    """Open every root component with O_NOFOLLOW, not just the leaf."""

    parts = _absolute_parts(path)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail("untrusted_root", "A trusted root is not a directory.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _duplicate_directory_authority(
    descriptor: int,
    *,
    code: str,
    message: str,
) -> int:
    """Duplicate one caller-owned directory capability without reopening a locator."""

    if type(descriptor) is not int or descriptor < 0:
        _fail(code, message)
    try:
        duplicate = os.dup(descriptor)
    except OSError as exc:
        raise CanonicalExportArtifactError(code, message) from exc
    try:
        metadata = os.fstat(duplicate)
        if not stat.S_ISDIR(metadata.st_mode):
            _fail(code, message)
        return duplicate
    except BaseException:
        os.close(duplicate)
        raise


def _acquire_output_root_authority(
    *,
    output_root: Path | None,
    output_root_fd: int | None,
) -> int:
    """Acquire exactly one output-root authority.

    The fd form is the production authority boundary: the callee duplicates the
    caller's already-validated directory descriptor and never resolves its old
    pathname again. The Path form remains available for compatibility callers.
    """

    if (output_root is None) == (output_root_fd is None):
        _fail(
            "output_root_authority_invalid",
            "Exactly one verified output-root authority is required.",
        )
    if output_root_fd is not None:
        return _duplicate_directory_authority(
            output_root_fd,
            code="output_root_unavailable",
            message="Verified output root is unavailable.",
        )
    try:
        return _open_absolute_directory_no_follow(Path(output_root))
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise CanonicalExportArtifactError(
            "output_root_unavailable",
            "Verified output root is unavailable.",
        ) from exc


def _relative_parts(value: object) -> tuple[str, ...]:
    if type(value) is not str or not value or "\x00" in value or "\\" in value:
        _fail("source_locator_invalid", "Source locator is not a safe root-relative path.")
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        _fail("source_locator_invalid", "Source locator is not a safe root-relative path.")
    return tuple(path.parts)


def _open_relative_file_no_follow(root_descriptor: int, relative_path: object) -> int:
    parts = _relative_parts(relative_path)
    directory = os.dup(root_descriptor)
    try:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        for part in parts[:-1]:
            next_directory = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = next_directory
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(parts[-1], file_flags, dir_fd=directory)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            _fail("source_not_regular", "Canonical export source is not a regular file.")
        return descriptor
    except FileNotFoundError as exc:
        raise CanonicalExportArtifactError(
            "source_unavailable",
            "Canonical export source is unavailable.",
        ) from exc
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "source_locator_invalid",
            "Canonical export source could not be opened safely.",
        ) from exc
    finally:
        os.close(directory)


def _validate_source_record(
    binding: Mapping[str, object],
    record: Mapping[str, object] | None,
    metadata: os.stat_result,
) -> dict[str, object]:
    if record is None:
        _fail("source_unavailable", "Canonical export source is unavailable.")
    expected_id = binding["asset_source_id"]
    if record.get("id") != expected_id or record.get("asset_id") != binding["asset_id"]:
        _fail("source_identity_mismatch", "Canonical export source identity does not match its binding.")
    if record.get("sha256") != binding["asset_sha256"]:
        _fail("source_hash_binding_mismatch", "Canonical export source hash does not match its binding.")
    if record.get("availability") != "available" or record.get("kind") != binding["media_kind"]:
        _fail("source_unavailable", "Canonical export source is unavailable.")
    observed_size = record.get("observed_size")
    observed_mtime_ns = record.get("observed_mtime_ns")
    source_file_id = record.get("source_file_id")
    if (
        type(observed_size) is not int
        or observed_size != metadata.st_size
        or type(observed_mtime_ns) is not int
        or observed_mtime_ns != metadata.st_mtime_ns
        or type(source_file_id) is not str
        or source_file_id != str(metadata.st_ino)
    ):
        _fail("source_identity_mismatch", "Canonical export source filesystem identity changed.")
    if type(record.get("file_size")) is int and record["file_size"] != metadata.st_size:
        _fail("source_identity_mismatch", "Canonical export source size changed.")
    codec = record.get("codec")
    if type(codec) is not dict:
        _fail("source_probe_missing", "Canonical export source probe metadata is unavailable.")
    if binding["media_kind"] == "video" and type(codec.get("video_stream_index")) is not int:
        _fail("source_probe_missing", "Canonical video source has no verified stream selection.")
    return dict(record)


def _source_label(record: Mapping[str, object], binding: Mapping[str, object]) -> str:
    candidate = record.get("display_filename")
    try:
        encoded_candidate = candidate.encode("utf-8", errors="strict") if type(candidate) is str else b""
    except UnicodeEncodeError:
        encoded_candidate = b""
    if (
        type(candidate) is str
        and candidate not in {"", ".", ".."}
        and 0 < len(encoded_candidate) <= 255
        and "\x00" not in candidate
        and "/" not in candidate
        and "\\" not in candidate
        and not _looks_absolute(candidate)
    ):
        return candidate
    return str(binding["asset_id"])


def _copy_source_to_snapshot(
    source_descriptor: int,
    *,
    snapshot_directory: Path,
    ordinal: int,
    expected_sha256: str,
    cancelled: Cancelled,
) -> tuple[Path, str, int]:
    destination = snapshot_directory / f"source-{ordinal:04d}.bin"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    output = os.open(destination, flags, 0o600)
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(source_descriptor, 0, os.SEEK_SET)
        while True:
            _cancel_if_requested(cancelled)
            chunk = os.read(source_descriptor, _SOURCE_COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(output, view)
                view = view[written:]
        os.fsync(output)
    finally:
        os.close(output)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != expected_sha256:
        _fail("source_hash_mismatch", "Canonical export source bytes changed after import.")
    return destination, actual_sha256, total


def _freeze_sources(
    bundle: CanonicalRenderPlan,
    *,
    source_resolver: SourceResolver,
    workspace: Path,
    cancelled: Cancelled,
    fault_inject: FaultInjector | None,
) -> tuple[list[_FrozenSource], dict[str, object]]:
    snapshot_directory = workspace / "frozen-sources"
    try:
        snapshot_directory.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise CanonicalExportArtifactError(
            "workspace_not_empty",
            "Canonical export workspace already contains source snapshots.",
        ) from exc
    frozen: list[_FrozenSource] = []
    snapshots: list[dict[str, object]] = []
    for ordinal, binding in enumerate(bundle.source_bindings):
        _cancel_if_requested(cancelled)
        _inject(fault_inject, f"before_source_open:{ordinal}")
        record = source_resolver(str(binding["asset_source_id"]))
        if record is None:
            _fail("source_unavailable", "Canonical export source is unavailable.")
        try:
            root_descriptor = _open_absolute_directory_no_follow(Path(str(record.get("root_path") or "")))
        except (FileNotFoundError, NotADirectoryError, OSError) as exc:
            raise CanonicalExportArtifactError(
                "source_unavailable",
                "Canonical export source root is unavailable.",
            ) from exc
        try:
            source_descriptor = _open_relative_file_no_follow(root_descriptor, record.get("relative_path"))
        finally:
            os.close(root_descriptor)
        try:
            metadata = os.fstat(source_descriptor)
            normalized_record = _validate_source_record(binding, record, metadata)
            snapshot, digest, size = _copy_source_to_snapshot(
                source_descriptor,
                snapshot_directory=snapshot_directory,
                ordinal=ordinal,
                expected_sha256=str(binding["asset_sha256"]),
                cancelled=cancelled,
            )
        finally:
            os.close(source_descriptor)
        _inject(fault_inject, f"source_frozen:{ordinal}")
        frozen.append(
            _FrozenSource(
                snapshot=snapshot,
                record=normalized_record,
                binding=deepcopy(binding),
                size_bytes=size,
                sha256=digest,
            )
        )
        snapshots.append(
            {
                "ordinal": ordinal,
                "clip_id": binding["clip_id"],
                "asset_source_id": binding["asset_source_id"],
                "asset_id": binding["asset_id"],
                "asset_sha256": digest,
                "size_bytes": size,
                "media_kind": binding["media_kind"],
                "source_label": _source_label(normalized_record, binding),
                "source_label_role": "locator_hint_only",
            }
        )
    actual_read_manifest: dict[str, object] = {
        "object": ACTUAL_READ_MANIFEST_OBJECT,
        "schema_version": "1",
        "timeline_content_sha256": bundle.timeline_content_sha256,
        "source_bindings": [deepcopy(row) for row in bundle.source_bindings],
        "source_bindings_sha256": bundle.source_bindings_sha256,
        "snapshots": snapshots,
    }
    _require_path_free_json(actual_read_manifest)
    return frozen, actual_read_manifest


def _terminate_and_reap_renderer(process: subprocess.Popen[bytes]) -> None:
    """Best-effort termination followed by a mandatory child reap."""

    try:
        terminate_process(process)
    except BaseException:
        pass
    try:
        running = process.poll() is None
    except BaseException:
        running = True
    if running:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait()
    except (ChildProcessError, OSError):
        pass


def _default_command_runner(argv: Sequence[str], timeout_seconds: float, cancelled: Cancelled) -> None:
    _cancel_if_requested(cancelled)
    try:
        process = subprocess.Popen(
            list(argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "ffmpeg_start_failed",
            "The canonical export renderer could not be started.",
        ) from exc
    try:
        deadline = time.monotonic() + max(20.0, timeout_seconds)
        while process.poll() is None:
            if cancelled():
                raise MediaCancelled("Canonical export was cancelled.")
            if time.monotonic() > deadline:
                _fail("ffmpeg_timeout", "Canonical export rendering exceeded its time limit.")
            time.sleep(0.05)
        returncode = process.wait()
        if returncode != 0:
            _fail("ffmpeg_failed", "Canonical export rendering failed.")
    except BaseException:
        _terminate_and_reap_renderer(process)
        raise


def _default_master_probe(path: Path) -> Mapping[str, object]:
    try:
        projected = ffprobe(path)
    except MediaCapabilityError as exc:
        raise CanonicalExportArtifactError(exc.code, "Canonical export master verification failed.") from exc
    executable = resolve_binary("ffprobe")
    if executable is None:
        _fail("ffprobe_missing", "Canonical export master verification is unavailable.")
    try:
        completed = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe,fd",
                "-print_format",
                "json",
                "-show_entries",
                "stream=index,codec_type",
                "-f",
                "mov",
                "-enable_drefs",
                "0",
                "-use_absolute_path",
                "0",
                str(path),
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CanonicalExportArtifactError(
            "ffprobe_timeout",
            "Canonical export master stream inventory timed out.",
        ) from exc
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "ffprobe_failed",
            "Canonical export master stream inventory could not be read.",
        ) from exc
    if completed.returncode != 0:
        _fail("invalid_media", "Canonical export master stream inventory was rejected.")
    if len(completed.stdout) > _MAX_PROBE_BYTES:
        _fail("probe_output_too_large", "Canonical export master stream inventory is too large.")
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalExportArtifactError(
            "invalid_probe_json",
            "Canonical export master stream inventory is invalid.",
        ) from exc
    streams = payload.get("streams") if type(payload) is dict else None
    if type(streams) is not list:
        _fail("master_probe_invalid", "Canonical export master stream inventory is invalid.")
    video_indices: list[int] = []
    audio_indices: list[int] = []
    for stream in streams:
        if type(stream) is not dict or set(stream) != {"index", "codec_type"}:
            _fail("master_probe_invalid", "Canonical export master stream inventory is invalid.")
        index = stream.get("index")
        codec_type = stream.get("codec_type")
        if type(index) is not int or index < 0 or type(codec_type) is not str:
            _fail("master_probe_invalid", "Canonical export master stream inventory is invalid.")
        if codec_type == "video":
            video_indices.append(index)
        elif codec_type == "audio":
            audio_indices.append(index)
    if len(set(video_indices + audio_indices)) != len(video_indices) + len(audio_indices):
        _fail("master_probe_invalid", "Canonical export master stream identities are invalid.")
    codec = projected.get("codec")
    if type(projected) is not dict or type(codec) is not dict:
        _fail("master_probe_invalid", "Canonical export master probe is invalid.")
    result = deepcopy(projected)
    result["codec"]["video_stream_indices"] = video_indices
    result["codec"]["audio_stream_indices"] = audio_indices
    return result


def _canonical_video_only_clip_command(argv: Sequence[str]) -> list[str]:
    """Close the shared render command to one mapped video stream and no audio."""

    command = list(argv)
    if len(command) < 2:
        _fail("canonical_video_command_invalid", "Canonical clip command is invalid.")
    output = command.pop()
    synthetic_audio = "anullsrc=r=48000:cl=stereo"
    try:
        audio_source = command.index(synthetic_audio)
    except ValueError:
        _fail("canonical_video_command_invalid", "Canonical clip command has no bounded silent input.")
    if (
        audio_source < 5
        or command[audio_source - 5] != "-f"
        or command[audio_source - 4] != "lavfi"
        or command[audio_source - 3] != "-t"
        or command[audio_source - 1] != "-i"
    ):
        _fail("canonical_video_command_invalid", "Canonical clip audio input is invalid.")
    del command[audio_source - 5 : audio_source + 1]

    video_only: list[str] = []
    map_count = 0
    index = 0
    audio_options = {"-af", "-c:a", "-b:a", "-ar", "-ac"}
    while index < len(command):
        option = command[index]
        if option == "-map":
            if index + 1 >= len(command):
                _fail("canonical_video_command_invalid", "Canonical clip stream mapping is invalid.")
            if map_count == 0:
                video_only.extend((option, command[index + 1]))
            map_count += 1
            index += 2
            continue
        if option in audio_options:
            if index + 1 >= len(command):
                _fail("canonical_video_command_invalid", "Canonical clip audio option is invalid.")
            index += 2
            continue
        video_only.append(option)
        index += 1
    if map_count != 2:
        _fail("canonical_video_command_invalid", "Canonical clip stream mapping is not closed.")
    video_only.extend(("-an", output))
    return video_only


def _canonical_video_only_assemble_command(argv: Sequence[str]) -> list[str]:
    """Map exactly the concat video stream and explicitly disable audio."""

    command = list(argv)
    if len(command) < 2:
        _fail("canonical_video_command_invalid", "Canonical assembly command is invalid.")
    output = command.pop()
    command.extend(("-map", "0:v:0", "-an", output))
    return command


def _validate_master_probe(plan: RenderPlan, probe: Mapping[str, object]) -> tuple[int, int]:
    if type(probe) is not dict or set(probe) != _MASTER_PROBE_FIELDS:
        _fail("master_probe_invalid", "Canonical export master probe has an invalid closed shape.")
    codec = probe.get("codec")
    if type(codec) is not dict or set(codec) != _MASTER_CODEC_FIELDS:
        _fail("master_probe_invalid", "Canonical export master codec probe has an invalid closed shape.")
    video_indices = codec.get("video_stream_indices")
    audio_indices = codec.get("audio_stream_indices")
    if (
        type(video_indices) is not list
        or type(audio_indices) is not list
        or any(type(index) is not int or index < 0 for index in video_indices + audio_indices)
        or len(set(video_indices + audio_indices)) != len(video_indices) + len(audio_indices)
    ):
        _fail("master_probe_invalid", "Canonical export master stream inventory is invalid.")
    if len(video_indices) != 1:
        _fail(
            "master_video_stream_count_invalid",
            "Canonical export master must contain exactly one video stream.",
        )
    if (
        codec.get("audio_stream_index") is not None
        or codec.get("audio_streams") != []
        or audio_indices != []
    ):
        _fail("master_audio_stream_forbidden", "Canonical export master must contain no audio stream.")
    if (
        type(codec.get("video_stream_index")) is not int
        or codec["video_stream_index"] < 0
        or codec["video_stream_index"] != video_indices[0]
        or codec.get("stream_selection") != "default_disposition_then_lowest_index"
        or any(
            type(codec.get(field)) is not str or not codec[field]
            for field in (
                "format_name",
                "video_codec",
                "pixel_format",
                "avg_frame_rate",
                "time_base",
            )
        )
        or type(probe.get("rotation_degrees")) is not int
        or (probe.get("captured_at") is not None and type(probe.get("captured_at")) is not str)
    ):
        _fail("master_probe_invalid", "Canonical export master probe metadata is invalid.")
    try:
        validate_render_duration(plan, dict(probe))
    except (MediaCapabilityError, KeyError, TypeError, ValueError) as exc:
        raise CanonicalExportArtifactError(
            "master_duration_mismatch",
            "Canonical export master duration does not match the Timeline.",
        ) from exc
    width = probe.get("width")
    height = probe.get("height")
    if width != plan.width or height != plan.height:
        _fail("master_dimensions_mismatch", "Canonical export master dimensions are invalid.")
    frame_rate = codec["avg_frame_rate"]
    if str(frame_rate) not in {"30", "30.0", "30/1"}:
        _fail("master_frame_rate_mismatch", "Canonical export master frame rate is invalid.")
    return int(probe["duration_ms"]), int(width) * int(height)


def render_canonical_master(
    timeline: Mapping[str, object],
    source_bindings: Sequence[Mapping[str, object]],
    *,
    source_resolver: SourceResolver,
    job_workspace: Path,
    master_output: Path,
    ffmpeg_binary: str,
    cancelled: Cancelled = lambda: False,
    command_runner: CommandRunner = _default_command_runner,
    probe_master: MasterProbe = _default_master_probe,
    image_freezer: ImageFreezer = freeze_still_image,
    runtime_identity: Mapping[str, object] | None = None,
    fault_inject: FaultInjector | None = None,
) -> dict[str, object]:
    """Freeze exact source bytes, render one master, and return only path-free proof."""

    bundle = build_canonical_render_plan(timeline, source_bindings)
    workspace = Path(job_workspace)
    output = Path(master_output)
    try:
        workspace_descriptor = _open_absolute_directory_no_follow(workspace)
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:
        raise CanonicalExportArtifactError(
            "workspace_unavailable",
            "Canonical export workspace is unavailable.",
        ) from exc
    else:
        os.close(workspace_descriptor)
    if output.parent != workspace or output.name in {"", ".", ".."} or output.exists() or output.is_symlink():
        _fail("invalid_master_output", "Canonical export master must be one new job-workspace file.")
    if not ffmpeg_binary or "\x00" in ffmpeg_binary:
        _fail("ffmpeg_unavailable", "Canonical export renderer is unavailable.")
    runtime = deepcopy(dict(runtime_identity or {}))
    _require_path_free_json(runtime)
    if shutil.disk_usage(workspace).free < bundle.render_plan.required_free_bytes:
        _fail("insufficient_storage", "Canonical export workspace has insufficient free space.")

    _inject(fault_inject, "before_source_freeze")
    frozen, actual_read_manifest = _freeze_sources(
        bundle,
        source_resolver=source_resolver,
        workspace=workspace,
        cancelled=cancelled,
        fault_inject=fault_inject,
    )
    _inject(fault_inject, "after_source_freeze")
    normalized: list[Path] = []
    for ordinal, (clip, frozen_source) in enumerate(
        zip(bundle.render_plan.clips, frozen, strict=True)
    ):
        _cancel_if_requested(cancelled)
        input_path = frozen_source.snapshot
        if clip["kind"] == "image":
            input_path = workspace / f"still-{ordinal:04d}.png"
            try:
                image_freezer(frozen_source.snapshot, input_path)
            except CanonicalExportArtifactError:
                raise
            except Exception as exc:
                raise CanonicalExportArtifactError(
                    "image_normalization_failed",
                    "Canonical image source could not be normalized.",
                ) from exc
        clip_output = workspace / f"clip-{ordinal:04d}.mp4"
        argv = _canonical_video_only_clip_command(
            build_clip_command(
                bundle.render_plan,
                clip,
                source=frozen_source.record,
                source_path=frozen_source.snapshot,
                input_path=input_path,
                output_path=clip_output,
                ffmpeg_binary=ffmpeg_binary,
            )
        )
        _inject(fault_inject, f"before_clip_render:{ordinal}")
        command_runner(
            argv,
            max(60.0, int(clip["timeline_duration_ms"]) / 1000 * 8),
            cancelled,
        )
        _cancel_if_requested(cancelled)
        if not clip_output.is_file() or clip_output.is_symlink():
            _fail("clip_artifact_missing", "Canonical clip renderer did not produce an artifact.")
        normalized.append(clip_output)
        _inject(fault_inject, f"clip_rendered:{ordinal}")

    concat_file = workspace / "concat.txt"
    concat_file.write_text(concat_manifest(normalized), encoding="utf-8")
    with concat_file.open("rb") as handle:
        os.fsync(handle.fileno())
    argv = _canonical_video_only_assemble_command(
        build_assemble_command(
            concat_file=concat_file,
            output_path=output,
            ffmpeg_binary=ffmpeg_binary,
        )
    )
    _inject(fault_inject, "before_master_assemble")
    command_runner(argv, max(60.0, bundle.render_plan.duration_ms / 1000 * 4), cancelled)
    _cancel_if_requested(cancelled)
    master_fd = _open_master_artifact_no_follow(output)
    try:
        expected_identity = _master_file_identity(os.fstat(master_fd))
        _inject(fault_inject, "master_assembled")
        _require_held_master_unchanged(
            output,
            master_fd,
            expected_identity=expected_identity,
        )
        before_probe_sha256, before_probe_size = _sha256_fd(master_fd)
        _require_held_master_unchanged(
            output,
            master_fd,
            expected_identity=expected_identity,
        )
        probe = probe_master(output)
        _require_held_master_unchanged(
            output,
            master_fd,
            expected_identity=expected_identity,
        )
        video_sha256, video_size = _sha256_fd(master_fd)
        _require_held_master_unchanged(
            output,
            master_fd,
            expected_identity=expected_identity,
        )
        if (video_sha256, video_size) != (before_probe_sha256, before_probe_size):
            _fail("master_artifact_changed", "Canonical export master changed during verification.")
        duration_ms, _pixels = _validate_master_probe(bundle.render_plan, probe)
    finally:
        os.close(master_fd)
    _inject(fault_inject, "master_verified")

    actual_read_sha256 = canonical_sha256(actual_read_manifest)
    runtime_manifest: dict[str, object] = {
        "object": RUNTIME_MANIFEST_OBJECT,
        "schema_version": "1",
        "renderer": CANONICAL_EXPORT_ARTIFACTS_VERSION,
        "profile": CANONICAL_RENDER_PROFILE,
        "timeline_content_sha256": bundle.timeline_content_sha256,
        "timeline_source_bindings_sha256": bundle.source_bindings_sha256,
        "width": bundle.render_plan.width,
        "height": bundle.render_plan.height,
        "fps": bundle.render_plan.fps,
        "audio": "silent",
        "fit": "cover",
        "transitions": "hard_cut",
        "actual_read_manifest": actual_read_manifest,
        "actual_read_manifest_sha256": actual_read_sha256,
        "runtime_identity": runtime,
    }
    proof: dict[str, object] = {
        "object": "memolens.canonical_render_artifact",
        "schema_version": "1",
        "profile": CANONICAL_RENDER_PROFILE,
        "timeline_content_sha256": bundle.timeline_content_sha256,
        "timeline_source_bindings_sha256": bundle.source_bindings_sha256,
        "video_sha256": video_sha256,
        "video_size_bytes": video_size,
        "video_duration_ms": duration_ms,
        "width": bundle.render_plan.width,
        "height": bundle.render_plan.height,
        "fps": bundle.render_plan.fps,
        "usage_occurrences": [deepcopy(row) for row in bundle.usage_occurrences],
        "usage_sha256": canonical_sha256(list(bundle.usage_occurrences)),
        "runtime_manifest": runtime_manifest,
        "runtime_manifest_sha256": canonical_sha256(runtime_manifest),
    }
    _require_path_free_json(proof)
    return proof


def _write_bytes_at(directory_fd: int, name: str, data: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, mode, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy_verified_master_at(
    master_path: Path,
    directory_fd: int,
    *,
    expected_sha256: str,
    expected_size: int,
    cancelled: Cancelled,
) -> None:
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    source = os.open(master_path, source_flags)
    destination = os.open(
        VIDEO_FILENAME,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=directory_fd,
    )
    digest = hashlib.sha256()
    total = 0
    try:
        metadata = os.fstat(source)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("master_artifact_invalid", "Verified master is not a regular file.")
        while True:
            _cancel_if_requested(cancelled)
            chunk = os.read(source, _SOURCE_COPY_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                view = view[written:]
        os.fsync(destination)
    finally:
        os.close(destination)
        os.close(source)
    if total != expected_size or digest.hexdigest() != expected_sha256:
        _fail("master_artifact_changed", "Verified master changed before packaging.")


def usage_text_from_manifest(manifest: Mapping[str, object]) -> str:
    """Pure human projection. Machine truth remains the canonical manifest."""

    if manifest.get("object") != PACKAGE_MANIFEST_OBJECT:
        _fail("invalid_package_manifest", "Package manifest object is invalid.")
    occurrences_value = manifest.get("usage_occurrences")
    if occurrences_value is None:
        _fail("invalid_package_manifest", "Package manifest is incomplete.")
    occurrences = _validate_occurrences(occurrences_value)
    project = manifest.get("project_id", "unknown")
    export_id = manifest.get("export_id", "unknown")
    source_labels: dict[str, str] = {}
    runtime = manifest.get("runtime_manifest")
    actual_read = runtime.get("actual_read_manifest") if isinstance(runtime, Mapping) else None
    snapshots = actual_read.get("snapshots") if isinstance(actual_read, Mapping) else None
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            if isinstance(snapshot, Mapping) and isinstance(snapshot.get("source_label"), str):
                source_labels[str(snapshot.get("asset_source_id"))] = str(snapshot["source_label"])
    final_video = manifest.get("video")
    if not isinstance(final_video, Mapping) or not isinstance(final_video.get("sha256"), str):
        _fail("invalid_package_manifest", "Package final-video binding is invalid.")
    lines = [
        "MemoLens 使用清单",
        f"项目：{project}",
        f"导出：{export_id}",
        f"成片 SHA-256：{final_video['sha256']}",
        "",
        "素材出现（按 Timeline 原始顺序，重复出现保留）：",
    ]
    for item in occurrences:
        timeline_interval = f"[{item['timeline_start_ms']},{item['timeline_end_ms']}) ms"
        label = source_labels.get(str(item["asset_source_id"]), str(item["asset_id"]))
        if item["media_kind"] == "image":
            source_text = "图片（无 source interval）"
        else:
            source_text = f"视频 [{item['source_start_ms']},{item['source_end_ms']}) ms"
        lines.append(
            f"{int(item['ordinal']) + 1}. {label}；{source_text}；Timeline {timeline_interval}；"
            f"asset={item['asset_id']}；clip={item['clip_id']}"
        )
    lines += [
        "",
        "原件声明：本轻量包未复制、移动、删除或修改 Library 原素材。",
        "区间语义：视频 source interval 严格采用左闭右开 [start,end) 毫秒。",
        "",
    ]
    result = "\n".join(lines)
    if len(result.encode("utf-8")) > _MAX_USAGE_BYTES:
        _fail("usage_projection_too_large", "Human usage projection exceeds the size limit.")
    return result


def _validate_runtime_manifest(
    value: object,
    *,
    occurrences: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if type(value) is not dict or set(value) != _RUNTIME_MANIFEST_FIELDS:
        _fail("invalid_runtime_manifest", "Runtime manifest has an invalid closed shape.")
    runtime = deepcopy(value)
    if (
        runtime.get("object") != RUNTIME_MANIFEST_OBJECT
        or runtime.get("schema_version") != "1"
        or runtime.get("renderer") != CANONICAL_EXPORT_ARTIFACTS_VERSION
        or runtime.get("profile") != CANONICAL_RENDER_PROFILE
        or runtime.get("fps") != 30
        or runtime.get("audio") != "silent"
        or runtime.get("fit") != "cover"
        or runtime.get("transitions") != "hard_cut"
        or (runtime.get("width"), runtime.get("height"))
        not in {(1920, 1080), (1080, 1920), (1080, 1080), (1080, 1350)}
    ):
        _fail("invalid_runtime_manifest", "Runtime manifest execution contract is invalid.")
    timeline_sha256 = _require_sha256(
        runtime.get("timeline_content_sha256"),
        code="invalid_runtime_manifest",
    )
    bindings_sha256 = _require_sha256(
        runtime.get("timeline_source_bindings_sha256"),
        code="invalid_runtime_manifest",
    )
    actual = runtime.get("actual_read_manifest")
    if type(actual) is not dict or set(actual) != _ACTUAL_READ_MANIFEST_FIELDS:
        _fail("invalid_actual_read_manifest", "Actual-read manifest has an invalid closed shape.")
    if (
        actual.get("object") != ACTUAL_READ_MANIFEST_OBJECT
        or actual.get("schema_version") != "1"
        or actual.get("timeline_content_sha256") != timeline_sha256
        or actual.get("source_bindings_sha256") != bindings_sha256
    ):
        _fail("invalid_actual_read_manifest", "Actual-read Timeline binding is invalid.")
    bindings = actual.get("source_bindings")
    snapshots = actual.get("snapshots")
    if (
        type(bindings) is not list
        or type(snapshots) is not list
        or len(bindings) != len(occurrences)
        or len(snapshots) != len(occurrences)
    ):
        _fail("invalid_actual_read_manifest", "Actual-read source set is invalid.")
    if canonical_sha256(bindings) != bindings_sha256:
        _fail("invalid_actual_read_manifest", "Actual-read source binding digest does not match.")
    seen_clip_ids: set[str] = set()
    evidence_identities: dict[str, tuple[tuple[tuple[str, object], ...], tuple[tuple[str, object], ...]]] = {}
    source_identities: dict[str, tuple[object, object]] = {}
    for index, (binding, snapshot, occurrence) in enumerate(
        zip(bindings, snapshots, occurrences, strict=True)
    ):
        media_kind = occurrence["media_kind"]
        evidence_ref = binding.get("evidence_ref") if isinstance(binding, Mapping) else None
        residual_reference_match = (
            _RESIDUAL_EVIDENCE_REF.fullmatch(evidence_ref)
            if type(evidence_ref) is str
            else None
        )
        if media_kind == "video" and residual_reference_match is not None:
            expected_binding_fields = _SOURCE_BINDING_RESIDUAL_VIDEO_FIELDS
        elif media_kind == "video":
            expected_binding_fields = _SOURCE_BINDING_VIDEO_FIELDS
        else:
            expected_binding_fields = _SOURCE_BINDING_IMAGE_FIELDS
        if type(binding) is not dict or set(binding) != expected_binding_fields:
            _fail("invalid_actual_read_binding", "Actual-read source binding is invalid.")
        if type(snapshot) is not dict or set(snapshot) != _SNAPSHOT_PROOF_FIELDS:
            _fail("invalid_snapshot_proof", "Frozen snapshot proof is invalid.")
        for field in ("clip_id", "asset_id", "asset_sha256", "asset_source_id", "media_kind"):
            if binding.get(field) != occurrence.get(field) or snapshot.get(field) != occurrence.get(field):
                _fail("actual_read_occurrence_mismatch", "Actual-read proof does not match usage occurrence.")
        if canonical_sha256(binding) != occurrence["source_binding_sha256"]:
            _fail("actual_read_occurrence_mismatch", "Actual-read binding digest does not match usage.")
        _require_sha256(binding.get("coverage_proof_sha256"), code="invalid_actual_read_binding")
        _require_sha256(snapshot.get("asset_sha256"), code="invalid_snapshot_proof")
        clip_id = str(binding["clip_id"])
        source_id = str(binding["asset_source_id"])
        if clip_id in seen_clip_ids or type(evidence_ref) is not str:
            _fail(
                "invalid_actual_read_binding",
                "Actual-read clip identity must be unique and evidence identity must be explicit.",
            )
        binding_lineage = tuple(
            (field, binding[field])
            for field in sorted(
                expected_binding_fields - {"clip_id", "source_in_ms", "source_out_ms"}
            )
        )
        snapshot_lineage = tuple(
            (field, snapshot[field])
            for field in sorted(_SNAPSHOT_PROOF_FIELDS - {"ordinal", "clip_id"})
        )
        evidence_identity = (binding_lineage, snapshot_lineage)
        existing_evidence_identity = evidence_identities.get(evidence_ref)
        if (
            existing_evidence_identity is not None
            and existing_evidence_identity != evidence_identity
        ):
            _fail(
                "invalid_actual_read_binding",
                "Repeated evidence must preserve one exact source and proof identity.",
            )
        source_identity = (binding.get("asset_id"), binding.get("asset_sha256"))
        existing_identity = source_identities.get(source_id)
        if existing_identity is not None and existing_identity != source_identity:
            _fail(
                "invalid_actual_read_binding",
                "One actual-read source identity cannot identify different asset content.",
            )
        seen_clip_ids.add(clip_id)
        evidence_identities.setdefault(evidence_ref, evidence_identity)
        source_identities.setdefault(source_id, source_identity)
        if snapshot.get("ordinal") != index or type(snapshot.get("size_bytes")) is not int or snapshot["size_bytes"] < 0:
            _fail("invalid_snapshot_proof", "Frozen snapshot proof metadata is invalid.")
        label = snapshot.get("source_label")
        if (
            type(label) is not str
            or not label
            or label in {".", ".."}
            or "/" in label
            or "\\" in label
            or "\x00" in label
            or snapshot.get("source_label_role") != "locator_hint_only"
        ):
            _fail("invalid_snapshot_proof", "Frozen snapshot source label is invalid.")
        if media_kind == "image":
            analysis_revision = binding.get("analysis_revision")
            if (
                evidence_ref
                != f"memolens://evidence/asset/{occurrence['asset_id']}"
                or type(binding.get("analysis_run_id")) is not str
                or _ANALYSIS_RUN_ID.fullmatch(binding["analysis_run_id"])
                is None
                or type(analysis_revision) is not int
                or not 1 <= analysis_revision <= 1_800_000
            ):
                _fail("invalid_actual_read_binding", "Actual-read image evidence identity is invalid.")
            _require_sha256(
                binding.get("analysis_content_sha256"),
                code="invalid_actual_read_binding",
            )
            _require_sha256(
                binding.get("source_binding_sha256"),
                code="invalid_actual_read_binding",
            )
        elif residual_reference_match is None:
            span_id = binding.get("span_id")
            span_match = _SPAN_ID.fullmatch(span_id) if type(span_id) is str else None
            analysis_revision = binding.get("analysis_revision")
            if (
                span_match is None
                or evidence_ref != f"memolens://evidence/span/{span_id}"
                or span_match.group("asset_digest")
                != str(occurrence["asset_id"]).split("_", 1)[1]
                or type(binding.get("analysis_run_id")) is not str
                or _ANALYSIS_RUN_ID.fullmatch(binding["analysis_run_id"]) is None
                or type(analysis_revision) is not int
                or not 1 <= analysis_revision <= 1_800_000
                or int(span_match.group("analysis_revision")) != analysis_revision
                or binding.get("source_in_ms") != occurrence["source_start_ms"]
                or binding.get("source_out_ms") != occurrence["source_end_ms"]
                or binding.get("input_asset_sha256") != occurrence["asset_sha256"]
            ):
                _fail("actual_read_occurrence_mismatch", "Actual-read video identity or interval is invalid.")
        else:
            try:
                residual_binding = require_valid_residual_binding(
                    binding.get("residual_binding")  # type: ignore[arg-type]
                )
            except (ResidualIdentityContractError, TypeError) as exc:
                raise CanonicalExportArtifactError(
                    "invalid_actual_read_binding",
                    "Actual-read residual proof is invalid.",
                ) from exc
            residual_id = residual_reference_match.group("residual_id")
            expected_lineage = {
                "residual_id": residual_binding["residual_id"],
                "parent_segment_id": residual_binding["parent_segment_id"],
                "span_id": residual_binding["parent_segment_id"],
                "asset_id": residual_binding["asset_id"],
                "asset_sha256": residual_binding["asset_sha256"],
                "asset_source_id": residual_binding["asset_source_id"],
                "analysis_run_id": residual_binding["analysis_run_id"],
                "analysis_revision": residual_binding["analysis_revision"],
                "input_asset_sha256": residual_binding["input_asset_sha256"],
            }
            source_in_ms = binding.get("source_in_ms")
            source_out_ms = binding.get("source_out_ms")
            if (
                residual_binding["residual_id"] != residual_id
                or any(
                    binding.get(field) != expected
                    for field, expected in expected_lineage.items()
                )
                or type(source_in_ms) is not int
                or type(source_out_ms) is not int
                or source_in_ms < int(residual_binding["source_in_ms"])
                or source_out_ms > int(residual_binding["source_out_ms"])
                or source_out_ms <= source_in_ms
                or source_in_ms != occurrence["source_start_ms"]
                or source_out_ms != occurrence["source_end_ms"]
            ):
                _fail(
                    "actual_read_occurrence_mismatch",
                    "Actual-read residual identity, parent, or interval is invalid.",
                )
    if canonical_sha256(actual) != runtime.get("actual_read_manifest_sha256"):
        _fail("invalid_actual_read_manifest", "Actual-read manifest digest does not match.")
    if type(runtime.get("runtime_identity")) is not dict:
        _fail("invalid_runtime_manifest", "Runtime identity must be a path-free object.")
    _require_path_free_json(runtime)
    return runtime


def _validate_expected_package_manifest(
    value: object,
    *,
    package_basename: str,
    render_proof: Mapping[str, object],
    script_sha256: str,
    script_size_bytes: int,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != _PACKAGE_MANIFEST_FIELDS:
        _fail("invalid_package_manifest", "Expected package manifest has an invalid closed shape.")
    manifest = deepcopy(value)
    if (
        manifest.get("object") != PACKAGE_MANIFEST_OBJECT
        or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or manifest.get("package_basename") != package_basename
        or manifest.get("profile") != CANONICAL_RENDER_PROFILE
        or manifest.get("package_roles") != list(PACKAGE_ROLES)
        or manifest.get("required_files") != list(REQUIRED_PACKAGE_FILES)
        or manifest.get("source_policy")
        != {
            "library_originals_copied": False,
            "library_originals_moved": False,
            "library_originals_modified": False,
            "automatic_upload": False,
        }
        or manifest.get("optional_roles") != {"subtitle": "absent", "cover": "absent"}
    ):
        _fail("invalid_package_manifest", "Expected package manifest contract is invalid.")
    _require_identifier(manifest.get("export_id"), code="invalid_package_manifest")
    _require_identifier(manifest.get("job_id"), code="invalid_package_manifest")
    _require_identifier(manifest.get("project_id"), code="invalid_package_manifest")
    timeline_binding = _validate_timeline_binding(manifest.get("timeline_binding"))
    manifest["timeline_binding"] = timeline_binding
    occurrences = _validate_occurrences(manifest.get("usage_occurrences"))
    manifest["usage_occurrences"] = occurrences
    usage_sha256 = canonical_sha256(occurrences)
    if manifest.get("usage_sha256") != usage_sha256:
        _fail("package_usage_digest_mismatch", "Expected package usage digest does not match.")
    runtime_manifest = render_proof.get("runtime_manifest")
    if not isinstance(runtime_manifest, Mapping):
        _fail("invalid_render_proof", "Canonical render proof has no runtime manifest.")
    validated_runtime = _validate_runtime_manifest(
        dict(runtime_manifest),
        occurrences=occurrences,
    )
    runtime_digest = _require_sha256(
        render_proof.get("runtime_manifest_sha256"),
        code="invalid_runtime_manifest_sha256",
    )
    if canonical_sha256(validated_runtime) != runtime_digest:
        _fail("runtime_manifest_mismatch", "Canonical runtime manifest digest does not match.")
    if manifest.get("runtime_manifest") != validated_runtime or manifest.get(
        "runtime_manifest_sha256"
    ) != runtime_digest:
        _fail("runtime_manifest_mismatch", "Expected package runtime manifest does not match render proof.")
    video_sha256 = _require_sha256(render_proof.get("video_sha256"), code="invalid_video_sha256")
    video_size = render_proof.get("video_size_bytes")
    video_duration = render_proof.get("video_duration_ms")
    if type(video_size) is not int or video_size < 0 or type(video_duration) is not int or video_duration <= 0:
        _fail("invalid_render_proof", "Canonical render proof has invalid video metadata.")
    if manifest.get("video") != {
        "sha256": video_sha256,
        "size_bytes": video_size,
        "duration_ms": video_duration,
    }:
        _fail("render_video_binding_mismatch", "Expected package video does not match render proof.")
    if manifest.get("script") != {"sha256": script_sha256, "size_bytes": script_size_bytes}:
        _fail("script_binding_mismatch", "Expected package script does not match script bytes.")
    if (
        timeline_binding["timeline_content_sha256"] != render_proof.get("timeline_content_sha256")
        or timeline_binding["source_bindings_sha256"]
        != render_proof.get("timeline_source_bindings_sha256")
    ):
        _fail("render_export_binding_mismatch", "Render proof does not match the exact export Timeline.")
    if render_proof.get("usage_occurrences") != occurrences or render_proof.get(
        "usage_sha256"
    ) != usage_sha256:
        _fail("render_usage_binding_mismatch", "Render proof usage does not match package occurrences.")
    _require_path_free_json(manifest)
    return manifest


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise CanonicalExportArtifactError(
            "package_missing",
            "Lightweight package is unavailable.",
        ) from exc
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "package_invalid",
            "Lightweight package directory is invalid.",
        ) from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        _fail("package_invalid", "Lightweight package is not a directory.")
    return descriptor


def _read_file_at(directory_fd: int, name: str, *, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise CanonicalExportArtifactError(
            "package_incomplete",
            "Lightweight package is missing a required file.",
        ) from exc
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "package_file_invalid",
            "A lightweight package file could not be opened safely.",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            _fail("package_file_invalid", "A lightweight package file is invalid.")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_SOURCE_COPY_CHUNK, limit - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                _fail("package_file_too_large", "A lightweight package file exceeds its size limit.")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _hash_regular_file_at(directory_fd: int, name: str) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError as exc:
        raise CanonicalExportArtifactError(
            "package_incomplete",
            "Lightweight package is missing a required file.",
        ) from exc
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "package_file_invalid",
            "A lightweight package file could not be opened safely.",
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("package_file_invalid", "A lightweight package file is invalid.")
        return _sha256_fd(descriptor)
    finally:
        os.close(descriptor)


def _read_canonical_json_at(directory_fd: int, name: str, *, limit: int) -> dict[str, object]:
    raw = _read_file_at(directory_fd, name, limit=limit)
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalExportArtifactError(
            "package_json_invalid",
            "Lightweight package JSON is invalid.",
        ) from exc
    if type(value) is not dict or canonical_json(value).encode("utf-8") != raw:
        _fail("package_json_not_canonical", "Lightweight package JSON is not canonical.")
    return value


def _verify_package_directory(
    directory_fd: int,
    *,
    package_basename: str,
    expected_package_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    try:
        entries = set(os.listdir(directory_fd))
    except OSError as exc:
        raise CanonicalExportArtifactError(
            "package_directory_unreadable",
            "Lightweight package directory could not be enumerated.",
        ) from exc
    if entries != set(REQUIRED_PACKAGE_FILES):
        _fail("package_file_set_mismatch", "Lightweight package file set is not exact.")
    marker = _read_canonical_json_at(
        directory_fd,
        COMPLETION_MARKER_FILENAME,
        limit=_MAX_MARKER_BYTES,
    )
    manifest_bytes = _read_file_at(directory_fd, MANIFEST_FILENAME, limit=_MAX_JSON_BYTES)
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalExportArtifactError(
            "package_manifest_invalid",
            "Lightweight package manifest is invalid.",
        ) from exc
    if type(manifest) is not dict or canonical_json(manifest).encode("utf-8") != manifest_bytes:
        _fail("package_manifest_not_canonical", "Lightweight package manifest is not canonical.")
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    if (
        set(marker)
        != {
            "object",
            "schema_version",
            "completion_state",
            "package_basename",
            "package_manifest_sha256",
            "required_file_sha256",
        }
        or not isinstance(marker.get("required_file_sha256"), dict)
        or set(marker["required_file_sha256"])
        != {VIDEO_FILENAME, SCRIPT_FILENAME, MANIFEST_FILENAME, USAGE_FILENAME}
        or marker.get("object") != PACKAGE_MARKER_OBJECT
        or marker.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or marker.get("completion_state") != "complete"
        or marker.get("package_basename") != package_basename
        or marker.get("package_manifest_sha256") != manifest_sha256
    ):
        _fail("package_completion_marker_invalid", "Lightweight package completion marker is invalid.")
    if (
        manifest.get("object") != PACKAGE_MANIFEST_OBJECT
        or manifest.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or manifest.get("package_basename") != package_basename
        or manifest.get("required_files") != list(REQUIRED_PACKAGE_FILES)
        or set(manifest) != _PACKAGE_MANIFEST_FIELDS
        or manifest.get("profile") != CANONICAL_RENDER_PROFILE
        or manifest.get("package_roles") != list(PACKAGE_ROLES)
        or manifest.get("source_policy")
        != {
            "library_originals_copied": False,
            "library_originals_moved": False,
            "library_originals_modified": False,
            "automatic_upload": False,
        }
        or manifest.get("optional_roles") != {"subtitle": "absent", "cover": "absent"}
    ):
        _fail("package_manifest_invalid", "Lightweight package manifest contract is invalid.")
    _require_path_free_json(manifest)
    _require_identifier(manifest.get("export_id"), code="package_manifest_invalid")
    _require_identifier(manifest.get("job_id"), code="package_manifest_invalid")
    _require_identifier(manifest.get("project_id"), code="package_manifest_invalid")
    timeline_binding = _validate_timeline_binding(manifest.get("timeline_binding"))
    if expected_package_manifest is not None and manifest != dict(expected_package_manifest):
        _fail("package_export_binding_mismatch", "Lightweight package manifest does not match expected bytes.")
    occurrences = _validate_occurrences(manifest.get("usage_occurrences"))
    usage_sha256 = canonical_sha256(occurrences)
    if manifest.get("usage_sha256") != usage_sha256:
        _fail("package_usage_digest_mismatch", "Lightweight package usage digest does not match.")
    runtime_manifest = manifest.get("runtime_manifest")
    if not isinstance(runtime_manifest, Mapping):
        _fail("package_runtime_manifest_mismatch", "Lightweight package runtime manifest does not match.")
    runtime_manifest = _validate_runtime_manifest(
        dict(runtime_manifest),
        occurrences=occurrences,
    )
    if canonical_sha256(runtime_manifest) != manifest.get("runtime_manifest_sha256"):
        _fail("package_runtime_manifest_mismatch", "Lightweight package runtime manifest does not match.")
    if (
        timeline_binding["timeline_content_sha256"]
        != runtime_manifest["timeline_content_sha256"]
        or timeline_binding["source_bindings_sha256"]
        != runtime_manifest["timeline_source_bindings_sha256"]
    ):
        _fail("package_runtime_manifest_mismatch", "Runtime manifest does not match the export Timeline.")
    video = manifest.get("video")
    script = manifest.get("script")
    if (
        not isinstance(video, Mapping)
        or set(video) != {"sha256", "size_bytes", "duration_ms"}
        or not isinstance(script, Mapping)
        or set(script) != {"sha256", "size_bytes"}
    ):
        _fail("package_file_binding_invalid", "Lightweight package file binding is invalid.")
    _require_sha256(video.get("sha256"), code="package_file_binding_invalid")
    _require_sha256(script.get("sha256"), code="package_file_binding_invalid")
    if (
        type(video.get("size_bytes")) is not int
        or video["size_bytes"] < 0
        or type(video.get("duration_ms")) is not int
        or video["duration_ms"] <= 0
        or abs(video["duration_ms"] - occurrences[-1]["timeline_end_ms"])
        > max(50, int(2000 / 30))
        or type(script.get("size_bytes")) is not int
        or script["size_bytes"] <= 0
    ):
        _fail("package_file_binding_invalid", "Lightweight package file metadata is invalid.")
    video_sha256, video_size = _hash_regular_file_at(directory_fd, VIDEO_FILENAME)
    if video.get("sha256") != video_sha256 or video.get("size_bytes") != video_size:
        _fail("package_video_digest_mismatch", "Lightweight package video digest does not match.")
    script_bytes = _read_file_at(directory_fd, SCRIPT_FILENAME, limit=_MAX_SCRIPT_BYTES)
    if script.get("sha256") != _sha256_bytes(script_bytes) or script.get("size_bytes") != len(script_bytes):
        _fail("package_script_digest_mismatch", "Lightweight package script digest does not match.")
    try:
        _validate_script_text(script_bytes.decode("utf-8", errors="strict"))
    except UnicodeDecodeError as exc:
        raise CanonicalExportArtifactError(
            "package_script_invalid",
            "Lightweight package script is not normalized UTF-8 text.",
        ) from exc
    usage_bytes = _read_file_at(directory_fd, USAGE_FILENAME, limit=_MAX_USAGE_BYTES)
    expected_usage = usage_text_from_manifest(manifest).encode("utf-8")
    if usage_bytes != expected_usage:
        _fail("package_usage_projection_mismatch", "Human usage list is not the canonical projection.")
    required_digests = {
        VIDEO_FILENAME: video_sha256,
        SCRIPT_FILENAME: _sha256_bytes(script_bytes),
        MANIFEST_FILENAME: manifest_sha256,
        USAGE_FILENAME: _sha256_bytes(usage_bytes),
    }
    if marker.get("required_file_sha256") != required_digests:
        _fail("package_completion_marker_invalid", "Lightweight package file bindings do not match.")
    completion_marker_sha256 = canonical_sha256(marker)
    human_usage_sha256 = _sha256_bytes(usage_bytes)
    core_artifact_proof = {
        "video_sha256": video_sha256,
        "video_size_bytes": video_size,
        "video_duration_ms": video.get("duration_ms"),
        "script_sha256": _sha256_bytes(script_bytes),
        "script_size_bytes": len(script_bytes),
        "package_manifest": manifest,
        "package_manifest_sha256": manifest_sha256,
        "human_usage_sha256": human_usage_sha256,
        "completion_marker_sha256": completion_marker_sha256,
    }
    proof: dict[str, object] = {
        "object": "memolens.lightweight_package_artifact",
        "schema_version": "1",
        "package_basename": package_basename,
        "completion_state": "complete",
        "video_sha256": video_sha256,
        "video_size_bytes": video_size,
        "video_duration_ms": video.get("duration_ms"),
        "script_sha256": _sha256_bytes(script_bytes),
        "script_size_bytes": len(script_bytes),
        "usage_sha256": usage_sha256,
        "usage_occurrences": occurrences,
        "package_manifest": manifest,
        "package_manifest_sha256": manifest_sha256,
        "runtime_manifest": deepcopy(dict(runtime_manifest)),
        "runtime_manifest_sha256": manifest["runtime_manifest_sha256"],
        "human_usage_sha256": human_usage_sha256,
        "completion_marker_sha256": completion_marker_sha256,
        "required_file_sha256": required_digests,
        "core_artifact_proof": core_artifact_proof,
    }
    _require_path_free_json(proof)
    return proof


def _rename_directory_no_replace(
    root_fd: int,
    staging_name: str,
    final_name: str,
) -> None:
    """Use the host's exclusive directory-rename primitive; never emulate overwrite."""

    libc = ctypes.CDLL(None, use_errno=True)
    encoded_staging = os.fsencode(staging_name)
    encoded_final = os.fsencode(final_name)
    if hasattr(libc, "renameatx_np"):
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(root_fd, encoded_staging, root_fd, encoded_final, 0x00000004)
    elif hasattr(libc, "renameat2"):
        renameat2 = libc.renameat2
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(root_fd, encoded_staging, root_fd, encoded_final, 0x00000001)
    else:
        _fail(
            "atomic_no_replace_unsupported",
            "This filesystem runtime has no safe no-replace directory publication primitive.",
        )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        _fail("package_exists", "A package with this name already exists.")
    if error in {errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL}:
        _fail(
            "atomic_no_replace_unsupported",
            "This filesystem does not support safe no-replace directory publication.",
        )
    raise CanonicalExportArtifactError(
        "package_publish_failed",
        "Lightweight package could not be published atomically.",
    )


def build_lightweight_package(
    *,
    verified_master: Path,
    render_proof: Mapping[str, object],
    expected_package_manifest: Mapping[str, object],
    script_text: str,
    output_root: Path | None = None,
    output_root_fd: int | None = None,
    package_basename: str,
    cancelled: Cancelled = lambda: False,
    fault_inject: FaultInjector | None = None,
) -> dict[str, object]:
    """Build and atomically publish a marker-last, no-overwrite lightweight package."""

    basename = _validate_safe_basename(package_basename)
    script_bytes = _validate_script_text(script_text)
    _require_path_free_json(render_proof)
    master_sha256 = _require_sha256(render_proof.get("video_sha256"), code="invalid_video_sha256")
    master_size = render_proof.get("video_size_bytes")
    if type(master_size) is not int or master_size < 0:
        _fail("invalid_render_proof", "Canonical render proof has invalid video size.")
    manifest = _validate_expected_package_manifest(
        dict(expected_package_manifest),
        package_basename=basename,
        render_proof=render_proof,
        script_sha256=_sha256_bytes(script_bytes),
        script_size_bytes=len(script_bytes),
    )
    root_fd = _acquire_output_root_authority(
        output_root=output_root,
        output_root_fd=output_root_fd,
    )
    try:
        try:
            os.stat(basename, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("package_exists", "A package with this name already exists.")
        staging_name = f".{basename}.staging-{secrets.token_hex(12)}"
        os.mkdir(staging_name, mode=0o700, dir_fd=root_fd)
        staging_fd = _open_child_directory(root_fd, staging_name)
        try:
            _cancel_if_requested(cancelled)
            _inject(fault_inject, "package_staging_created")
            _copy_verified_master_at(
                Path(verified_master),
                staging_fd,
                expected_sha256=master_sha256,
                expected_size=master_size,
                cancelled=cancelled,
            )
            _inject(fault_inject, "package_video_written")
            _write_bytes_at(staging_fd, SCRIPT_FILENAME, script_bytes)
            _inject(fault_inject, "package_script_written")
            usage_bytes = usage_text_from_manifest(manifest).encode("utf-8")
            manifest_bytes = canonical_json(manifest).encode("utf-8")
            manifest_sha256 = _sha256_bytes(manifest_bytes)
            _write_bytes_at(staging_fd, MANIFEST_FILENAME, manifest_bytes)
            _write_bytes_at(staging_fd, USAGE_FILENAME, usage_bytes)
            _inject(fault_inject, "package_manifest_written")
            marker = {
                "object": PACKAGE_MARKER_OBJECT,
                "schema_version": PACKAGE_SCHEMA_VERSION,
                "completion_state": "complete",
                "package_basename": basename,
                "package_manifest_sha256": manifest_sha256,
                "required_file_sha256": {
                    VIDEO_FILENAME: master_sha256,
                    SCRIPT_FILENAME: _sha256_bytes(script_bytes),
                    MANIFEST_FILENAME: manifest_sha256,
                    USAGE_FILENAME: _sha256_bytes(usage_bytes),
                },
            }
            _write_bytes_at(
                staging_fd,
                COMPLETION_MARKER_FILENAME,
                canonical_json(marker).encode("utf-8"),
            )
            os.fsync(staging_fd)
            _inject(fault_inject, "package_completion_marker_written")
            _verify_package_directory(
                staging_fd,
                package_basename=basename,
                expected_package_manifest=manifest,
            )
            _cancel_if_requested(cancelled)
            _inject(fault_inject, "before_package_publish")
        finally:
            os.close(staging_fd)
        _rename_directory_no_replace(root_fd, staging_name, basename)
        _inject(fault_inject, "package_published")
        os.fsync(root_fd)
        final_fd = _open_child_directory(root_fd, basename)
        try:
            return _verify_package_directory(
                final_fd,
                package_basename=basename,
                expected_package_manifest=manifest,
            )
        finally:
            os.close(final_fd)
    finally:
        os.close(root_fd)


def verify_existing_package(
    *,
    output_root: Path | None = None,
    output_root_fd: int | None = None,
    package_basename: str,
    expected_package_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Reconcile commit_pending from final package bytes without trusting DB state."""

    basename = _validate_safe_basename(package_basename)
    if expected_package_manifest is not None:
        _require_path_free_json(expected_package_manifest)
    root_fd = _acquire_output_root_authority(
        output_root=output_root,
        output_root_fd=output_root_fd,
    )
    try:
        directory_fd = _open_child_directory(root_fd, basename)
        try:
            return _verify_package_directory(
                directory_fd,
                package_basename=basename,
                expected_package_manifest=expected_package_manifest,
            )
        finally:
            os.close(directory_fd)
    finally:
        os.close(root_fd)


def inspect_existing_package(
    *,
    output_root: Path | None = None,
    output_root_fd: int | None = None,
    package_basename: str,
    expected_package_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return a path-free state projection for interrupted-package recovery."""

    try:
        proof = verify_existing_package(
            output_root=output_root,
            output_root_fd=output_root_fd,
            package_basename=package_basename,
            expected_package_manifest=expected_package_manifest,
        )
    except CanonicalExportArtifactError as exc:
        if exc.code == "package_missing":
            return {"state": "missing", "code": exc.code}
        if exc.code == "package_incomplete":
            return {"state": "incomplete", "code": exc.code}
        return {"state": "invalid", "code": exc.code}
    return {"state": "complete", "proof": proof}


def inspect_package_directory(
    *,
    package_directory: Path,
    package_basename: str,
    expected_package_manifest: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Inspect a job-owned staging directory before or after marker creation."""

    basename = _validate_safe_basename(package_basename)
    try:
        directory_fd = _open_absolute_directory_no_follow(Path(package_directory))
    except (FileNotFoundError, NotADirectoryError, OSError):
        return {"state": "missing", "code": "package_missing"}
    try:
        try:
            os.stat(COMPLETION_MARKER_FILENAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return {"state": "incomplete", "code": "package_incomplete"}
        try:
            proof = _verify_package_directory(
                directory_fd,
                package_basename=basename,
                expected_package_manifest=expected_package_manifest,
            )
        except CanonicalExportArtifactError as exc:
            return {"state": "invalid", "code": exc.code}
        return {"state": "complete", "proof": proof}
    finally:
        os.close(directory_fd)


def quarantine_published_package(
    *,
    output_root: Path | None = None,
    output_root_fd: int | None = None,
    package_basename: str,
    expected_package_manifest: Mapping[str, object],
) -> dict[str, object]:
    """Hide an exact orphaned package without deleting or overwriting bytes.

    This is intentionally narrower than cleanup: the package must validate
    against the immutable job/export context before one exclusive atomic rename.
    A retry verifies and reuses the deterministic hidden quarantine identity.
    """

    basename = _validate_safe_basename(package_basename)
    expected = deepcopy(dict(expected_package_manifest))
    _require_path_free_json(expected)
    if (
        set(expected) != _PACKAGE_MANIFEST_FIELDS
        or expected.get("object") != PACKAGE_MANIFEST_OBJECT
        or expected.get("schema_version") != PACKAGE_SCHEMA_VERSION
        or expected.get("package_basename") != basename
        or expected.get("profile") != CANONICAL_RENDER_PROFILE
        or expected.get("package_roles") != list(PACKAGE_ROLES)
        or expected.get("required_files") != list(REQUIRED_PACKAGE_FILES)
        or expected.get("source_policy")
        != {
            "library_originals_copied": False,
            "library_originals_moved": False,
            "library_originals_modified": False,
            "automatic_upload": False,
        }
        or expected.get("optional_roles") != {"subtitle": "absent", "cover": "absent"}
    ):
        _fail("invalid_package_manifest", "Expected package manifest is invalid.")
    _require_identifier(expected.get("export_id"), code="invalid_package_manifest")
    _require_identifier(expected.get("job_id"), code="invalid_package_manifest")
    _require_identifier(expected.get("project_id"), code="invalid_package_manifest")
    expected_timeline_binding = _validate_timeline_binding(expected.get("timeline_binding"))
    quarantine_id = canonical_sha256(
        {
            "object": "memolens.export_package_quarantine",
            "schema_version": "1",
            "package_basename": basename,
            "export_id": expected["export_id"],
            "job_id": expected["job_id"],
            "timeline_revision_sha256": expected_timeline_binding["revision_sha256"],
        }
    )
    quarantine_name = f".memolens-quarantine-{quarantine_id[:32]}"
    root_fd = _acquire_output_root_authority(
        output_root=output_root,
        output_root_fd=output_root_fd,
    )
    try:
        try:
            final_fd = _open_child_directory(root_fd, basename)
        except CanonicalExportArtifactError as exc:
            if exc.code != "package_missing":
                raise
            try:
                quarantine_fd = _open_child_directory(root_fd, quarantine_name)
            except CanonicalExportArtifactError as quarantine_exc:
                if quarantine_exc.code == "package_missing":
                    _fail("package_missing", "Published package is unavailable for quarantine.")
                raise
            try:
                proof = _verify_package_directory(
                    quarantine_fd,
                    package_basename=basename,
                    expected_package_manifest=expected,
                )
            finally:
                os.close(quarantine_fd)
            result = {
                "state": "quarantined",
                "quarantine_id": quarantine_id,
                "package_basename": basename,
                "replayed": True,
                "artifact_proof": proof,
            }
            _require_path_free_json(result)
            return result
        try:
            proof = _verify_package_directory(
                final_fd,
                package_basename=basename,
                expected_package_manifest=expected,
            )
        finally:
            os.close(final_fd)
        try:
            os.stat(quarantine_name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _fail("quarantine_exists", "The exact quarantine identity already exists.")
        _rename_directory_no_replace(root_fd, basename, quarantine_name)
        os.fsync(root_fd)
        result = {
            "state": "quarantined",
            "quarantine_id": quarantine_id,
            "package_basename": basename,
            "replayed": False,
            "artifact_proof": proof,
        }
        _require_path_free_json(result)
        return result
    finally:
        os.close(root_fd)


__all__ = [
    "ACTUAL_READ_MANIFEST_OBJECT",
    "CANONICAL_EXPORT_ARTIFACTS_VERSION",
    "CANONICAL_RENDER_PROFILE",
    "COMPLETION_MARKER_FILENAME",
    "CanonicalExportArtifactError",
    "CanonicalRenderPlan",
    "MANIFEST_FILENAME",
    "PACKAGE_MANIFEST_OBJECT",
    "PACKAGE_MARKER_OBJECT",
    "PACKAGE_ROLES",
    "REQUIRED_PACKAGE_FILES",
    "RUNTIME_MANIFEST_OBJECT",
    "SCRIPT_FILENAME",
    "USAGE_FILENAME",
    "VIDEO_FILENAME",
    "build_canonical_render_plan",
    "build_lightweight_package",
    "derive_usage_occurrences",
    "inspect_existing_package",
    "inspect_package_directory",
    "quarantine_published_package",
    "render_canonical_master",
    "project_script_text",
    "script_artifact_proof",
    "usage_text_from_manifest",
    "verify_existing_package",
]
