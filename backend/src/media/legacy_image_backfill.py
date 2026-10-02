from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from backend.src.media.import_plan import IMAGE_ANALYSIS_PROFILE
from backend.src.media.importing import MediaImportService
from backend.src.media.video import IMAGE_EXTENSIONS
from core.image_analysis_persistence import LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE
from core.media_db import (
    IdempotentWriteResult,
    MediaRepository,
    canonical_json,
)


LEGACY_IMAGE_BACKFILL_BATCH_SCOPE = (
    "desktop:POST:/v1/image-analysis/legacy-backfill-batches"
)
DEFAULT_LEGACY_IMAGE_BACKFILL_LIMIT = 50
MAX_LEGACY_IMAGE_BACKFILL_LIMIT = 100
MAX_CURSOR_BYTES = 2048
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass
class LegacyImageBackfillError(ValueError):
    code: str
    message: str
    status: int = 409
    details: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class _BatchRequest:
    library_root_id: str
    limit: int
    after_legacy_id: str | None
    dry_run: bool


@dataclass(frozen=True)
class _LegacyCandidate:
    legacy_id: str
    asset_sha256: str
    relative_path: str
    mime_type: str
    source_binding_sha256: str

    def public_identity(self) -> dict[str, str]:
        return {
            "legacy_id": self.legacy_id,
            "asset_sha256": self.asset_sha256,
            "source_binding_sha256": self.source_binding_sha256,
        }


