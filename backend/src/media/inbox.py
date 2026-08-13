from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from core.media_db import MediaRepository, MediaRevisionConflictError, canonical_json


INBOX_STATES = frozenset({"inbox", "kept", "archived"})
MEDIA_KINDS = frozenset({"image", "video", "audio"})
MAX_NOTE_LENGTH = 1_000


class InboxAssetNotFoundError(LookupError):
    pass


class ReviewRevisionConflictError(RuntimeError):
    def __init__(self, current_review: dict[str, object]):
        super().__init__("The asset review changed after this screen was loaded.")
        self.current_review = current_review


@dataclass(frozen=True)
class InboxPage:
    items: list[dict[str, object]]
    next_cursor: str | None
    summary: dict[str, int]

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


class MediaInboxService:
    """Revisioned, non-destructive review metadata for local media assets."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def list_assets(
        self,
        *,
        state: str = "inbox",
        kinds: str | list[str] | tuple[str, ...] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxPage:
        normalized_state = str(state or "inbox").strip().casefold()
        if normalized_state not in {*INBOX_STATES, "all"}:
            raise ValueError("`state` must be inbox, kept, archived, or all.")
        normalized_kinds = self._parse_kinds(kinds)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("`limit` must be an integer from 1 to 100.")
        cursor_value = self._decode_cursor(cursor) if cursor else None

        rows, summary = self.repository.list_inbox_assets(
            state=normalized_state,
            kinds=normalized_kinds,
            limit=limit,
            cursor_sort=cursor_value[0] if cursor_value else None,
            cursor_id=cursor_value[1] if cursor_value else None,
        )

        has_more = len(rows) > limit
        visible_rows = rows[:limit]
        items = [self._present_asset(row) for row in visible_rows]
        next_cursor = None
        if has_more and visible_rows:
            last = visible_rows[-1]
            next_cursor = self._encode_cursor(str(last["cursor_sort"]), str(last["id"]))
        return InboxPage(items=items, next_cursor=next_cursor, summary=summary)

    def get_review(self, asset_id: str) -> dict[str, object]:
        review = self.repository.get_asset_review(asset_id)
        if review is None:
            raise InboxAssetNotFoundError("Asset does not exist.")
        return review

    def update_review(
        self,
        asset_id: str,
        payload: dict[str, object],
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[dict[str, object], bool]:
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
            raise ValueError("`base_revision` must be a non-negative integer.")
        changes = self._validated_changes(payload)
        try:
            return self.repository.put_asset_review(
                asset_id=asset_id,
                base_revision=base_revision,
                changes=changes,
                idempotency_scope=idempotency_scope,
                idempotency_key=idempotency_key,
                request_sha256=request_sha256,
            )
        except LookupError as exc:
            raise InboxAssetNotFoundError(str(exc)) from exc
        except MediaRevisionConflictError as exc:
            raise ReviewRevisionConflictError(exc.current) from exc

    @staticmethod
    def _validated_changes(payload: dict[str, object]) -> dict[str, object]:
        allowed = {
            "base_revision",
            "inbox_state",
            "favorite",
            "project_ready",
            "note",
            "db_path",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"Unsupported review fields: {', '.join(unknown)}.")
        changes: dict[str, object] = {}
        if "inbox_state" in payload:
            state = payload["inbox_state"]
            if not isinstance(state, str) or state not in INBOX_STATES:
                raise ValueError("`inbox_state` must be inbox, kept, or archived.")
            changes["inbox_state"] = state
        for key in ("favorite", "project_ready"):
            if key in payload:
                value = payload[key]
                if not isinstance(value, bool):
                    raise ValueError(f"`{key}` must be a boolean.")
                changes[key] = value
        if "note" in payload:
            note = payload["note"]
            if note is not None and (not isinstance(note, str) or len(note.strip()) > MAX_NOTE_LENGTH):
                raise ValueError(f"`note` must be null or at most {MAX_NOTE_LENGTH} characters.")
            changes["note"] = note.strip() or None if isinstance(note, str) else None
        if not changes:
            raise ValueError("At least one review field must be provided.")
        return changes

    @staticmethod
    def _parse_kinds(raw: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        values: list[Any]
        if raw is None or raw == "":
            values = ["image", "video"]
        elif isinstance(raw, str):
            values = raw.split(",")
        elif isinstance(raw, (list, tuple)):
            values = list(raw)
        else:
            raise ValueError("`kinds` must be a comma-separated string.")
        normalized = tuple(dict.fromkeys(str(value).strip().casefold() for value in values if str(value).strip()))
        if not normalized or any(value not in MEDIA_KINDS for value in normalized):
            raise ValueError("`kinds` may contain image, video, or audio.")
        return normalized

    @staticmethod
    def _present_asset(row: Any) -> dict[str, object]:
        kind = str(row["kind"])
        keyframe_id = row["representative_keyframe_id"]
        thumbnail_url = (
            f"/v1/assets/{row['id']}/thumbnail"
            if kind == "image"
            else f"/v1/keyframes/{keyframe_id}"
            if kind == "video" and keyframe_id
            else None
        )
        return {
            "id": str(row["id"]),
            "kind": kind,
            "filename": str(row["filename"]),
            "captured_at": row["captured_at"],
            "width": row["width"],
            "height": row["height"],
            "duration_ms": row["duration_ms"],
            "thumbnail_url": thumbnail_url,
            "review": row["review"],
        }

    @staticmethod
    def _encode_cursor(sort_value: str, asset_id: str) -> str:
        raw = canonical_json({"sort": sort_value, "id": asset_id}).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[str, str]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 512:
            raise ValueError("`cursor` is invalid.")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("`cursor` is invalid.") from exc
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("sort"), str)
            or not isinstance(value.get("id"), str)
            or not value["sort"]
            or not value["id"]
        ):
            raise ValueError("`cursor` is invalid.")
        return value["sort"], value["id"]
