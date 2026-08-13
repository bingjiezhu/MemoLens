from __future__ import annotations

import copy
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from core.media_db import IdempotentWriteResult, MediaRepository, canonical_json, new_id
from .timeline_operations import TimelineEditor, operation_summary, reflow_timeline
from .timeline_validation import MAX_TIMELINE_DURATION_MS, TimelineValidator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _operation_id(timeline_id: str, base_revision: int, value: object, index: int) -> str:
    digest = hashlib.sha256(f"{timeline_id}\0{base_revision}\0{index}\0{canonical_json(value)}".encode()).hexdigest()
    return f"op_{digest[:24]}"


class TimelineValidationError(ValueError):
    def __init__(self, errors: list[dict[str, object]]):
        super().__init__(str(errors[0].get("message")) if errors else "Timeline is invalid.")
        self.errors = errors


class TimelineService:
    """Deterministic timeline authority. FFmpeg never consumes free-form instructions."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository
        self._validator = TimelineValidator(repository)
        self._editor = TimelineEditor(repository, self._clip_from_match)

    def create_from_project(
        self,
        project_id: str,
        *,
        brief_revision: int = 1,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        project = self.repository.get_project(project_id)
        brief_row = self.repository.get_brief(project_id, brief_revision)
        if project is None or brief_row is None:
            raise LookupError("Creative project or brief revision does not exist.")
        brief = brief_row["brief"]
        raw_candidates = brief.get("candidate_refs") if isinstance(brief, dict) else None
        candidates = [item for item in raw_candidates or [] if isinstance(item, dict)]
        if not candidates:
            raise ValueError("The creative brief has no grounded candidate assets.")

        target = min(max(_integer(brief.get("duration_ms")) or 30_000, 1_000), MAX_TIMELINE_DURATION_MS)
        clips: list[dict[str, object]] = []
        cursor = 0
        for candidate in candidates:
            if cursor >= target:
                break
            clip = self._clip_from_match(candidate, timeline_start_ms=cursor)
            if clip is None:
                continue
            duration = min(int(clip["timeline_duration_ms"]), target - cursor)
            if duration <= 0:
                continue
            clip["timeline_duration_ms"] = duration
            if clip["kind"] == "video":
                clip["source_out_ms"] = int(clip["source_in_ms"]) + duration
            clips.append(clip)
            cursor += duration
        if not clips:
            raise ValueError("No grounded candidate can be used in a timeline.")

        timeline_id = new_id("tl")
        ratio = str(brief.get("aspect_ratio") or "16:9")
        width, height = {
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
            "16:9": (1920, 1080),
        }.get(ratio, (1920, 1080))
        timeline: dict[str, object] = {
            "schema_version": "1.0",
            "id": timeline_id,
            "project_id": project_id,
            "revision": 1,
            "format": {
                "width": width,
                "height": height,
                "fps": 30,
                "sample_rate": 48_000,
                "duration_ms": cursor,
                "background_color": "#000000",
            },
            "tracks": [
                {
                    "id": new_id("track"),
                    "type": "video",
                    "role": "primary",
                    "z_index": 0,
                    "muted": False,
                    "clips": clips,
                }
            ],
            "transitions": [],
            "provenance": self._provenance(
                clips,
                created_by="director",
                parent_revision=None,
                brief_revision=brief_revision,
                operations=[],
            ),
        }
        validation = self.validate(timeline)
        if not validation["valid"]:
            raise TimelineValidationError(validation["errors"])
        saved = self.repository.save_timeline(
            timeline_id=timeline_id,
            project_id=project_id,
            revision=1,
            timeline=timeline,
            provenance=timeline["provenance"],
            validation_status="valid",
            connection=connection,
        )
        return {
            "timeline": saved["timeline"],
            "content_sha256": saved["content_sha256"],
            "validation": validation,
            "diff": [],
        }

    def create_from_project_idempotent(
        self,
        project_id: str,
        *,
        brief_revision: int,
        idempotency_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> IdempotentWriteResult:
        def mutation(connection: sqlite3.Connection) -> tuple[dict[str, object], int, str]:
            result = self.create_from_project(
                project_id,
                brief_revision=brief_revision,
                connection=connection,
            )
            timeline_id = str(result["timeline"]["id"])
            body = {
                "object": "timeline.revision",
                "schema_version": "1",
                "id": timeline_id,
                **result,
            }
            return body, 201, timeline_id

        return self.repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=idempotency_key,
            request_sha256=request_sha256,
            resource_type="timeline",
            mutation=mutation,
        )

    def _clip_from_match(
        self,
        match: dict[str, object],
        *,
        timeline_start_ms: int,
        duration_override_ms: int | None = None,
    ) -> dict[str, object] | None:
        if match.get("result_type") == "video_segment":
            segment = self.repository.get_segment(str(match.get("id") or ""))
            if not segment or segment.get("source_availability") != "available":
                return None
            source_in = int(segment["start_ms"])
            available = int(segment["end_ms"]) - source_in
            duration = min(duration_override_ms or available, available)
            asset = self.repository.get_asset(str(segment["asset_id"]))
            if not asset:
                return None
            return self._base_clip(
                kind="video",
                asset=asset,
                asset_source_id=str(segment["asset_source_id"]),
                segment_id=str(segment["id"]),
                source_in_ms=source_in,
                source_out_ms=source_in + duration,
                timeline_start_ms=timeline_start_ms,
                timeline_duration_ms=duration,
                match_id=str(segment["id"]),
                reason="Grounded video segment selected by mixed retrieval.",
            )
        asset_id = str(match.get("asset_id") or match.get("id") or "")
        asset = self.repository.get_asset(asset_id)
        if not asset or asset.get("kind") != "image" or asset.get("source_availability") != "available":
            return None
        return self._base_clip(
            kind="image",
            asset=asset,
            asset_source_id=str(asset["asset_source_id"]),
            segment_id=None,
            source_in_ms=None,
            source_out_ms=None,
            timeline_start_ms=timeline_start_ms,
            timeline_duration_ms=duration_override_ms or 3_000,
            match_id=asset_id,
            reason="Grounded image selected by mixed retrieval.",
        )

    @staticmethod
    def _base_clip(
        *,
        kind: str,
        asset: dict[str, object],
        asset_source_id: str,
        segment_id: str | None,
        source_in_ms: int | None,
        source_out_ms: int | None,
        timeline_start_ms: int,
        timeline_duration_ms: int,
        match_id: str,
        reason: str,
    ) -> dict[str, object]:
        value: dict[str, object] = {
            "id": new_id("clip"),
            "kind": kind,
            "asset_id": str(asset["id"]),
            "asset_source_id": asset_source_id,
            "segment_id": segment_id,
            "timeline_start_ms": timeline_start_ms,
            "timeline_duration_ms": timeline_duration_ms,
            "fit": "cover",
            "crop": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
            "volume_db": 0.0,
            "audio_enabled": kind == "video",
            "provenance": {"reason": reason, "match_id": match_id},
        }
        if kind == "video":
            value["source_in_ms"] = source_in_ms
            value["source_out_ms"] = source_out_ms
        return value

    def _provenance(
        self,
        clips: list[dict[str, object]],
        *,
        created_by: str,
        parent_revision: int | None,
        brief_revision: int,
        operations: list[dict[str, object]],
    ) -> dict[str, object]:
        source_assets: dict[str, object] = {}
        source_runs: dict[str, str] = {}
        for clip in clips:
            asset = self.repository.get_asset(str(clip.get("asset_id") or ""))
            if asset:
                source_assets[str(asset["id"])] = {
                    "sha256": str(asset["sha256"]),
                    "asset_source_id": str(clip.get("asset_source_id") or ""),
                }
            if clip.get("segment_id"):
                segment = self.repository.get_segment(str(clip["segment_id"]))
                if segment:
                    source_runs[str(segment["id"])] = str(segment["analysis_run_id"])
        return {
            "created_by": created_by,
            "parent_revision": parent_revision,
            "brief_revision": brief_revision,
            "operations": operations,
            "source_analysis_runs": source_runs,
            "source_assets": source_assets,
            "created_at": _now(),
        }

    def validate(self, timeline: object) -> dict[str, object]:
        return self._validator.validate(timeline)

    def instruction_operations(self, timeline_id: str, base_revision: int, instruction: str) -> list[dict[str, object]]:
        row = self.repository.get_timeline(timeline_id, base_revision)
        if not row:
            raise LookupError("Timeline revision does not exist.")
        clips = list(clips_in_render_order(row["timeline"]))
        clauses = [
            value.strip()
            for value in re.split(r"[,;，；]|\b(?:and|then)\b|然后|再把", instruction, flags=re.I)
            if value.strip()
        ]
        operations: list[dict[str, object]] = []
        chinese = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        for clause in clauses:
            index: int | None = None
            if re.search(r"最后|last", clause, re.I):
                index = len(clips) - 1
            else:
                match = re.search(r"第\s*([1-9一二两三四五六七八九])|(?:clip|shot)\s*#?\s*(\d+)", clause, re.I)
                if match:
                    ordinal = (
                        int(match.group(2))
                        if match.group(2)
                        else chinese.get(match.group(1), int(match.group(1)) if str(match.group(1)).isdigit() else 0)
                    )
                    index = ordinal - 1
            amount_match = re.search(r"(\d+(?:\.\d+)?)\s*(ms|毫秒|s|sec(?:ond)?s?|秒)", clause, re.I)
            if index is None or not 0 <= index < len(clips) or not amount_match:
                raise ValueError("instruction_not_safely_understood")
            delta = round(
                float(amount_match.group(1)) * (1 if amount_match.group(2).lower() in {"ms", "毫秒"} else 1000)
            )
            clip = clips[index]
            if re.search(r"缩短|减少|shorten|trim", clause, re.I):
                next_duration = int(clip["timeline_duration_ms"]) - delta
            elif re.search(r"延长|加长|extend|longer", clause, re.I):
                next_duration = int(clip["timeline_duration_ms"]) + delta
            else:
                raise ValueError("instruction_not_safely_understood")
            if next_duration <= 0:
                raise ValueError("instruction_would_create_invalid_duration")
            if clip.get("kind") == "video":
                source_in = int(clip["source_in_ms"])
                operation = {
                    "op": "trim_clip",
                    "clip_id": clip["id"],
                    "source_in_ms": source_in,
                    "source_out_ms": source_in + next_duration,
                }
            else:
                operation = {"op": "set_duration", "clip_id": clip["id"], "timeline_duration_ms": next_duration}
            operations.append(operation)
        return self.normalize_operations(timeline_id, base_revision, operations)

    def normalize_operations(
        self,
        timeline_id: str,
        base_revision: int,
        operations: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        normalized: list[dict[str, object]] = []
        aliases = {
            "delete_clip": "remove_clip",
            "set_clip_duration": "set_duration",
            "set_transition": "add_transition",
        }
        for index, raw in enumerate(operations):
            if not isinstance(raw, dict):
                raise ValueError(f"operations[{index}] must be an object.")
            operation = copy.deepcopy(raw)
            operation["op"] = aliases.get(str(operation.get("op")), operation.get("op"))
            operation.setdefault("op_id", _operation_id(timeline_id, base_revision, operation, index))
            operation.setdefault("preconditions", {"timeline_revision": base_revision})
            normalized.append(operation)
        return normalized

    def preview_revision(
        self,
        timeline_id: str,
        *,
        base_revision: int,
        operations: list[dict[str, object]],
    ) -> dict[str, object]:
        current = self.repository.get_timeline(timeline_id)
        if not current:
            raise LookupError("Timeline does not exist.")
        if int(current["revision"]) != base_revision:
            raise RuntimeError(f"revision_conflict:{current['revision']}")
        normalized = self.normalize_operations(timeline_id, base_revision, operations)
        timeline = copy.deepcopy(current["timeline"])
        diff: list[dict[str, object]] = []
        for operation in normalized:
            before = copy.deepcopy(timeline)
            self._editor.apply(timeline, operation)
            diff.append(
                {
                    "op": operation["op"],
                    "op_id": operation["op_id"],
                    "summary": operation_summary(operation),
                    "clip_id": operation.get("clip_id"),
                    "before_sha256": hashlib.sha256(canonical_json(before).encode()).hexdigest(),
                    "after_sha256": hashlib.sha256(canonical_json(timeline).encode()).hexdigest(),
                }
            )
        reflow_timeline(timeline)
        timeline["revision"] = base_revision + 1
        timeline["provenance"] = self._provenance(
            list(clips_in_render_order(timeline)),
            created_by="user",
            parent_revision=base_revision,
            brief_revision=int(current["brief_revision"]),
            operations=normalized,
        )
        validation = self.validate(timeline)
        if not validation["valid"]:
            raise TimelineValidationError(validation["errors"])
        return {
            "timeline": timeline,
            "operations": normalized,
            "diff": diff,
            "validation": validation,
            "content_sha256": hashlib.sha256(canonical_json(timeline).encode()).hexdigest(),
        }

    def revise(
        self,
        timeline_id: str,
        *,
        base_revision: int,
        operations: list[dict[str, object]],
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, object]:
        preview = self.preview_revision(timeline_id, base_revision=base_revision, operations=operations)
        timeline = preview["timeline"]
        current = self.repository.get_timeline(timeline_id, base_revision)
        if not current:
            raise RuntimeError("revision_conflict:0")
        saved = self.repository.save_timeline_revision_cas(
            timeline_id=timeline_id,
            project_id=str(timeline["project_id"]),
            base_revision=base_revision,
            expected_base_sha256=str(current["content_sha256"]),
            timeline=timeline,
            provenance=timeline["provenance"],
            validation_status="valid",
            connection=connection,
        )
        return {**preview, "timeline": saved["timeline"], "content_sha256": saved["content_sha256"]}

    def revise_idempotent(
        self,
        timeline_id: str,
        *,
        base_revision: int,
        operations: list[dict[str, object]],
        idempotency_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> IdempotentWriteResult:
        def mutation(connection: sqlite3.Connection) -> tuple[dict[str, object], int, str]:
            result = self.revise(
                timeline_id,
                base_revision=base_revision,
                operations=operations,
                connection=connection,
            )
            body = {
                "object": "timeline.revision",
                "schema_version": "1",
                "id": timeline_id,
                **result,
            }
            return body, 201, f"{timeline_id}:{result['timeline']['revision']}"

        return self.repository.execute_idempotent_write(
            scope=idempotency_scope,
            key=idempotency_key,
            request_sha256=request_sha256,
            resource_type="timeline_revision",
            mutation=mutation,
        )


def clips_in_render_order(timeline: dict[str, object]) -> Iterable[dict[str, object]]:
    tracks = timeline.get("tracks") if isinstance(timeline.get("tracks"), list) else []
    primary = [track for track in tracks if isinstance(track, dict) and track.get("role") == "primary"]
    if len(primary) != 1 or not isinstance(primary[0].get("clips"), list):
        return []
    return [clip for clip in primary[0]["clips"] if isinstance(clip, dict)]