class LegacyImageBackfillService:
    """Explicitly admit legacy-only rows into the canonical image pipeline.

    Legacy descriptions, tags, embeddings, geocodes, and quality fields never
    cross this boundary.  A row contributes only an expected ID, relative
    source name, and SHA-256; the source is reopened through a no-follow root
    descriptor, re-hashed, probed, and then handed to the existing canonical
    import and image-analysis admission code.
    """

    def __init__(
        self,
        repository: MediaRepository,
        job_runner: object,
        *,
        runtime_generation: str,
        database_file_identity: tuple[int, int],
    ) -> None:
        if not runtime_generation:
            raise ValueError("A pinned runtime generation is required.")
        self.repository = repository
        self.job_runner = job_runner
        self.runtime_generation = runtime_generation
        self.database_file_identity = database_file_identity
        self.database_uuid = repository.database_uuid
        self.import_service = MediaImportService(repository, job_runner)  # type: ignore[arg-type]

    def run(
        self,
        payload: Mapping[str, object],
        *,
        idempotency_key: str,
        request_sha256: str,
    ) -> IdempotentWriteResult:
        replay = self.repository.replay_idempotent_write(
            scope=LEGACY_IMAGE_BACKFILL_BATCH_SCOPE,
            key=idempotency_key,
            request_sha256=request_sha256,
        )
        if replay is not None:
            self._submit_frozen_jobs(replay.response)
            return replay

        request = self._parse_request(payload)
        root_record = self.repository.library_root(request.library_root_id)
        if root_record is None or root_record.get("status") != "active":
            raise LegacyImageBackfillError(
                "legacy_backfill_root_unavailable",
                "The approved Library root is unavailable.",
            )
        try:
            root = self.repository.validate_library_root(request.library_root_id)
        except (OSError, ValueError) as exc:
            raise LegacyImageBackfillError(
                "legacy_backfill_root_unavailable",
                "The approved Library root no longer matches its persisted identity.",
            ) from exc

        candidates, has_more = self._freeze_candidates(request)
        candidate_set_sha256 = self._candidate_set_sha256(candidates)
        next_cursor = (
            self._encode_cursor(request.library_root_id, candidates[-1].legacy_id)
            if has_more and candidates
            else None
        )
        operation_material = (
            f"{LEGACY_IMAGE_BACKFILL_BATCH_SCOPE}\0{idempotency_key}\0"
            f"{request_sha256}\0{candidate_set_sha256}"
        )
        operation_id = (
            "legacy_backfill_"
            f"{hashlib.sha256(operation_material.encode()).hexdigest()[:24]}"
        )

        if not candidates:
            return self.repository.execute_idempotent_write(
                scope=LEGACY_IMAGE_BACKFILL_BATCH_SCOPE,
                key=idempotency_key,
                request_sha256=request_sha256,
                resource_type="legacy_image_backfill_batch",
                mutation=lambda _connection: (
                    self._response_body(
                        operation_id=operation_id,
                        request=request,
                        candidates=(),
                        candidate_set_sha256=candidate_set_sha256,
                        jobs=(),
                        asset_ids={},
                        status="empty",
                        has_more=False,
                        next_cursor=None,
                    ),
                    200,
                    operation_id,
                ),
            )

        self._validate_candidate_rows_before_filesystem(
            candidates,
            library_root_id=request.library_root_id,
        )
        plan = self.import_service.prepare_import(
            root_id=request.library_root_id,
            root=root,
            payload={
                "relative_paths": [item.relative_path for item in candidates],
                "recursive": False,
                # Even a dry run must decode/probe the held bytes.  Dry-run only
                # suppresses domain writes; it does not weaken source proof.
                "dry_run": False,
                "kinds": ["image"],
            },
        )
        try:
            self._validate_prepared_plan(candidates, plan)
            if request.dry_run:
                committed = self.repository.execute_idempotent_write(
                    scope=LEGACY_IMAGE_BACKFILL_BATCH_SCOPE,
                    key=idempotency_key,
                    request_sha256=request_sha256,
                    resource_type="legacy_image_backfill_batch",
                    mutation=lambda connection: self._apply_dry_run(
                        connection,
                        operation_id=operation_id,
                        request=request,
                        candidates=candidates,
                        candidate_set_sha256=candidate_set_sha256,
                        has_more=has_more,
                        next_cursor=next_cursor,
                    ),
                )
            else:
                committed = self.repository.execute_idempotent_write(
                    scope=LEGACY_IMAGE_BACKFILL_BATCH_SCOPE,
                    key=idempotency_key,
                    request_sha256=request_sha256,
                    resource_type="legacy_image_backfill_batch",
                    mutation=lambda connection: self._apply_batch(
                        connection,
                        operation_id=operation_id,
                        request=request,
                        candidates=candidates,
                        candidate_set_sha256=candidate_set_sha256,
                        has_more=has_more,
                        next_cursor=next_cursor,
                        plan=plan,
                    ),
                )
        finally:
            plan.close()
        self._submit_frozen_jobs(committed.response)
        return committed

    def _parse_request(self, payload: Mapping[str, object]) -> _BatchRequest:
        if type(payload) is not dict:
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_request",
                "Request body must be a JSON object.",
                400,
            )
        allowed = {"library_root_id", "limit", "cursor", "dry_run"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_request",
                "Unsupported legacy backfill fields: " + ", ".join(unknown) + ".",
                400,
            )
        root_id = payload.get("library_root_id")
        if (
            type(root_id) is not str
            or not 1 <= len(root_id) <= 200
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", root_id) is None
        ):
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_request",
                "`library_root_id` must be one bounded approved-root identifier.",
                400,
            )
        limit = payload.get("limit", DEFAULT_LEGACY_IMAGE_BACKFILL_LIMIT)
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_LEGACY_IMAGE_BACKFILL_LIMIT
        ):
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_limit",
                f"`limit` must be an integer from 1 to {MAX_LEGACY_IMAGE_BACKFILL_LIMIT}.",
                400,
            )
        dry_run = payload.get("dry_run", False)
        if type(dry_run) is not bool:
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_request",
                "`dry_run` must be a boolean.",
                400,
            )
        cursor = payload.get("cursor")
        if cursor is not None and (
            type(cursor) is not str or not 1 <= len(cursor) <= MAX_CURSOR_BYTES
        ):
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_cursor",
                "`cursor` must be one bounded opaque cursor.",
                400,
            )
        after = self._decode_cursor(str(root_id), cursor) if cursor else None
        return _BatchRequest(
            library_root_id=str(root_id),
            limit=limit,
            after_legacy_id=after,
            dry_run=dry_run,
        )

    def _cursor_key(self) -> bytes:
        material = (
            f"legacy-image-backfill-cursor/v1\0{self.database_uuid}\0"
            f"{self.runtime_generation}"
        )
        return hashlib.sha256(material.encode()).digest()

    def _encode_cursor(self, root_id: str, after_legacy_id: str) -> str:
        payload = canonical_json(
            {
                "version": "1",
                "database_uuid": self.database_uuid,
                "library_root_id": root_id,
                "after_legacy_id": after_legacy_id,
            }
        ).encode()
        signature = hmac.new(self._cursor_key(), payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")

    def _decode_cursor(self, root_id: str, cursor: str) -> str:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.b64decode(
                padded.encode(),
                altchars=b"-_",
                validate=True,
            )
            if (
                base64.urlsafe_b64encode(decoded).decode().rstrip("=")
                != cursor
            ):
                raise ValueError("non-canonical cursor encoding")
            if len(decoded) <= 32 or len(decoded) > MAX_CURSOR_BYTES:
                raise ValueError("cursor length")
            payload, signature = decoded[:-32], decoded[-32:]
            if not hmac.compare_digest(
                signature,
                hmac.new(self._cursor_key(), payload, hashlib.sha256).digest(),
            ):
                raise ValueError("cursor signature")
            document = json.loads(payload.decode("utf-8"))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_cursor",
                "The opaque legacy backfill cursor is invalid for this runtime.",
                400,
            ) from exc
        if (
            type(document) is not dict
            or set(document)
            != {"version", "database_uuid", "library_root_id", "after_legacy_id"}
            or document.get("version") != "1"
            or document.get("database_uuid") != self.database_uuid
            or document.get("library_root_id") != root_id
            or type(document.get("after_legacy_id")) is not str
            or not 1 <= len(document["after_legacy_id"]) <= 256
        ):
            raise LegacyImageBackfillError(
                "invalid_legacy_backfill_cursor",
                "The opaque legacy backfill cursor is outside this database/root scope.",
                400,
            )
        return str(document["after_legacy_id"])

    def _freeze_candidates(
        self,
        request: _BatchRequest,
    ) -> tuple[tuple[_LegacyCandidate, ...], bool]:
        with self.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT legacy.id,legacy.sha256,legacy.relative_path,
                          legacy.mime_type
                     FROM image_index legacy
                    WHERE legacy.id>?
                      AND NOT EXISTS (
                        SELECT 1
                          FROM assets asset
                          JOIN image_analysis_heads head
                            ON head.asset_id=asset.id
                         WHERE asset.id=legacy.id
                           AND asset.kind='image'
                           AND asset.sha256=legacy.sha256)
                    ORDER BY legacy.id
                    LIMIT ?""",
                (request.after_legacy_id or "", request.limit + 1),
            ).fetchall()
        selected = rows[: request.limit]
        candidates = tuple(
            _LegacyCandidate(
                legacy_id=str(row["id"]),
                asset_sha256=str(row["sha256"]),
                relative_path=str(row["relative_path"]),
                mime_type=str(row["mime_type"]),
                source_binding_sha256=hashlib.sha256(
                    (
                        f"{request.library_root_id}\0{row['relative_path']}\0"
                        f"{row['sha256']}"
                    ).encode()
                ).hexdigest(),
            )
            for row in selected
        )
        return candidates, len(rows) > request.limit

    @staticmethod
    def _candidate_set_sha256(
        candidates: tuple[_LegacyCandidate, ...],
    ) -> str:
        # This digest deliberately contains no path.  The relative path is
        # committed through each source_binding_sha256 instead.
        document = [candidate.public_identity() for candidate in candidates]
        return hashlib.sha256(canonical_json(document).encode()).hexdigest()

    def _validate_candidate_rows_before_filesystem(
        self,
        candidates: tuple[_LegacyCandidate, ...],
        *,
        library_root_id: str,
    ) -> None:
        seen_bindings: set[str] = set()
        source_binding_by_sha256: dict[str, str] = {}
        with self.repository.transaction() as connection:
            for candidate in candidates:
                if (
                    not _SHA256.fullmatch(candidate.asset_sha256)
                    or not candidate.mime_type.casefold().startswith("image/")
                    or Path(candidate.relative_path).suffix.casefold()
                    not in IMAGE_EXTENSIONS
                ):
                    raise LegacyImageBackfillError(
                        "legacy_backfill_kind_conflict",
                        "A legacy candidate is not an admissible image source.",
                        details={"legacy_id": candidate.legacy_id},
                    )
                relative = Path(candidate.relative_path)
                if (
                    not candidate.relative_path
                    or "\x00" in candidate.relative_path
                    or relative.is_absolute()
                    or any(part in {"", ".", ".."} for part in relative.parts)
                ):
                    raise LegacyImageBackfillError(
                        "legacy_backfill_kind_conflict",
                        "A legacy candidate has an invalid source binding.",
                        details={"legacy_id": candidate.legacy_id},
                    )
                prior_sha_binding = source_binding_by_sha256.get(
                    candidate.asset_sha256
                )
                if (
                    prior_sha_binding is not None
                    and prior_sha_binding != candidate.source_binding_sha256
                ):
                    raise LegacyImageBackfillError(
                        "legacy_backfill_ambiguous_source",
                        "Byte-identical legacy rows cannot mint competing publication intents.",
                    )
                source_binding_by_sha256[candidate.asset_sha256] = (
                    candidate.source_binding_sha256
                )
                if candidate.source_binding_sha256 in seen_bindings:
                    raise LegacyImageBackfillError(
                        "legacy_backfill_ambiguous_source",
                        "More than one legacy row claims the same admitted source binding.",
                    )
                seen_bindings.add(candidate.source_binding_sha256)
                self._validate_database_conflicts(
                    connection,
                    candidate,
                    library_root_id=library_root_id,
                )

    @staticmethod
    def _validate_database_conflicts(
        connection: sqlite3.Connection,
        candidate: _LegacyCandidate,
        *,
        library_root_id: str,
    ) -> None:
        alias = connection.execute(
            """SELECT alias.canonical_asset_id,alias.library_root_id,
                      alias.source_id,source.relative_path,source.asset_id,
                      asset.kind,asset.sha256
                 FROM legacy_image_aliases alias
                 JOIN asset_sources source ON source.id=alias.source_id
                 JOIN assets asset ON asset.id=alias.canonical_asset_id
                WHERE alias.legacy_id=?""",
            (candidate.legacy_id,),
        ).fetchone()
        if alias is not None:
            raise LegacyImageBackfillError(
                "legacy_alias_conflict",
                "A legacy alias already owns this immutable reference.",
                details={"legacy_id": candidate.legacy_id},
            )

        same_id = connection.execute(
            "SELECT id,kind,sha256 FROM assets WHERE id=?",
            (candidate.legacy_id,),
        ).fetchone()
        if same_id is not None and same_id["kind"] != "image":
            raise LegacyImageBackfillError(
                "legacy_backfill_kind_conflict",
                "The legacy identifier collides with a non-image canonical asset.",
                details={"legacy_id": candidate.legacy_id},
            )
        if same_id is not None and same_id["sha256"] != candidate.asset_sha256:
            raise LegacyImageBackfillError(
                "legacy_alias_conflict",
                "The legacy identifier collides with different canonical bytes.",
                details={"legacy_id": candidate.legacy_id},
            )

        same_sha = connection.execute(
            "SELECT id,kind FROM assets WHERE sha256=?",
            (candidate.asset_sha256,),
        ).fetchone()
        if same_sha is not None and same_sha["kind"] != "image":
            raise LegacyImageBackfillError(
                "legacy_backfill_kind_conflict",
                "The legacy bytes already belong to a non-image canonical asset.",
                details={"legacy_id": candidate.legacy_id},
            )

        exact_source = connection.execute(
            """SELECT source.asset_id,asset.kind,asset.sha256
                 FROM asset_sources source
                 JOIN assets asset ON asset.id=source.asset_id
                WHERE source.library_root_id=? AND source.relative_path=?""",
            (library_root_id, candidate.relative_path),
        ).fetchone()
        if exact_source is not None and exact_source["kind"] != "image":
            raise LegacyImageBackfillError(
                "legacy_backfill_kind_conflict",
                "The admitted source path already belongs to a non-image asset.",
                details={"legacy_id": candidate.legacy_id},
            )
        if (
            exact_source is not None
            and exact_source["sha256"] != candidate.asset_sha256
        ):
            raise LegacyImageBackfillError(
                "legacy_alias_conflict",
                "The admitted source path is already bound to different bytes.",
                details={"legacy_id": candidate.legacy_id},
            )

    @staticmethod
    def _validate_prepared_plan(candidates, plan) -> None:
        if plan.rejected:
            raise LegacyImageBackfillError(
                "legacy_backfill_missing_source",
                "A legacy source is missing, replaced, symlinked, or unreadable.",
            )
        prepared_by_path = {item.relative_path: item for item in plan.assets}
        if len(prepared_by_path) != len(candidates):
            raise LegacyImageBackfillError(
                "legacy_backfill_missing_source",
                "The frozen legacy candidate set could not be reopened exactly.",
            )
        for candidate in candidates:
            prepared = prepared_by_path.get(candidate.relative_path)
            if prepared is None:
                raise LegacyImageBackfillError(
                    "legacy_backfill_missing_source",
                    "The frozen legacy candidate source is unavailable.",
                    details={"legacy_id": candidate.legacy_id},
                )
            if prepared.kind != "image" or prepared.probe_error is not None:
                raise LegacyImageBackfillError(
                    "legacy_backfill_kind_conflict",
                    "The held legacy source is not a valid image.",
                    details={"legacy_id": candidate.legacy_id},
                )
            if prepared.sha256 != candidate.asset_sha256:
                raise LegacyImageBackfillError(
                    "legacy_backfill_sha_mismatch",
                    "The held legacy source bytes do not match the frozen SHA-256.",
                    details={"legacy_id": candidate.legacy_id},
                )

    def _apply_dry_run(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        request: _BatchRequest,
        candidates: tuple[_LegacyCandidate, ...],
        candidate_set_sha256: str,
        has_more: bool,
        next_cursor: str | None,
    ) -> tuple[dict[str, object], int, str]:
        self._require_frozen_rows_in_transaction(connection, candidates)
        return (
            self._response_body(
                operation_id=operation_id,
                request=request,
                candidates=candidates,
                candidate_set_sha256=candidate_set_sha256,
                jobs=(),
                asset_ids={},
                status="dry_run",
                has_more=has_more,
                next_cursor=next_cursor,
            ),
            200,
            operation_id,
        )

    def _apply_batch(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        request: _BatchRequest,
        candidates: tuple[_LegacyCandidate, ...],
        candidate_set_sha256: str,
        has_more: bool,
        next_cursor: str | None,
        plan,
    ) -> tuple[dict[str, object], int, str]:
        self._require_frozen_rows_in_transaction(connection, candidates)
        for candidate in candidates:
            self._validate_database_conflicts(
                connection,
                candidate,
                library_root_id=request.library_root_id,
            )
        imported = self.import_service.apply_prepared(
            connection,
            root_id=request.library_root_id,
            plan=plan,
            retain_source_bindings=True,
        )
        if imported.rejected or len(imported.assets) != len(candidates):
            raise LegacyImageBackfillError(
                "legacy_backfill_source_changed",
                "The admitted legacy source set changed before commit.",
            )
        assets_by_path = {
            str(asset.get("relative_path") or ""): asset
            for asset in imported.assets
        }
        items_by_path = {item.relative_path: item for item in plan.assets}
        jobs: list[dict[str, object]] = []
        asset_ids: dict[str, str] = {}
        for candidate in candidates:
            asset = assets_by_path.get(candidate.relative_path)
            item = items_by_path.get(candidate.relative_path)
            if asset is None or item is None:
                raise LegacyImageBackfillError(
                    "legacy_backfill_source_changed",
                    "The admitted legacy source set changed before enqueue.",
                )
            asset_id = str(asset.get("id") or "")
            source_id = str(asset.get("asset_source_id") or "")
            if not asset_id or not source_id:
                raise LegacyImageBackfillError(
                    "legacy_backfill_source_changed",
                    "Canonical import returned an incomplete source binding.",
                )
            job = self._enqueue_candidate(
                connection,
                operation_id=operation_id,
                candidate=candidate,
                item=item,
                asset_id=asset_id,
                source_id=source_id,
                library_root_id=request.library_root_id,
                root_permission_fingerprint=plan.root_permission_fingerprint,
            )
            jobs.append(job)
            asset_ids[candidate.legacy_id] = asset_id
        body = self._response_body(
            operation_id=operation_id,
            request=request,
            candidates=candidates,
            candidate_set_sha256=candidate_set_sha256,
            jobs=tuple(jobs),
            asset_ids=asset_ids,
            status="queued",
            has_more=has_more,
            next_cursor=next_cursor,
        )
        return body, 202, operation_id

    @staticmethod
    def _require_frozen_rows_in_transaction(
        connection: sqlite3.Connection,
        candidates: tuple[_LegacyCandidate, ...],
    ) -> None:
        for candidate in candidates:
            row = connection.execute(
                """SELECT sha256,relative_path,mime_type FROM image_index
                    WHERE id=?""",
                (candidate.legacy_id,),
            ).fetchone()
            if (
                row is None
                or row["sha256"] != candidate.asset_sha256
                or row["relative_path"] != candidate.relative_path
                or row["mime_type"] != candidate.mime_type
            ):
                raise LegacyImageBackfillError(
                    "legacy_backfill_source_changed",
                    "The frozen legacy row changed before commit.",
                    details={"legacy_id": candidate.legacy_id},
                )

    def _enqueue_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        candidate: _LegacyCandidate,
        item,
        asset_id: str,
        source_id: str,
        library_root_id: str,
        root_permission_fingerprint: str | None,
    ) -> dict[str, object]:
        if root_permission_fingerprint is None:
            raise LegacyImageBackfillError(
                "legacy_backfill_root_unavailable",
                "The prepared root authority is incomplete.",
            )
        stable_material = (
            f"{self.database_uuid}\0{candidate.legacy_id}\0"
            f"{library_root_id}\0{candidate.relative_path}\0"
            f"{candidate.asset_sha256}"
        )
        enqueue_key = (
            "legacy-image-backfill-"
            f"{hashlib.sha256(stable_material.encode()).hexdigest()}"
        )
        existing = connection.execute(
            """SELECT binding.job_id,binding.asset_id,binding.source_id,
                      binding.library_root_id,binding.relative_path,
                      binding.input_asset_sha256
                 FROM image_analysis_job_bindings binding
                WHERE binding.database_uuid=? AND binding.enqueue_scope=?
                  AND binding.idempotency_key=?""",
            (
                self.database_uuid,
                LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE,
                enqueue_key,
            ),
        ).fetchone()
        if existing is not None:
            if (
                existing["asset_id"] != asset_id
                or existing["source_id"] != source_id
                or existing["library_root_id"] != library_root_id
                or existing["relative_path"] != candidate.relative_path
                or existing["input_asset_sha256"] != candidate.asset_sha256
            ):
                raise LegacyImageBackfillError(
                    "legacy_backfill_ambiguous_source",
                    "A prior backfill enqueue is bound to a different canonical source.",
                )
            job = self.repository.get_media_job_in_transaction(
                connection,
                str(existing["job_id"]),
            )
            if job is None:
                raise LegacyImageBackfillError(
                    "legacy_backfill_source_changed",
                    "A prior backfill job binding is incomplete.",
                )
            return job

        head = connection.execute(
            """SELECT analysis_run_id,revision,content_sha256
                 FROM image_analysis_heads WHERE asset_id=?""",
            (asset_id,),
        ).fetchone()
        identity = item.source_identity
        identity_material = (
            f"{identity.device}\0{identity.inode}\0{identity.size}\0"
            f"{identity.mtime_ns}\0{identity.ctime_ns}"
        )
        source_binding = {
            "source_id": source_id,
            "library_root_id": library_root_id,
            "relative_path": candidate.relative_path,
            "observed_size": identity.size,
            "observed_mtime_ns": identity.mtime_ns,
            "observed_ctime_ns": identity.ctime_ns,
            "source_device": identity.device,
            "source_inode": identity.inode,
            "file_identity_sha256": hashlib.sha256(
                identity_material.encode()
            ).hexdigest(),
            "root_permission_fingerprint": root_permission_fingerprint,
            "asset_id": asset_id,
            "input_asset_sha256": candidate.asset_sha256,
        }
        job = self.repository.enqueue_image_analysis_in_transaction(
            connection,
            runtime_generation=self.runtime_generation,
            database_file_identity=self.database_file_identity,
            source_binding=source_binding,
            asset_id=asset_id,
            source_id=source_id,
            analysis_profile=IMAGE_ANALYSIS_PROFILE,
            expected_head=dict(head) if head is not None else None,
            enqueue_scope=LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE,
            idempotency_key=enqueue_key,
        )
        if (
            type(job) is not dict
            or job.get("kind") != "image_analysis"
            or job.get("asset_id") != asset_id
        ):
            raise LegacyImageBackfillError(
                "legacy_backfill_source_changed",
                "Canonical image admission returned an invalid job.",
            )
        return job

    @staticmethod
    def _public_job(job: Mapping[str, object]) -> dict[str, object]:
        hidden = {"database_uuid", "checkpoint", "checkpoint_json", "error_json"}
        public = {key: value for key, value in job.items() if key not in hidden}
        public["object"] = "media.job"
        public["schema_version"] = "1"
        return public

    def _response_body(
        self,
        *,
        operation_id: str,
        request: _BatchRequest,
        candidates: tuple[_LegacyCandidate, ...],
        candidate_set_sha256: str,
        jobs: tuple[Mapping[str, object], ...],
        asset_ids: Mapping[str, str],
        status: str,
        has_more: bool,
        next_cursor: str | None,
    ) -> dict[str, object]:
        public_jobs = [self._public_job(job) for job in jobs]
        jobs_by_asset = {
            str(job.get("asset_id") or ""): str(job.get("id") or "")
            for job in jobs
        }
        return {
            "object": "memolens.image_analysis_legacy_backfill_batch",
            "schema_version": "1",
            "id": operation_id,
            "status": status,
            "dry_run": request.dry_run,
            "library_root_id": request.library_root_id,
            "candidate_count": len(candidates),
            "candidate_set_sha256": candidate_set_sha256,
            "candidates": [
                {
                    **candidate.public_identity(),
                    "asset_id": asset_ids.get(candidate.legacy_id),
                    "job_id": jobs_by_asset.get(
                        asset_ids.get(candidate.legacy_id, "")
                    ),
                    "outcome": "validated" if request.dry_run else "queued",
                }
                for candidate in candidates
            ],
            "jobs": public_jobs,
            "job": public_jobs[0] if public_jobs else None,
            "job_id": public_jobs[0].get("id") if public_jobs else None,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "legacy_metadata_reused": False,
            "external_analysis": False,
        }

    def _submit_frozen_jobs(self, response: Mapping[str, object]) -> None:
        raw_jobs = response.get("jobs")
        if type(raw_jobs) is not list:
            return
        jobs = [dict(job) for job in raw_jobs if type(job) is dict]
        self.import_service.submit_jobs(jobs)


__all__ = [
    "DEFAULT_LEGACY_IMAGE_BACKFILL_LIMIT",
    "LEGACY_IMAGE_BACKFILL_BATCH_SCOPE",
    "LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE",
    "MAX_LEGACY_IMAGE_BACKFILL_LIMIT",
    "LegacyImageBackfillError",
    "LegacyImageBackfillService",
]
