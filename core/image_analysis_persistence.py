"""Canonical image job admission and publication for :mod:`core.media_db`.

This module is a mixin instead of another repository.  The managed SQLite
database remains owned by ``MediaRepository``; splitting the image-specific
logic here keeps the additive V14 bridge reviewable without introducing a
second transaction or a second source of truth.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from typing import BinaryIO
import uuid

from .image_analysis_contract import (
    IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION,
    ImageAnalysisContractError,
    build_projection_manifest,
    canonical_sha256,
    canonical_image_analysis_result_json,
    canonical_projection_change_json,
    canonical_projection_manifest_json,
    canonical_projection_receipt_json,
    canonical_publish_receipt_json,
    require_valid_image_analysis_result,
    require_valid_projection_change,
    require_valid_projection_manifest,
    require_valid_projection_receipt,
    require_valid_publish_receipt,
    seal_projection_change,
    seal_projection_receipt,
    seal_publish_receipt,
    source_binding_sha256,
)
from .image_analysis_job_contract import (
    IMAGE_ANALYSIS_MAX_ATTEMPTS,
    IMAGE_ANALYSIS_WORK_STAGES,
    IMAGE_ANALYSIS_WORKER_KIND,
    ImageAnalysisJobContractError,
    canonical_image_analysis_attempt_authority_json,
    canonical_json as canonical_job_json,
    canonical_sha256 as canonical_job_sha256,
    image_analysis_request_sha256,
    require_valid_image_analysis_attempt_authority,
    require_valid_image_analysis_checkpoint,
    require_valid_image_analysis_error,
    require_valid_image_analysis_request,
    seal_image_analysis_attempt_authority,
)
from .image_projection_renderer import (
    IMAGE_INDEX_SQL_COLUMNS,
    IMAGE_PROJECTOR_VERSION,
    ImageProjectionRenderError,
    materialize_projected_image_index_row,
    materialize_projected_image_index_values,
    render_projected_image_index_row,
    require_valid_projected_image_index_row,
)


LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE = "legacy-image-backfill/image-analysis"
IMAGE_PROJECTION_CONTRACT = "legacy-image-index-shadow/v1"
IMAGE_PROJECTION_COMPILER_ID = "memolens.image-manifest/v1"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _parse_json(value: object) -> dict[str, object] | None:
    if type(value) is not str:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if type(parsed) is dict else None


def _require_rebuild_runtime_generation(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 83
        or not value.startswith("runtime_generation_")
        or any(character not in "0123456789abcdef" for character in value[19:])
    ):
        raise ImageAnalysisPersistenceError(
            "image_projection_rebuild_runtime_generation_invalid"
        )
    return value


def _require_rebuild_nonce(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 200
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in {".", "_", "-"})
            )
            for character in value
        )
    ):
        raise ImageAnalysisPersistenceError("image_projection_rebuild_nonce_invalid")
    return value


def _require_rebuild_digest(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ImageAnalysisPersistenceError("image_projection_rebuild_digest_invalid")
    return value


def _require_rebuild_token(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 200
        or any(
            not (
                character.isascii()
                and (character.isalnum() or character in {".", "_", "-", "/"})
            )
            for character in value
        )
    ):
        raise ImageAnalysisPersistenceError("image_projection_rebuild_token_invalid")
    return value


def _require_image_source_terminal_availability(value: object) -> str:
    if value not in {"missing", "changed", "revoked"}:
        raise ImageAnalysisPersistenceError(
            "image_source_removal_availability_invalid"
        )
    return str(value)


def _require_image_source_removal_binding(
    value: object,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != {
        "analysis_run_id",
        "revision",
        "content_sha256",
    }:
        raise ImageAnalysisPersistenceError(
            "image_source_removal_head_invalid"
        )
    analysis_run_id = value.get("analysis_run_id")
    revision = value.get("revision")
    content_sha256 = value.get("content_sha256")
    if (
        type(analysis_run_id) is not str
        or not analysis_run_id
        or type(revision) is not int
        or not 1 <= revision <= 1_800_000
        or type(content_sha256) is not str
        or len(content_sha256) != 64
        or any(character not in "0123456789abcdef" for character in content_sha256)
    ):
        raise ImageAnalysisPersistenceError(
            "image_source_removal_head_invalid"
        )
    return dict(value)


@dataclass
class ImageAnalysisPersistenceError(RuntimeError):
    """Stable fail-closed error raised by the canonical image bridge."""

    code: str

    def __str__(self) -> str:
        return self.code


@dataclass
class ExactImageSource:
    """A content-verified source descriptor pinned to its admitted inode."""

    binding: dict[str, object]
    handle: BinaryIO

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> ExactImageSource:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class ImageAnalysisPersistenceMixin:
    """Add durable canonical image methods to ``MediaRepository``.

    The host class supplies ``db_path``, ``transaction()``, ``_managed_connection()``,
    ``open_library_root_fd()``, ``_open_relative_regular()`` and ``get_media_job()``.
    """

    db_path: Path

    def _image_database_file_identity(self) -> tuple[int, int]:
        try:
            observed = os.stat(self.db_path, follow_symlinks=False)
        except OSError as exc:
            raise ImageAnalysisPersistenceError("image_database_identity_invalid") from exc
        if not stat.S_ISREG(observed.st_mode):
            raise ImageAnalysisPersistenceError("image_database_identity_invalid")
        return int(observed.st_dev), int(observed.st_ino)

    def _admit_image_database_identity(
        self,
        expected: tuple[int, int] | None = None,
    ) -> tuple[int, int]:
        current = self._image_database_file_identity()
        if expected is not None and current != expected:
            raise ImageAnalysisPersistenceError("image_database_scope_changed")
        bound = getattr(self, "_image_bound_database_identity", None)
        poisoned = bool(getattr(self, "_image_database_binding_failed", False))
        if poisoned:
            raise ImageAnalysisPersistenceError("image_database_scope_changed")
        if bound is None:
            setattr(self, "_image_bound_database_identity", current)
        elif bound != current:
            setattr(self, "_image_database_binding_failed", True)
            raise ImageAnalysisPersistenceError("image_database_scope_changed")
        return current

    def bind_image_database_identity(self) -> tuple[int, int]:
        """Bind this repository generation to the current managed DB inode."""

        return self._admit_image_database_identity()

    @staticmethod
    def _image_meta_in_transaction(connection: sqlite3.Connection) -> dict[str, object]:
        row = connection.execute("SELECT database_uuid,schema_version FROM database_meta WHERE singleton=1").fetchone()
        if row is None or int(row["schema_version"]) < 14:
            raise ImageAnalysisPersistenceError("image_database_identity_invalid")
        return {
            "database_uuid": str(row["database_uuid"]),
            "schema_version": int(row["schema_version"]),
        }

    def _open_admitted_image_source(
        self,
        *,
        source_id: str,
        expected_asset_id: str | None = None,
        expected_input_sha256: str | None = None,
    ) -> ExactImageSource:
        with self._managed_connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """SELECT s.id,s.asset_id,s.library_root_id,s.relative_path,
                          s.observed_size,s.observed_mtime_ns,s.source_file_id,
                          s.availability,r.permission_fingerprint,r.status AS root_status,
                          a.kind,a.sha256,a.file_size
                     FROM asset_sources s
                     JOIN library_roots r ON r.id=s.library_root_id
                     JOIN assets a ON a.id=s.asset_id
                    WHERE s.id=?""",
                (source_id,),
            ).fetchone()
        if (
            row is None
            or row["kind"] != "image"
            or row["availability"] != "available"
            or row["root_status"] != "active"
            or (expected_asset_id is not None and row["asset_id"] != expected_asset_id)
            or (expected_input_sha256 is not None and row["sha256"] != expected_input_sha256)
        ):
            raise ImageAnalysisPersistenceError("image_source_unavailable")

        root_fd: int | None = None
        file_fd: int | None = None
        try:
            _, root_fd = self.open_library_root_fd(str(row["library_root_id"]))  # type: ignore[attr-defined]
            file_fd = self._open_relative_regular(  # type: ignore[attr-defined]
                root_fd,
                str(row["relative_path"]),
            )
            observed = os.fstat(file_fd)
            if (
                not stat.S_ISREG(observed.st_mode)
                or observed.st_size != int(row["observed_size"])
                or observed.st_size != int(row["file_size"])
                or observed.st_mtime_ns != int(row["observed_mtime_ns"])
                or (row["source_file_id"] is not None and str(observed.st_ino) != str(row["source_file_id"]))
            ):
                raise ImageAnalysisPersistenceError("image_source_changed")
            identity_material = (
                f"{observed.st_dev}\0{observed.st_ino}\0{observed.st_size}\0"
                f"{observed.st_mtime_ns}\0{observed.st_ctime_ns}"
            )
            file_identity_sha256 = hashlib.sha256(identity_material.encode()).hexdigest()
            digest = hashlib.sha256()
            offset = 0
            while chunk := os.pread(file_fd, 1024 * 1024, offset):
                digest.update(chunk)
                offset += len(chunk)
            after = os.fstat(file_fd)
            if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
            ) or digest.hexdigest() != str(row["sha256"]):
                raise ImageAnalysisPersistenceError("image_source_changed")
            os.lseek(file_fd, 0, os.SEEK_SET)
            handle = os.fdopen(file_fd, "rb")
            file_fd = None
            return ExactImageSource(
                binding={
                    "source_id": str(row["id"]),
                    "library_root_id": str(row["library_root_id"]),
                    "relative_path": str(row["relative_path"]),
                    "observed_size": int(observed.st_size),
                    "observed_mtime_ns": int(observed.st_mtime_ns),
                    "observed_ctime_ns": int(observed.st_ctime_ns),
                    "source_device": int(observed.st_dev),
                    "source_inode": int(observed.st_ino),
                    "file_identity_sha256": file_identity_sha256,
                    "root_permission_fingerprint": str(row["permission_fingerprint"]),
                    "asset_id": str(row["asset_id"]),
                    "input_asset_sha256": str(row["sha256"]),
                },
                handle=handle,
            )
        except ImageAnalysisPersistenceError:
            raise
        except (OSError, ValueError) as exc:
            raise ImageAnalysisPersistenceError("image_source_changed") from exc
        finally:
            if file_fd is not None:
                os.close(file_fd)
            if root_fd is not None:
                os.close(root_fd)

    @staticmethod
    def _expected_image_head_in_transaction(
        connection: sqlite3.Connection,
        asset_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """SELECT analysis_run_id,revision,content_sha256
                 FROM image_analysis_heads WHERE asset_id=?""",
            (asset_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _verified_latest_image_projection_change_in_transaction(
        connection: sqlite3.Connection,
        *,
        asset_id: str,
        at_or_before: int | None = None,
    ) -> dict[str, object] | None:
        high_water_predicate = "" if at_or_before is None else "AND position<=?"
        parameters: tuple[object, ...] = (
            (asset_id,)
            if at_or_before is None
            else (asset_id, at_or_before)
        )
        row = connection.execute(
            f"""SELECT position,change_id,database_uuid,projection_contract,
                       asset_id,analysis_run_id,revision,content_sha256,
                       source_id,source_binding_sha256,operation,change_json,
                       change_sha256,created_at
                  FROM image_projection_changes
                 WHERE asset_id=? {high_water_predicate}
                 ORDER BY position DESC LIMIT 1""",
            parameters,
        ).fetchone()
        if row is None:
            return None
        change = _parse_json(row["change_json"])
        if change is None:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        try:
            require_valid_projection_change(change)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        analysis_binding = change.get("analysis_binding")
        if (
            type(analysis_binding) is not dict
            or change.get("change_position") != row["position"]
            or change.get("projection_contract") != row["projection_contract"]
            or change.get("database_uuid") != row["database_uuid"]
            or change.get("asset_id") != row["asset_id"]
            or analysis_binding.get("analysis_run_id") != row["analysis_run_id"]
            or analysis_binding.get("revision") != row["revision"]
            or analysis_binding.get("content_sha256") != row["content_sha256"]
            or change.get("source_binding_sha256")
            != row["source_binding_sha256"]
            or change.get("operation") != row["operation"]
            or change.get("change_sha256") != row["change_sha256"]
            or row["change_id"]
            != f"ichange_{str(row['change_sha256'])[:24]}"
            or type(row["created_at"]) is not str
            or not row["created_at"]
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        return {**dict(row), "change": change}

    @staticmethod
    def _require_bound_image_source_in_transaction(
        connection: sqlite3.Connection,
        binding: Mapping[str, object],
    ) -> None:
        """Re-admit the persisted root/source/asset under the caller's write lock."""

        row = connection.execute(
            """SELECT source.id,source.asset_id,source.library_root_id,
                      source.relative_path,source.observed_size,
                      source.observed_mtime_ns,source.source_file_id,
                      source.availability,root.permission_fingerprint,
                      root.status AS root_status,asset.kind,asset.sha256,
                      asset.file_size
                 FROM asset_sources source
                 JOIN library_roots root ON root.id=source.library_root_id
                 JOIN assets asset ON asset.id=source.asset_id
                WHERE source.id=?""",
            (binding.get("source_id"),),
        ).fetchone()
        observed_size = binding.get("observed_size")
        observed_mtime_ns = binding.get("observed_mtime_ns")
        source_inode = binding.get("source_inode")
        if (
            row is None
            or type(observed_size) is not int
            or type(observed_mtime_ns) is not int
            or type(source_inode) is not int
            or row["id"] != binding.get("source_id")
            or row["asset_id"] != binding.get("asset_id")
            or row["library_root_id"] != binding.get("library_root_id")
            or row["relative_path"] != binding.get("relative_path")
            or int(row["observed_size"]) != observed_size
            or int(row["observed_mtime_ns"]) != observed_mtime_ns
            or (
                row["source_file_id"] is not None
                and str(row["source_file_id"]) != str(source_inode)
            )
            or row["availability"] != "available"
            or row["root_status"] != "active"
            or row["permission_fingerprint"]
            != binding.get("root_permission_fingerprint")
            or row["kind"] != "image"
            or row["sha256"] != binding.get("input_asset_sha256")
            or int(row["file_size"]) != observed_size
        ):
            raise ImageAnalysisPersistenceError("image_source_authority_changed")

    def enqueue_image_analysis_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        runtime_generation: str,
        database_file_identity: tuple[int, int],
        source_binding: Mapping[str, object],
        asset_id: str,
        source_id: str,
        analysis_profile: Mapping[str, object],
        expected_head: Mapping[str, object] | None,
        enqueue_scope: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Create an exact-bound job inside a caller-owned write transaction.

        This method never opens, commits or rolls back a SQLite connection.  It
        exists so asset/source admission and durable image work can become one
        atomic domain write.  The caller remains responsible for holding and
        rechecking the admitted source descriptor through its outer commit.
        """

        if not connection.in_transaction:
            raise ImageAnalysisPersistenceError("image_transaction_required")
        database_identity = self._admit_image_database_identity(database_file_identity)
        meta = self._image_meta_in_transaction(connection)
        normalized_expected = dict(expected_head) if expected_head is not None else None
        profile = dict(analysis_profile)
        if set(profile) != {"profile_id", "profile_version", "profile_sha256"}:
            raise ImageAnalysisPersistenceError("invalid_image_analysis_profile")

        required_binding_fields = {
            "source_id",
            "library_root_id",
            "relative_path",
            "observed_size",
            "observed_mtime_ns",
            "observed_ctime_ns",
            "source_device",
            "source_inode",
            "file_identity_sha256",
            "root_permission_fingerprint",
            "asset_id",
            "input_asset_sha256",
        }
        binding = dict(source_binding)
        if (
            set(binding) != required_binding_fields
            or binding.get("asset_id") != asset_id
            or binding.get("source_id") != source_id
        ):
            raise ImageAnalysisPersistenceError("invalid_image_source_binding")
        self._require_bound_image_source_in_transaction(connection, binding)

        existing = connection.execute(
            """SELECT binding.job_id,binding.request_json,binding.request_sha256
                 FROM image_analysis_job_bindings binding
                WHERE binding.database_uuid=? AND binding.enqueue_scope=?
                  AND binding.idempotency_key=?""",
            (meta["database_uuid"], enqueue_scope, idempotency_key),
        ).fetchone()
        if existing is not None:
            stored_request = _parse_json(existing["request_json"])
            if stored_request is None:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            try:
                require_valid_image_analysis_request(stored_request)
            except (ImageAnalysisContractError, ValueError) as exc:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt") from exc
            if image_analysis_request_sha256(stored_request) != existing["request_sha256"]:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            intended = (
                stored_request.get("intended_publication")
                if stored_request is not None
                else None
            )
            if type(intended) is not dict:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            run_id = str(intended.get("analysis_run_id") or "")
            revision = intended.get("revision")
            if type(revision) is not int:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            job_id = str(existing["job_id"])
            run_revision = 0
            current_head = self._expected_image_head_in_transaction(connection, asset_id)
        else:
            current_head = self._expected_image_head_in_transaction(connection, asset_id)
            if current_head != normalized_expected:
                raise ImageAnalysisPersistenceError("image_analysis_head_conflict")
            revision = 1 if current_head is None else int(current_head["revision"]) + 1
            run_revision_row = connection.execute(
                """SELECT COALESCE(MAX(revision),0)+1 AS revision
                     FROM analysis_runs WHERE asset_id=?""",
                (asset_id,),
            ).fetchone()
            if run_revision_row is None:
                raise ImageAnalysisPersistenceError("image_analysis_run_allocation_failed")
            run_revision = int(run_revision_row["revision"])
            run_id = _identifier("arun")
            job_id = _identifier("job")

        request: dict[str, object] = {
            "object": "memolens.image_analysis_request",
            "schema_version": "1",
            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
            "database_binding": {
                "database_uuid": meta["database_uuid"],
                "database_device": database_identity[0],
                "database_inode": database_identity[1],
            },
            "asset_binding": {
                "asset_id": asset_id,
                "input_asset_sha256": binding["input_asset_sha256"],
            },
            "source_binding": {
                key: binding[key]
                for key in (
                    "source_id",
                    "library_root_id",
                    "relative_path",
                    "observed_size",
                    "observed_mtime_ns",
                    "observed_ctime_ns",
                    "source_device",
                    "source_inode",
                    "file_identity_sha256",
                )
            },
            "analysis_profile": profile,
            "expected_head": normalized_expected,
            "intended_publication": {
                "analysis_run_id": run_id,
                "revision": revision,
            },
            "idempotency": {"scope": enqueue_scope, "key": idempotency_key},
        }
        require_valid_image_analysis_request(request)
        request_sha256 = image_analysis_request_sha256(request)
        initial_checkpoint = {
            "object": "memolens.image_analysis_checkpoint",
            "schema_version": "1",
            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
            "job_id": job_id,
            "attempt": 1,
            "current_stage": "queued",
            "completed_stages": [],
            "stage_outcomes": [],
            "artifact_digests": [],
            "publish_binding": None,
        }
        require_valid_image_analysis_checkpoint(initial_checkpoint)

        if existing is not None:
            if str(existing["request_sha256"]) != request_sha256:
                raise ImageAnalysisPersistenceError("image_analysis_idempotency_conflict")
            reused = True
        else:
            reused = False
            now = _utc_now_iso()
            connection.execute(
                """INSERT INTO analysis_runs(
                       id,asset_id,revision,run_kind,parent_run_id,
                       analysis_profile_id,analysis_profile_json,
                       input_asset_sha256,status,transcript_status,
                       visual_status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,'queued','unavailable','pending',?)""",
                (
                    run_id,
                    asset_id,
                    run_revision,
                    "initial" if run_revision == 1 else "reanalyze",
                    current_head["analysis_run_id"] if current_head else None,
                    profile["profile_id"],
                    canonical_job_json(profile),
                    binding["input_asset_sha256"],
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO image_analysis_job_bindings(
                       job_id,database_uuid,database_device,database_inode,
                       asset_id,analysis_run_id,
                       intended_revision,library_root_id,
                       root_permission_fingerprint,source_id,relative_path,
                       source_device,source_inode,observed_size,observed_mtime_ns,
                       observed_ctime_ns,
                       file_identity_sha256,input_asset_sha256,
                       source_binding_sha256,analysis_profile_id,
                       analysis_profile_version,analysis_profile_json,
                       analysis_profile_sha256,expected_head_state,
                       expected_head_run_id,expected_head_revision,
                       expected_head_content_sha256,enqueue_scope,idempotency_key,
                       request_json,request_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    meta["database_uuid"],
                    database_identity[0],
                    database_identity[1],
                    asset_id,
                    run_id,
                    revision,
                    binding["library_root_id"],
                    binding["root_permission_fingerprint"],
                    source_id,
                    binding["relative_path"],
                    binding["source_device"],
                    binding["source_inode"],
                    binding["observed_size"],
                    binding["observed_mtime_ns"],
                    binding["observed_ctime_ns"],
                    binding["file_identity_sha256"],
                    binding["input_asset_sha256"],
                    source_binding_sha256(
                        {
                            "source_id": binding["source_id"],
                            "library_root_id": binding["library_root_id"],
                            "observed_size": binding["observed_size"],
                            "observed_mtime_ns": binding["observed_mtime_ns"],
                            "file_identity_sha256": binding["file_identity_sha256"],
                        }
                    ),
                    profile["profile_id"],
                    profile["profile_version"],
                    canonical_job_json(profile),
                    profile["profile_sha256"],
                    "missing" if current_head is None else "present",
                    current_head["analysis_run_id"] if current_head else None,
                    current_head["revision"] if current_head else None,
                    current_head["content_sha256"] if current_head else None,
                    enqueue_scope,
                    idempotency_key,
                    canonical_job_json(request),
                    request_sha256,
                    now,
                ),
            )
            authority = seal_image_analysis_attempt_authority(
                {
                    "object": "memolens.image_analysis_attempt_authority",
                    "schema_version": "1",
                    "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
                    "job_id": job_id,
                    "attempt": 1,
                    "runtime_generation": runtime_generation,
                    "database_binding": request["database_binding"],
                    "request_sha256": request_sha256,
                    "reason": "initial",
                    "predecessor": None,
                    "start_checkpoint_sha256": canonical_job_sha256(initial_checkpoint),
                }
            )
            connection.execute(
                """INSERT INTO image_analysis_attempt_authorities(
                       job_id,attempt,runtime_generation,database_uuid,
                       database_device,database_inode,request_sha256,reason,
                       predecessor_attempt,predecessor_status,predecessor_stage,
                       predecessor_checkpoint_sha256,
                       predecessor_authority_sha256,start_checkpoint_sha256,
                       authority_json,authority_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?,?,?)""",
                (
                    job_id,
                    1,
                    runtime_generation,
                    meta["database_uuid"],
                    database_identity[0],
                    database_identity[1],
                    request_sha256,
                    "initial",
                    authority["start_checkpoint_sha256"],
                    canonical_image_analysis_attempt_authority_json(authority),
                    authority["authority_sha256"],
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO image_analysis_attempt_states(
                       job_id,attempt,authority_sha256,state,
                       heartbeat_sequence,claimed_at,heartbeat_at,finished_at)
                   VALUES(?,?,?,'queued',0,NULL,NULL,NULL)""",
                (job_id, 1, authority["authority_sha256"]),
            )
            connection.execute(
                """INSERT INTO media_jobs(
                       id,database_uuid,kind,asset_id,analysis_run_id,status,
                       stage,progress,attempt,cancel_requested,checkpoint_json,
                       created_at)
                   VALUES(?,?,?,?,?,'queued','queued',0,1,0,?,?)""",
                (
                    job_id,
                    meta["database_uuid"],
                    IMAGE_ANALYSIS_WORKER_KIND,
                    asset_id,
                    run_id,
                    canonical_job_json(initial_checkpoint),
                    now,
                ),
            )

        self._require_bound_image_source_in_transaction(connection, binding)
        result = self.get_media_job_in_transaction(connection, job_id)  # type: ignore[attr-defined]
        if result is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        result["reused"] = reused
        return result

    def enqueue_image_analysis(
        self,
        *,
        runtime_generation: str,
        database_file_identity: tuple[int, int],
        asset_id: str,
        source_id: str,
        analysis_profile: Mapping[str, object],
        expected_head: Mapping[str, object] | None,
        enqueue_scope: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        """Create one permanently idempotent, exact-bound image analysis job."""

        database_identity = self._admit_image_database_identity(database_file_identity)
        normalized_expected = dict(expected_head) if expected_head is not None else None
        profile = dict(analysis_profile)
        if set(profile) != {"profile_id", "profile_version", "profile_sha256"}:
            raise ImageAnalysisPersistenceError("invalid_image_analysis_profile")
        with self._managed_connection() as replay_connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(replay_connection)
            replay_row = replay_connection.execute(
                """SELECT job_id,request_json,request_sha256
                     FROM image_analysis_job_bindings
                    WHERE database_uuid=? AND enqueue_scope=? AND idempotency_key=?""",
                (meta["database_uuid"], enqueue_scope, idempotency_key),
            ).fetchone()
        self._admit_image_database_identity(database_identity)
        if replay_row is not None:
            stored_request = _parse_json(replay_row["request_json"])
            if stored_request is None:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            require_valid_image_analysis_request(stored_request)
            if image_analysis_request_sha256(stored_request) != replay_row["request_sha256"]:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            stored_database = stored_request.get("database_binding")
            stored_asset = stored_request.get("asset_binding")
            stored_source = stored_request.get("source_binding")
            if (
                type(stored_database) is not dict
                or stored_database.get("database_uuid") != meta["database_uuid"]
                or stored_database.get("database_device") != database_identity[0]
                or stored_database.get("database_inode") != database_identity[1]
                or type(stored_asset) is not dict
                or stored_asset.get("asset_id") != asset_id
                or type(stored_source) is not dict
                or stored_source.get("source_id") != source_id
                or stored_request.get("analysis_profile") != profile
                or stored_request.get("expected_head") != normalized_expected
                or stored_request.get("idempotency")
                != {"scope": enqueue_scope, "key": idempotency_key}
            ):
                raise ImageAnalysisPersistenceError("image_analysis_idempotency_conflict")
            replayed = self.get_media_job(str(replay_row["job_id"]))  # type: ignore[attr-defined]
            if replayed is None:
                raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
            replayed["reused"] = True
            return replayed
        with self._open_admitted_image_source(
            source_id=source_id,
            expected_asset_id=asset_id,
        ) as pinned:
            binding = pinned.binding
            with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
                if self._admit_image_database_identity(database_identity) != database_identity:
                    raise ImageAnalysisPersistenceError("image_database_scope_changed")
                meta = self._image_meta_in_transaction(connection)
                self._require_bound_image_source_in_transaction(connection, binding)
                existing = connection.execute(
                    """SELECT binding.job_id,binding.request_json,binding.request_sha256
                         FROM image_analysis_job_bindings binding
                        WHERE binding.database_uuid=? AND binding.enqueue_scope=?
                          AND binding.idempotency_key=?""",
                    (meta["database_uuid"], enqueue_scope, idempotency_key),
                ).fetchone()
                if existing is not None:
                    stored_request = _parse_json(existing["request_json"])
                    intended = stored_request.get("intended_publication") if stored_request is not None else None
                    if type(intended) is not dict:
                        raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
                    run_id = str(intended.get("analysis_run_id") or "")
                    revision = intended.get("revision")
                    if type(revision) is not int:
                        raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
                    job_id = str(existing["job_id"])
                else:
                    current_head = self._expected_image_head_in_transaction(
                        connection,
                        asset_id,
                    )
                    if current_head != normalized_expected:
                        raise ImageAnalysisPersistenceError("image_analysis_head_conflict")
                    revision = 1 if current_head is None else int(current_head["revision"]) + 1
                    run_revision_row = connection.execute(
                        """SELECT COALESCE(MAX(revision),0)+1 AS revision
                             FROM analysis_runs WHERE asset_id=?""",
                        (asset_id,),
                    ).fetchone()
                    if run_revision_row is None:
                        raise ImageAnalysisPersistenceError("image_analysis_run_allocation_failed")
                    run_revision = int(run_revision_row["revision"])
                    run_id = _identifier("arun")
                    job_id = _identifier("job")
                request: dict[str, object] = {
                    "object": "memolens.image_analysis_request",
                    "schema_version": "1",
                    "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
                    "database_binding": {
                        "database_uuid": meta["database_uuid"],
                        "database_device": database_identity[0],
                        "database_inode": database_identity[1],
                    },
                    "asset_binding": {
                        "asset_id": asset_id,
                        "input_asset_sha256": binding["input_asset_sha256"],
                    },
                    "source_binding": {
                        key: binding[key]
                        for key in (
                            "source_id",
                            "library_root_id",
                            "relative_path",
                            "observed_size",
                            "observed_mtime_ns",
                            "observed_ctime_ns",
                            "source_device",
                            "source_inode",
                            "file_identity_sha256",
                        )
                    },
                    "analysis_profile": profile,
                    "expected_head": normalized_expected,
                    "intended_publication": {
                        "analysis_run_id": run_id,
                        "revision": revision,
                    },
                    "idempotency": {"scope": enqueue_scope, "key": idempotency_key},
                }
                require_valid_image_analysis_request(request)
                request_sha256 = image_analysis_request_sha256(request)
                initial_checkpoint = {
                    "object": "memolens.image_analysis_checkpoint",
                    "schema_version": "1",
                    "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
                    "job_id": job_id,
                    "attempt": 1,
                    "current_stage": "queued",
                    "completed_stages": [],
                    "stage_outcomes": [],
                    "artifact_digests": [],
                    "publish_binding": None,
                }
                require_valid_image_analysis_checkpoint(initial_checkpoint)
                if existing is not None:
                    if str(existing["request_sha256"]) != request_sha256:
                        raise ImageAnalysisPersistenceError("image_analysis_idempotency_conflict")
                    existing_job_id = str(existing["job_id"])
                else:
                    existing_job_id = ""
                    current_head = self._expected_image_head_in_transaction(
                        connection,
                        asset_id,
                    )
                    now = _utc_now_iso()
                    connection.execute(
                        """INSERT INTO analysis_runs(
                               id,asset_id,revision,run_kind,parent_run_id,
                               analysis_profile_id,analysis_profile_json,
                               input_asset_sha256,status,transcript_status,
                               visual_status,created_at)
                           VALUES(?,?,?,?,?,?,?,?,'queued','unavailable','pending',?)""",
                        (
                            run_id,
                            asset_id,
                            run_revision,
                            "initial" if run_revision == 1 else "reanalyze",
                            current_head["analysis_run_id"] if current_head else None,
                            profile["profile_id"],
                            canonical_job_json(profile),
                            binding["input_asset_sha256"],
                            now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO image_analysis_job_bindings(
                               job_id,database_uuid,database_device,database_inode,
                               asset_id,analysis_run_id,
                               intended_revision,library_root_id,
                               root_permission_fingerprint,source_id,relative_path,
                               source_device,source_inode,observed_size,observed_mtime_ns,
                               observed_ctime_ns,
                               file_identity_sha256,input_asset_sha256,
                               source_binding_sha256,analysis_profile_id,
                               analysis_profile_version,analysis_profile_json,
                               analysis_profile_sha256,expected_head_state,
                               expected_head_run_id,expected_head_revision,
                               expected_head_content_sha256,enqueue_scope,idempotency_key,
                               request_json,request_sha256,created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            job_id,
                            meta["database_uuid"],
                            database_identity[0],
                            database_identity[1],
                            asset_id,
                            run_id,
                            revision,
                            binding["library_root_id"],
                            binding["root_permission_fingerprint"],
                            source_id,
                            binding["relative_path"],
                            binding["source_device"],
                            binding["source_inode"],
                            binding["observed_size"],
                            binding["observed_mtime_ns"],
                            binding["observed_ctime_ns"],
                            binding["file_identity_sha256"],
                            binding["input_asset_sha256"],
                            source_binding_sha256(
                                {
                                    "source_id": binding["source_id"],
                                    "library_root_id": binding["library_root_id"],
                                    "observed_size": binding["observed_size"],
                                    "observed_mtime_ns": binding["observed_mtime_ns"],
                                    "file_identity_sha256": binding["file_identity_sha256"],
                                }
                            ),
                            profile["profile_id"],
                            profile["profile_version"],
                            canonical_job_json(profile),
                            profile["profile_sha256"],
                            "missing" if current_head is None else "present",
                            current_head["analysis_run_id"] if current_head else None,
                            current_head["revision"] if current_head else None,
                            current_head["content_sha256"] if current_head else None,
                            enqueue_scope,
                            idempotency_key,
                            canonical_job_json(request),
                            request_sha256,
                            now,
                        ),
                    )
                    authority = seal_image_analysis_attempt_authority(
                        {
                            "object": "memolens.image_analysis_attempt_authority",
                            "schema_version": "1",
                            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
                            "job_id": job_id,
                            "attempt": 1,
                            "runtime_generation": runtime_generation,
                            "database_binding": request["database_binding"],
                            "request_sha256": request_sha256,
                            "reason": "initial",
                            "predecessor": None,
                            "start_checkpoint_sha256": canonical_job_sha256(
                                initial_checkpoint
                            ),
                        }
                    )
                    connection.execute(
                        """INSERT INTO image_analysis_attempt_authorities(
                               job_id,attempt,runtime_generation,database_uuid,
                               database_device,database_inode,request_sha256,reason,
                               predecessor_attempt,predecessor_status,predecessor_stage,
                               predecessor_checkpoint_sha256,
                               predecessor_authority_sha256,start_checkpoint_sha256,
                               authority_json,authority_sha256,created_at)
                           VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,?,?,?,?)""",
                        (
                            job_id,
                            1,
                            runtime_generation,
                            meta["database_uuid"],
                            database_identity[0],
                            database_identity[1],
                            request_sha256,
                            "initial",
                            authority["start_checkpoint_sha256"],
                            canonical_image_analysis_attempt_authority_json(authority),
                            authority["authority_sha256"],
                            now,
                        ),
                    )
                    connection.execute(
                        """INSERT INTO image_analysis_attempt_states(
                               job_id,attempt,authority_sha256,state,
                               heartbeat_sequence,claimed_at,heartbeat_at,finished_at)
                           VALUES(?,?,?,'queued',0,NULL,NULL,NULL)""",
                        (job_id, 1, authority["authority_sha256"]),
                    )
                    connection.execute(
                        """INSERT INTO media_jobs(
                               id,database_uuid,kind,asset_id,analysis_run_id,status,
                               stage,progress,attempt,cancel_requested,checkpoint_json,
                               created_at)
                           VALUES(?,?,?,?,?,'queued','queued',0,1,0,?,?)""",
                        (
                            job_id,
                            meta["database_uuid"],
                            IMAGE_ANALYSIS_WORKER_KIND,
                            asset_id,
                            run_id,
                            canonical_job_json(initial_checkpoint),
                            now,
                        ),
                    )
                after = os.fstat(pinned.handle.fileno())
                if (
                    int(after.st_dev) != int(binding["source_device"])
                    or int(after.st_ino) != int(binding["source_inode"])
                    or int(after.st_size) != int(binding["observed_size"])
                    or int(after.st_mtime_ns) != int(binding["observed_mtime_ns"])
                    or int(after.st_ctime_ns) != int(binding["observed_ctime_ns"])
                    or self._admit_image_database_identity(database_identity) != database_identity
                ):
                    raise ImageAnalysisPersistenceError("image_source_changed")
                self._require_bound_image_source_in_transaction(connection, binding)
        result = self.get_media_job(existing_job_id or job_id)  # type: ignore[attr-defined]
        if result is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        result["reused"] = bool(existing_job_id)
        return result

    def get_image_analysis_job_binding(
        self,
        job_id: str,
    ) -> dict[str, object] | None:
        with self._managed_connection() as connection:  # type: ignore[attr-defined]
            row = connection.execute(
                """SELECT binding.*,job.kind AS job_kind,job.status AS job_status,
                          job.stage AS job_stage,job.attempt AS job_attempt,
                          job.cancel_requested AS job_cancel_requested,
                          job.database_uuid AS job_database_uuid,
                          job.asset_id AS job_asset_id,
                          job.analysis_run_id AS job_analysis_run_id,
                          authority.runtime_generation AS runtime_generation,
                          authority.authority_json AS attempt_authority_json,
                          authority.authority_sha256 AS attempt_authority_sha256,
                          state.state AS attempt_state,
                          state.heartbeat_sequence AS heartbeat_sequence,
                          run.revision AS run_revision,run.status AS run_status,
                          run.input_asset_sha256 AS run_input_asset_sha256
                     FROM image_analysis_job_bindings binding
                     JOIN media_jobs job ON job.id=binding.job_id
                     JOIN image_analysis_attempt_authorities authority
                       ON authority.job_id=job.id AND authority.attempt=job.attempt
                     JOIN image_analysis_attempt_states state
                       ON state.job_id=authority.job_id
                      AND state.attempt=authority.attempt
                      AND state.authority_sha256=authority.authority_sha256
                     JOIN analysis_runs run ON run.id=binding.analysis_run_id
                    WHERE binding.job_id=?""",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        value = dict(row)
        request = _parse_json(value.get("request_json"))
        if request is None:
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        require_valid_image_analysis_request(request)
        if image_analysis_request_sha256(request) != value.get("request_sha256"):
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        authority = _parse_json(value.get("attempt_authority_json"))
        if authority is None:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_authority_corrupt")
        require_valid_image_analysis_attempt_authority(authority)
        if (
            authority.get("job_id") != job_id
            or authority.get("attempt") != value.get("job_attempt")
            or authority.get("runtime_generation") != value.get("runtime_generation")
            or authority.get("request_sha256") != value.get("request_sha256")
            or authority.get("authority_sha256")
            != value.get("attempt_authority_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_attempt_authority_corrupt")
        value["request"] = request
        value["attempt_authority"] = authority
        return value

    @staticmethod
    def _require_image_job_row_matches_binding(
        binding: Mapping[str, object],
        *,
        runtime_generation: str,
        expected_attempt: int,
    ) -> None:
        if (
            binding.get("job_kind") != IMAGE_ANALYSIS_WORKER_KIND
            or binding.get("job_database_uuid") != binding.get("database_uuid")
            or binding.get("job_asset_id") != binding.get("asset_id")
            or binding.get("job_analysis_run_id") != binding.get("analysis_run_id")
            or binding.get("run_input_asset_sha256") != binding.get("input_asset_sha256")
            or binding.get("attempt_state") not in {"queued", "claimed"}
        ):
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        if binding.get("runtime_generation") != runtime_generation:
            raise ImageAnalysisPersistenceError("image_runtime_generation_changed")
        if type(expected_attempt) is not int or binding.get("job_attempt") != expected_attempt:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
        if bool(binding.get("job_cancel_requested")):
            raise ImageAnalysisPersistenceError("image_analysis_cancel_requested")

    def validate_image_analysis_job_admission(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
    ) -> dict[str, object]:
        binding = self.get_image_analysis_job_binding(job_id)
        if binding is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        self._require_image_job_row_matches_binding(
            binding,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        expected_identity = (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        )
        self._admit_image_database_identity(expected_identity)
        with self._managed_connection() as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
        if meta["database_uuid"] != binding["database_uuid"]:
            raise ImageAnalysisPersistenceError("image_database_scope_changed")
        with self._open_admitted_image_source(
            source_id=str(binding["source_id"]),
            expected_asset_id=str(binding["asset_id"]),
            expected_input_sha256=str(binding["input_asset_sha256"]),
        ) as exact:
            observed = exact.binding
            for key in (
                "source_id",
                "library_root_id",
                "relative_path",
                "source_device",
                "source_inode",
                "observed_size",
                "observed_mtime_ns",
                "observed_ctime_ns",
                "file_identity_sha256",
                "root_permission_fingerprint",
            ):
                if observed.get(key) != binding.get(key):
                    raise ImageAnalysisPersistenceError("image_source_changed")
        return binding

    @staticmethod
    def _image_attempt_checkpoint(
        *,
        job_id: str,
        attempt: int,
        current_stage: str = "queued",
    ) -> dict[str, object]:
        checkpoint = {
            "object": "memolens.image_analysis_checkpoint",
            "schema_version": "1",
            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
            "job_id": job_id,
            "attempt": attempt,
            "current_stage": current_stage,
            "completed_stages": [],
            "stage_outcomes": [],
            "artifact_digests": [],
            "publish_binding": None,
        }
        require_valid_image_analysis_checkpoint(checkpoint)
        return checkpoint

    @staticmethod
    def _image_attempt_error(
        *,
        code: str,
        stage: str,
        retryable: bool,
        detail: str,
    ) -> dict[str, object]:
        error = {
            "object": "memolens.image_analysis_error",
            "schema_version": "1",
            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
            "code": code,
            "stage": stage,
            "retryable": retryable,
            "detail_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        }
        require_valid_image_analysis_error(error)
        return error

    def claim_image_analysis_attempt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
    ) -> dict[str, object]:
        """CAS one queued authority into a one-generation worker claim."""

        binding = self.validate_image_analysis_job_admission(
            job_id,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        checkpoint = self._image_attempt_checkpoint(
            job_id=job_id,
            attempt=expected_attempt,
            current_stage="source_admission",
        )
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            authority_update = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET state='claimed',heartbeat_sequence=heartbeat_sequence+1,
                          claimed_at=COALESCE(claimed_at,?),heartbeat_at=?
                    WHERE job_id=? AND attempt=? AND state='queued'
                      AND authority_sha256=(
                        SELECT authority_sha256
                          FROM image_analysis_attempt_authorities
                         WHERE job_id=? AND attempt=?
                           AND runtime_generation=?)""",
                (
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    job_id,
                    expected_attempt,
                    runtime_generation,
                ),
            )
            if authority_update.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_attempt_claim_conflict")
            job_update = connection.execute(
                """UPDATE media_jobs
                      SET status='running',stage='source_admission',progress=0.02,
                          checkpoint_json=?,started_at=COALESCE(started_at,?),
                          heartbeat_at=?,error_json=NULL,finished_at=NULL
                    WHERE id=? AND attempt=? AND status='queued'
                      AND cancel_requested=0""",
                (
                    canonical_job_json(checkpoint),
                    now,
                    now,
                    job_id,
                    expected_attempt,
                ),
            )
            if job_update.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_attempt_claim_conflict")
            published = connection.execute(
                "SELECT 1 FROM image_analysis_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if published is None:
                connection.execute(
                    """UPDATE analysis_runs SET status='running',
                              started_at=COALESCE(started_at,?),error_json=NULL,
                              finished_at=NULL
                        WHERE id=? AND status IN ('queued','interrupted','failed')""",
                    (now, binding["analysis_run_id"]),
                )
        claimed = self.get_image_analysis_job_binding(job_id)
        if claimed is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        return claimed

    def heartbeat_image_analysis_attempt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        expected_sequence: int,
    ) -> int:
        """Advance one claimed attempt heartbeat with an exact sequence CAS."""

        if type(expected_sequence) is not int or expected_sequence < 0:
            raise ImageAnalysisPersistenceError("image_analysis_heartbeat_invalid")
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            updated = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET heartbeat_sequence=heartbeat_sequence+1,heartbeat_at=?
                    WHERE job_id=? AND attempt=? AND state='claimed'
                      AND heartbeat_sequence=? AND authority_sha256=(
                        SELECT authority_sha256
                          FROM image_analysis_attempt_authorities
                         WHERE job_id=? AND attempt=?
                           AND runtime_generation=?)""",
                (
                    now,
                    job_id,
                    expected_attempt,
                    expected_sequence,
                    job_id,
                    expected_attempt,
                    runtime_generation,
                ),
            )
            if updated.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_heartbeat_conflict")
            connection.execute(
                """UPDATE media_jobs SET heartbeat_at=?
                    WHERE id=? AND attempt=? AND status='running'""",
                (now, job_id, expected_attempt),
            )
        return expected_sequence + 1

    def checkpoint_image_analysis_stage(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        expected_sequence: int,
        stage: str,
        outcome: str,
        outcome_sha256: str,
        reason_code: str | None,
        artifact_digests: Sequence[Mapping[str, object]] = (),
    ) -> int:
        """Atomically seal one completed work stage and advance its heartbeat.

        A checkpoint is deliberately not an artifact cache.  It records only
        the closed outcome digest and artifact digests needed to prove what a
        later publication was built from.  A resumed attempt starts over and
        binds this checkpoint by hash through its append-only predecessor
        authority; it never treats these digests as reusable stage payloads.
        """

        if (
            type(expected_sequence) is not int
            or expected_sequence < 1
            or type(stage) is not str
            or stage not in IMAGE_ANALYSIS_WORK_STAGES[:6]
            or type(outcome) is not str
            or type(outcome_sha256) is not str
            or reason_code is not None
            and type(reason_code) is not str
            or isinstance(artifact_digests, (str, bytes, bytearray))
        ):
            raise ImageAnalysisPersistenceError("image_analysis_checkpoint_invalid")
        normalized_artifacts = [dict(item) for item in artifact_digests]
        if any(
            set(item) != {"stage", "name", "sha256"}
            or item.get("stage") != stage
            for item in normalized_artifacts
        ):
            raise ImageAnalysisPersistenceError("image_analysis_checkpoint_invalid")
        normalized_artifacts.sort(
            key=lambda item: (str(item["stage"]), str(item["name"]))
        )
        expected_database_identity = self._admit_image_database_identity()
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            current = connection.execute(
                """SELECT binding.database_uuid,binding.database_device,
                          binding.database_inode,binding.analysis_run_id,
                          job.kind AS job_kind,job.database_uuid AS job_database_uuid,
                          job.status AS job_status,job.stage AS job_stage,
                          job.attempt AS job_attempt,
                          job.cancel_requested AS job_cancel_requested,
                          job.checkpoint_json,
                          authority.runtime_generation,
                          authority.authority_sha256 AS attempt_authority_sha256,
                          state.state AS attempt_state,
                          state.heartbeat_sequence
                     FROM image_analysis_job_bindings binding
                     JOIN media_jobs job ON job.id=binding.job_id
                     JOIN image_analysis_attempt_authorities authority
                       ON authority.job_id=job.id AND authority.attempt=job.attempt
                     JOIN image_analysis_attempt_states state
                       ON state.job_id=authority.job_id
                      AND state.attempt=authority.attempt
                      AND state.authority_sha256=authority.authority_sha256
                    WHERE binding.job_id=?""",
                (job_id,),
            ).fetchone()
            if current is None:
                raise ImageAnalysisPersistenceError("image_analysis_job_missing")
            row = dict(current)
            database_identity = (
                int(row["database_device"]),
                int(row["database_inode"]),
            )
            if (
                row["database_uuid"]
                != self._image_meta_in_transaction(connection)["database_uuid"]
                or row["job_database_uuid"] != row["database_uuid"]
                or database_identity != expected_database_identity
            ):
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
            if row["runtime_generation"] != runtime_generation:
                raise ImageAnalysisPersistenceError(
                    "image_runtime_generation_changed"
                )
            if row["job_attempt"] != expected_attempt:
                raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
            if bool(row["job_cancel_requested"]):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_cancel_requested"
                )
            if (
                row["job_kind"] != IMAGE_ANALYSIS_WORKER_KIND
                or row["job_status"] != "running"
                or row["job_stage"] != stage
                or row["attempt_state"] != "claimed"
                or row["heartbeat_sequence"] != expected_sequence
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_conflict"
                )
            checkpoint = _parse_json(row["checkpoint_json"])
            if checkpoint is None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_corrupt"
                )
            try:
                require_valid_image_analysis_checkpoint(checkpoint)
            except ImageAnalysisJobContractError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_corrupt"
                ) from exc
            completed = checkpoint.get("completed_stages")
            stage_outcomes = checkpoint.get("stage_outcomes")
            persisted_artifacts = checkpoint.get("artifact_digests")
            if (
                checkpoint.get("job_id") != job_id
                or checkpoint.get("attempt") != expected_attempt
                or checkpoint.get("current_stage") != stage
                or type(completed) is not list
                or type(stage_outcomes) is not list
                or type(persisted_artifacts) is not list
                or completed != list(
                    IMAGE_ANALYSIS_WORK_STAGES[: IMAGE_ANALYSIS_WORK_STAGES.index(stage)]
                )
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_conflict"
                )
            next_stage = IMAGE_ANALYSIS_WORK_STAGES[
                IMAGE_ANALYSIS_WORK_STAGES.index(stage) + 1
            ]
            checkpoint["current_stage"] = next_stage
            checkpoint["completed_stages"] = [*completed, stage]
            checkpoint["stage_outcomes"] = [
                *stage_outcomes,
                {
                    "stage": stage,
                    "outcome": outcome,
                    "outcome_sha256": outcome_sha256,
                    "reason_code": reason_code,
                },
            ]
            checkpoint["artifact_digests"] = sorted(
                [*persisted_artifacts, *normalized_artifacts],
                key=lambda item: (str(item["stage"]), str(item["name"])),
            )
            try:
                require_valid_image_analysis_checkpoint(checkpoint)
            except ImageAnalysisJobContractError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_invalid"
                ) from exc
            state_update = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET heartbeat_sequence=heartbeat_sequence+1,heartbeat_at=?
                    WHERE job_id=? AND attempt=? AND state='claimed'
                      AND heartbeat_sequence=? AND authority_sha256=?
                      AND EXISTS(
                        SELECT 1 FROM image_analysis_attempt_authorities authority
                         WHERE authority.job_id=? AND authority.attempt=?
                           AND authority.authority_sha256=?
                           AND authority.runtime_generation=?)""",
                (
                    now,
                    job_id,
                    expected_attempt,
                    expected_sequence,
                    row["attempt_authority_sha256"],
                    job_id,
                    expected_attempt,
                    row["attempt_authority_sha256"],
                    runtime_generation,
                ),
            )
            if state_update.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_conflict"
                )
            progress = {
                "source_admission": 0.1,
                "metadata": 0.25,
                "geocode": 0.35,
                "vision": 0.45,
                "embedding": 0.65,
                "quality": 0.8,
            }[stage]
            job_update = connection.execute(
                """UPDATE media_jobs
                      SET stage=?,progress=?,checkpoint_json=?,heartbeat_at=?
                    WHERE id=? AND kind='image_analysis' AND attempt=?
                      AND status='running' AND stage=? AND cancel_requested=0""",
                (
                    next_stage,
                    progress,
                    canonical_job_json(checkpoint),
                    now,
                    job_id,
                    expected_attempt,
                    stage,
                ),
            )
            if job_update.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_conflict"
                )
            self._admit_image_database_identity(database_identity)
        return expected_sequence + 1

    def interrupt_image_analysis_attempt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        detail: str = "runtime_retired",
    ) -> bool:
        """Revoke one attempt without mutating an already published Result/run."""

        binding = self.get_image_analysis_job_binding(job_id)
        if binding is None:
            return False
        if binding.get("runtime_generation") != runtime_generation:
            raise ImageAnalysisPersistenceError("image_runtime_generation_changed")
        if type(expected_attempt) is not int or binding.get("job_attempt") != expected_attempt:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
        if (
            binding.get("job_status") == "failed"
            and binding.get("attempt_state") == "finished"
        ):
            return False
        self._require_image_job_row_matches_binding(
            binding,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        stage = str(binding["job_stage"])
        error = self._image_attempt_error(
            code="worker_interrupted",
            stage=stage,
            retryable=True,
            detail=detail,
        )
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            state_update = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET state='interrupted',heartbeat_at=?,finished_at=?
                    WHERE job_id=? AND attempt=? AND state IN ('queued','claimed')
                      AND authority_sha256=?""",
                (
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    binding["attempt_authority_sha256"],
                ),
            )
            if state_update.rowcount != 1:
                return False
            job_update = connection.execute(
                """UPDATE media_jobs SET status='interrupted',error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND attempt=?
                      AND status IN ('queued','running','cancelling')""",
                (
                    canonical_job_json(error),
                    now,
                    now,
                    job_id,
                    expected_attempt,
                ),
            )
            if job_update.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_attempt_interrupt_conflict")
            published = connection.execute(
                "SELECT 1 FROM image_analysis_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if published is None:
                connection.execute(
                    """UPDATE analysis_runs SET status='interrupted',error_json=?,
                              finished_at=? WHERE id=?
                           AND status IN ('queued','running','cancelling')""",
                    (
                        canonical_job_json(error),
                        now,
                        binding["analysis_run_id"],
                    ),
                )
        return True

    def fail_image_analysis_attempt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        code: str,
        stage: str,
        retryable: bool,
        detail: str,
    ) -> bool:
        """Fail one exact current attempt without granting generic job mutation."""

        if type(retryable) is not bool or type(detail) is not str:
            raise ImageAnalysisPersistenceError("image_analysis_failure_invalid")
        binding = self.get_image_analysis_job_binding(job_id)
        if binding is None:
            return False
        if binding.get("runtime_generation") != runtime_generation:
            raise ImageAnalysisPersistenceError("image_runtime_generation_changed")
        if type(expected_attempt) is not int or binding.get("job_attempt") != expected_attempt:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
        if (
            binding.get("job_status") == "failed"
            and binding.get("attempt_state") == "finished"
        ):
            return False
        self._require_image_job_row_matches_binding(
            binding,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        if binding.get("job_status") not in {"queued", "running"}:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_failure_conflict")
        if stage != binding.get("job_stage"):
            raise ImageAnalysisPersistenceError("image_analysis_failure_stage_changed")
        error = self._image_attempt_error(
            code=code,
            stage=stage,
            retryable=retryable,
            detail=detail,
        )
        expected_database_identity = (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        )
        self._admit_image_database_identity(expected_database_identity)
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            if meta["database_uuid"] != binding["database_uuid"]:
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
            state_update = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET state='finished',heartbeat_at=?,finished_at=?
                    WHERE job_id=? AND attempt=? AND state IN ('queued','claimed')
                      AND authority_sha256=? AND EXISTS(
                        SELECT 1 FROM image_analysis_attempt_authorities authority
                         WHERE authority.job_id=? AND authority.attempt=?
                           AND authority.authority_sha256=?
                           AND authority.runtime_generation=?)""",
                (
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    binding["attempt_authority_sha256"],
                    job_id,
                    expected_attempt,
                    binding["attempt_authority_sha256"],
                    runtime_generation,
                ),
            )
            if state_update.rowcount != 1:
                return False
            job_update = connection.execute(
                """UPDATE media_jobs SET status='failed',error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='image_analysis' AND attempt=?
                      AND status IN ('queued','running') AND stage=?
                      AND cancel_requested=0""",
                (
                    canonical_job_json(error),
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    stage,
                ),
            )
            if job_update.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_attempt_failure_conflict"
                )
            published = connection.execute(
                "SELECT 1 FROM image_analysis_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if published is None:
                run_update = connection.execute(
                    """UPDATE analysis_runs SET status='failed',error_json=?,finished_at=?
                        WHERE id=? AND status IN ('queued','running','cancelling')""",
                    (
                        canonical_job_json(error),
                        now,
                        binding["analysis_run_id"],
                    ),
                )
                if run_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_attempt_failure_conflict"
                    )
            self._admit_image_database_identity(expected_database_identity)
        return True

    def cancel_image_analysis_attempt(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        expected_attempt: int,
        detail: str = "user_cancel_requested",
    ) -> bool:
        """Finish a requested cancellation under the exact attempt authority."""

        if type(detail) is not str:
            raise ImageAnalysisPersistenceError("image_analysis_cancellation_invalid")
        binding = self.get_image_analysis_job_binding(job_id)
        if binding is None:
            return False
        if (
            binding.get("job_kind") != IMAGE_ANALYSIS_WORKER_KIND
            or binding.get("job_database_uuid") != binding.get("database_uuid")
            or binding.get("job_asset_id") != binding.get("asset_id")
            or binding.get("job_analysis_run_id") != binding.get("analysis_run_id")
            or binding.get("run_input_asset_sha256")
            != binding.get("input_asset_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        if binding.get("runtime_generation") != runtime_generation:
            raise ImageAnalysisPersistenceError("image_runtime_generation_changed")
        if type(expected_attempt) is not int or binding.get("job_attempt") != expected_attempt:
            raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
        if not bool(binding.get("job_cancel_requested")):
            raise ImageAnalysisPersistenceError("image_analysis_cancel_not_requested")
        if binding.get("job_status") == "cancelled":
            return False
        if (
            binding.get("job_status") != "cancelling"
            or binding.get("attempt_state") != "claimed"
        ):
            raise ImageAnalysisPersistenceError("image_analysis_attempt_cancel_conflict")
        stage = str(binding["job_stage"])
        error = self._image_attempt_error(
            code="cancelled",
            stage=stage,
            retryable=False,
            detail=detail,
        )
        expected_database_identity = (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        )
        self._admit_image_database_identity(expected_database_identity)
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            if meta["database_uuid"] != binding["database_uuid"]:
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
            state_update = connection.execute(
                """UPDATE image_analysis_attempt_states
                      SET state='cancelled',heartbeat_at=?,finished_at=?
                    WHERE job_id=? AND attempt=? AND state='claimed'
                      AND authority_sha256=? AND EXISTS(
                        SELECT 1 FROM image_analysis_attempt_authorities authority
                         WHERE authority.job_id=? AND authority.attempt=?
                           AND authority.authority_sha256=?
                           AND authority.runtime_generation=?)""",
                (
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    binding["attempt_authority_sha256"],
                    job_id,
                    expected_attempt,
                    binding["attempt_authority_sha256"],
                    runtime_generation,
                ),
            )
            if state_update.rowcount != 1:
                return False
            job_update = connection.execute(
                """UPDATE media_jobs SET status='cancelled',error_json=?,
                          heartbeat_at=?,finished_at=?
                    WHERE id=? AND kind='image_analysis' AND attempt=?
                      AND status='cancelling' AND stage=? AND cancel_requested=1""",
                (
                    canonical_job_json(error),
                    now,
                    now,
                    job_id,
                    expected_attempt,
                    stage,
                ),
            )
            if job_update.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_attempt_cancel_conflict"
                )
            published = connection.execute(
                "SELECT 1 FROM image_analysis_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if published is None:
                run_update = connection.execute(
                    """UPDATE analysis_runs SET status='cancelled',error_json=?,finished_at=?
                        WHERE id=? AND status='cancelling'""",
                    (
                        canonical_job_json(error),
                        now,
                        binding["analysis_run_id"],
                    ),
                )
                if run_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_attempt_cancel_conflict"
                    )
            self._admit_image_database_identity(expected_database_identity)
        return True

    def resume_image_analysis_job(
        self,
        job_id: str,
        *,
        runtime_generation: str,
        database_file_identity: tuple[int, int],
        reason: str = "explicit_resume",
    ) -> dict[str, object]:
        """Append exactly one new generation-bound attempt and fresh checkpoint."""

        database_identity = self._admit_image_database_identity(database_file_identity)
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            current = connection.execute(
                """SELECT binding.*,job.status AS job_status,job.stage AS job_stage,
                          job.attempt AS job_attempt,job.checkpoint_json,
                          job.error_json,job.cancel_requested,
                          authority.authority_json AS prior_authority_json,
                          authority.authority_sha256 AS prior_authority_sha256,
                          state.state AS prior_attempt_state
                     FROM image_analysis_job_bindings binding
                     JOIN media_jobs job ON job.id=binding.job_id
                     JOIN image_analysis_attempt_authorities authority
                       ON authority.job_id=job.id AND authority.attempt=job.attempt
                     JOIN image_analysis_attempt_states state
                       ON state.job_id=authority.job_id
                      AND state.attempt=authority.attempt
                      AND state.authority_sha256=authority.authority_sha256
                    WHERE binding.job_id=?""",
                (job_id,),
            ).fetchone()
            if current is None:
                raise ImageAnalysisPersistenceError("image_analysis_job_missing")
            binding = dict(current)
            if (
                binding["database_uuid"]
                != self._image_meta_in_transaction(connection)["database_uuid"]
                or (
                    int(binding["database_device"]),
                    int(binding["database_inode"]),
                )
                != database_identity
            ):
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
            if binding["job_status"] not in {"interrupted", "failed"}:
                raise ImageAnalysisPersistenceError("image_analysis_job_not_resumable")
            if bool(binding["cancel_requested"]):
                raise ImageAnalysisPersistenceError("image_analysis_job_not_resumable")
            if binding["prior_attempt_state"] not in {
                "interrupted",
                "claimed",
                "queued",
                "finished",
            }:
                raise ImageAnalysisPersistenceError("image_analysis_job_not_resumable")
            prior_error = _parse_json(binding["error_json"])
            if prior_error is None or prior_error.get("retryable") is not True:
                raise ImageAnalysisPersistenceError("image_analysis_job_not_resumable")
            if int(binding["job_attempt"]) >= IMAGE_ANALYSIS_MAX_ATTEMPTS:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_retry_budget_exhausted"
                )
            prior_checkpoint = _parse_json(binding["checkpoint_json"])
            if prior_checkpoint is None:
                raise ImageAnalysisPersistenceError("image_analysis_checkpoint_corrupt")
            prior_authority = _parse_json(binding["prior_authority_json"])
            if prior_authority is None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_attempt_authority_corrupt"
                )
            try:
                require_valid_image_analysis_checkpoint(prior_checkpoint)
            except ImageAnalysisJobContractError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_checkpoint_corrupt"
                ) from exc
            try:
                require_valid_image_analysis_attempt_authority(prior_authority)
            except ImageAnalysisJobContractError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_attempt_authority_corrupt"
                ) from exc
            if (
                prior_authority.get("job_id") != job_id
                or prior_authority.get("attempt") != binding["job_attempt"]
                or prior_authority.get("request_sha256")
                != binding["request_sha256"]
                or prior_authority.get("authority_sha256")
                != binding["prior_authority_sha256"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_attempt_authority_corrupt"
                )
            next_attempt = int(binding["job_attempt"]) + 1
            start_checkpoint = self._image_attempt_checkpoint(
                job_id=job_id,
                attempt=next_attempt,
            )
            predecessor = {
                "attempt": int(binding["job_attempt"]),
                "status": str(binding["job_status"]),
                "stage": str(binding["job_stage"]),
                "checkpoint_sha256": canonical_job_sha256(prior_checkpoint),
                "authority_sha256": str(binding["prior_authority_sha256"]),
            }
            authority = seal_image_analysis_attempt_authority(
                {
                    "object": "memolens.image_analysis_attempt_authority",
                    "schema_version": "1",
                    "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
                    "job_id": job_id,
                    "attempt": next_attempt,
                    "runtime_generation": runtime_generation,
                    "database_binding": {
                        "database_uuid": binding["database_uuid"],
                        "database_device": int(binding["database_device"]),
                        "database_inode": int(binding["database_inode"]),
                    },
                    "request_sha256": binding["request_sha256"],
                    "reason": reason,
                    "predecessor": predecessor,
                    "start_checkpoint_sha256": canonical_job_sha256(
                        start_checkpoint
                    ),
                }
            )
            if binding["prior_attempt_state"] in {"queued", "claimed"}:
                interrupted = connection.execute(
                    """UPDATE image_analysis_attempt_states
                          SET state='interrupted',heartbeat_at=?,finished_at=?
                        WHERE job_id=? AND attempt=?
                          AND state IN ('queued','claimed')""",
                    (now, now, job_id, binding["job_attempt"]),
                )
                if interrupted.rowcount != 1:
                    raise ImageAnalysisPersistenceError("image_analysis_resume_conflict")
            connection.execute(
                """INSERT INTO image_analysis_attempt_authorities(
                       job_id,attempt,runtime_generation,database_uuid,
                       database_device,database_inode,request_sha256,reason,
                       predecessor_attempt,predecessor_status,predecessor_stage,
                       predecessor_checkpoint_sha256,predecessor_authority_sha256,
                       start_checkpoint_sha256,authority_json,authority_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    next_attempt,
                    runtime_generation,
                    binding["database_uuid"],
                    binding["database_device"],
                    binding["database_inode"],
                    binding["request_sha256"],
                    reason,
                    predecessor["attempt"],
                    predecessor["status"],
                    predecessor["stage"],
                    predecessor["checkpoint_sha256"],
                    predecessor["authority_sha256"],
                    authority["start_checkpoint_sha256"],
                    canonical_image_analysis_attempt_authority_json(authority),
                    authority["authority_sha256"],
                    now,
                ),
            )
            connection.execute(
                """INSERT INTO image_analysis_attempt_states(
                       job_id,attempt,authority_sha256,state,heartbeat_sequence,
                       claimed_at,heartbeat_at,finished_at)
                   VALUES(?,?,?,'queued',0,NULL,NULL,NULL)""",
                (job_id, next_attempt, authority["authority_sha256"]),
            )
            updated = connection.execute(
                """UPDATE media_jobs SET status='queued',stage='queued',progress=0,
                          attempt=?,checkpoint_json=?,error_json=NULL,
                          started_at=NULL,heartbeat_at=NULL,finished_at=NULL
                    WHERE id=? AND attempt=? AND status IN ('interrupted','failed')""",
                (
                    next_attempt,
                    canonical_job_json(start_checkpoint),
                    job_id,
                    predecessor["attempt"],
                ),
            )
            if updated.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_resume_conflict")
            published = connection.execute(
                "SELECT 1 FROM image_analysis_results WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if published is None:
                connection.execute(
                    """UPDATE analysis_runs SET status='queued',error_json=NULL,
                              started_at=NULL,finished_at=NULL
                        WHERE id=? AND status IN ('interrupted','failed')""",
                    (binding["analysis_run_id"],),
                )
        resumed = self.get_media_job(job_id)  # type: ignore[attr-defined]
        if resumed is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        resumed["attempt_authority"] = authority
        return resumed

    @staticmethod
    def _image_result_matches_binding(
        result: Mapping[str, object],
        binding: Mapping[str, object],
    ) -> bool:
        source = result.get("source_binding")
        profile = result.get("analysis_profile")
        if type(source) is not dict or type(profile) is not dict:
            return False
        return (
            result.get("asset_id") == binding.get("asset_id")
            and result.get("asset_sha256") == binding.get("input_asset_sha256")
            and result.get("analysis_run_id") == binding.get("analysis_run_id")
            and result.get("revision") == binding.get("intended_revision")
            and source.get("source_id") == binding.get("source_id")
            and source.get("library_root_id") == binding.get("library_root_id")
            and source.get("observed_size") == binding.get("observed_size")
            and source.get("observed_mtime_ns") == binding.get("observed_mtime_ns")
            and source.get("file_identity_sha256") == binding.get("file_identity_sha256")
            and profile.get("id") == binding.get("analysis_profile_id")
            and profile.get("version") == binding.get("analysis_profile_version")
            and profile.get("content_sha256") == binding.get("analysis_profile_sha256")
        )

    @staticmethod
    def _normalize_image_analysis_artifacts(
        result: Mapping[str, object],
        artifacts: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        if isinstance(artifacts, (str, bytes, bytearray)):
            raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
        stages = result.get("stages")
        if type(stages) is not dict:
            raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
        required: dict[tuple[str, str], dict[str, object]] = {}
        for stage in ("metadata", "geocode", "vision", "embedding", "quality"):
            outcome = stages.get(stage)
            if type(outcome) is not dict:
                raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
            digest = outcome.get("artifact_sha256")
            if type(digest) is str:
                required[(stage, "stage_output")] = {
                    "artifact_sha256": digest,
                    "signal": None,
                    "model_id": None,
                    "dimensions": None,
                }
        embedding = stages.get("embedding")
        if type(embedding) is dict and type(embedding.get("output")) is dict:
            vectors = embedding["output"].get("vectors")
            if type(vectors) is list:
                for vector in vectors:
                    if type(vector) is not dict or type(vector.get("purpose")) is not str:
                        raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
                    required[("embedding", f"vector:{vector['purpose']}")] = {
                        "artifact_sha256": vector.get("artifact_sha256"),
                        "signal": vector.get("signal"),
                        "model_id": vector.get("model_id"),
                        "dimensions": vector.get("dimensions"),
                    }
        normalized: list[dict[str, object]] = []
        observed: set[tuple[str, str]] = set()
        allowed = {
            "stage",
            "name",
            "media_type",
            "signal",
            "model_id",
            "dimensions",
            "artifact_sha256",
            "bytes",
        }
        for artifact in artifacts:
            row = dict(artifact)
            if set(row) != allowed:
                raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
            stage = row.get("stage")
            name = row.get("name")
            identity = (str(stage), str(name))
            payload = row.get("bytes")
            media_type = row.get("media_type")
            if (
                type(stage) is not str
                or stage not in {"metadata", "geocode", "vision", "embedding", "quality"}
                or type(name) is not str
                or not 1 <= len(name) <= 200
                or type(media_type) is not str
                or not 1 <= len(media_type) <= 200
                or type(payload) is not bytes
                or not 1 <= len(payload) <= 64 * 1024 * 1024
                or identity in observed
                or identity not in required
            ):
                raise ImageAnalysisPersistenceError("invalid_image_analysis_artifacts")
            expected = required[identity]
            digest = hashlib.sha256(payload).hexdigest()
            if (
                row.get("artifact_sha256") != digest
                or row.get("artifact_sha256") != expected["artifact_sha256"]
                or row.get("signal") != expected["signal"]
                or row.get("model_id") != expected["model_id"]
                or row.get("dimensions") != expected["dimensions"]
            ):
                raise ImageAnalysisPersistenceError("image_analysis_artifact_mismatch")
            if name.startswith("vector:") and (
                type(row.get("dimensions")) is not int
                or len(payload) != int(row["dimensions"]) * 4
            ):
                raise ImageAnalysisPersistenceError("image_analysis_vector_shape_mismatch")
            observed.add(identity)
            normalized.append({**row, "size_bytes": len(payload)})
        if observed != set(required):
            raise ImageAnalysisPersistenceError("image_analysis_artifact_set_incomplete")
        normalized.sort(key=lambda row: (str(row["stage"]), str(row["name"])))
        return normalized

    @classmethod
    def _published_image_response_in_transaction(
        cls,
        connection: sqlite3.Connection,
        job_id: str,
    ) -> dict[str, object] | None:
        row = connection.execute(
            """SELECT binding.*,
                      meta.database_uuid AS meta_database_uuid,
                      result.asset_id AS result_asset_id,
                      result.analysis_run_id AS result_analysis_run_id,
                      result.revision AS result_revision,
                      result.content_sha256 AS result_content_sha256,
                      result.source_id AS result_source_id,
                      result.result_json AS result_json,
                      receipt.database_uuid AS receipt_database_uuid,
                      receipt.asset_id AS receipt_asset_id,
                      receipt.analysis_run_id AS receipt_analysis_run_id,
                      receipt.revision AS receipt_revision,
                      receipt.content_sha256 AS receipt_content_sha256,
                      receipt.change_position AS receipt_change_position,
                      receipt.request_sha256 AS receipt_request_sha256,
                      receipt.publish_receipt_json AS publish_receipt_json,
                      receipt.publish_receipt_sha256 AS stored_publish_receipt_sha256,
                      change.database_uuid AS change_database_uuid,
                      change.projection_contract AS change_projection_contract,
                      change.asset_id AS change_asset_id,
                      change.analysis_run_id AS change_analysis_run_id,
                      change.revision AS change_revision,
                      change.content_sha256 AS change_content_sha256,
                      change.source_id AS change_source_id,
                      change.source_binding_sha256 AS change_source_binding_sha256,
                      change.operation AS change_operation,
                      change.change_json AS change_json,
                      change.change_sha256 AS stored_change_sha256
                 FROM image_analysis_job_bindings binding
                 JOIN image_analysis_results result ON result.job_id=binding.job_id
                 JOIN image_publish_receipts receipt ON receipt.job_id=binding.job_id
                 JOIN image_projection_changes change
                   ON change.position=receipt.change_position
                 JOIN database_meta meta ON meta.singleton=1
                WHERE binding.job_id=?""",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        binding = dict(row)
        result = _parse_json(row["result_json"])
        receipt = _parse_json(row["publish_receipt_json"])
        change = _parse_json(row["change_json"])
        if receipt is None or result is None or change is None:
            raise ImageAnalysisPersistenceError("image_analysis_publication_corrupt")
        try:
            require_valid_publish_receipt(receipt)
            require_valid_image_analysis_result(result)
            require_valid_projection_change(change)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError("image_analysis_publication_corrupt") from exc
        expected_prior = (
            None
            if binding["expected_head_state"] == "missing"
            else {
                "analysis_run_id": binding["expected_head_run_id"],
                "revision": binding["expected_head_revision"],
                "content_sha256": binding["expected_head_content_sha256"],
            }
        )
        analysis_binding = {
            "analysis_run_id": binding["analysis_run_id"],
            "revision": binding["intended_revision"],
            "content_sha256": row["result_content_sha256"],
        }
        if (
            row["meta_database_uuid"] != binding["database_uuid"]
            or not cls._image_result_matches_binding(result, binding)
            or row["result_asset_id"] != binding["asset_id"]
            or row["result_analysis_run_id"] != binding["analysis_run_id"]
            or row["result_revision"] != binding["intended_revision"]
            or row["result_content_sha256"] != result.get("content_sha256")
            or row["result_source_id"] != binding["source_id"]
            or row["receipt_database_uuid"] != binding["database_uuid"]
            or row["receipt_asset_id"] != binding["asset_id"]
            or row["receipt_analysis_run_id"] != binding["analysis_run_id"]
            or row["receipt_revision"] != binding["intended_revision"]
            or row["receipt_content_sha256"] != result.get("content_sha256")
            or row["receipt_request_sha256"] != binding["request_sha256"]
            or receipt.get("publish_receipt_sha256")
            != row["stored_publish_receipt_sha256"]
            or receipt.get("database_uuid") != binding["database_uuid"]
            or receipt.get("job_id") != job_id
            or receipt.get("request_sha256") != binding["request_sha256"]
            or receipt.get("asset_id") != binding["asset_id"]
            or receipt.get("source_binding_sha256")
            != binding["source_binding_sha256"]
            or receipt.get("expected_prior_head") != expected_prior
            or receipt.get("result_head") != analysis_binding
            or receipt.get("change_position") != row["receipt_change_position"]
            or receipt.get("change_sha256") != row["stored_change_sha256"]
            or row["change_database_uuid"] != binding["database_uuid"]
            or row["change_projection_contract"]
            != change.get("projection_contract")
            or row["change_asset_id"] != binding["asset_id"]
            or row["change_analysis_run_id"] != binding["analysis_run_id"]
            or row["change_revision"] != binding["intended_revision"]
            or row["change_content_sha256"] != result.get("content_sha256")
            or row["change_source_id"] != binding["source_id"]
            or row["change_source_binding_sha256"]
            != binding["source_binding_sha256"]
            or row["change_operation"] != "upsert"
            or change.get("database_uuid") != binding["database_uuid"]
            or change.get("change_position") != row["receipt_change_position"]
            or change.get("asset_id") != binding["asset_id"]
            or change.get("analysis_binding") != analysis_binding
            or change.get("source_binding_sha256")
            != binding["source_binding_sha256"]
            or change.get("operation") != "upsert"
            or change.get("change_sha256") != row["stored_change_sha256"]
        ):
            raise ImageAnalysisPersistenceError("image_analysis_publication_corrupt")
        return {
            "job_id": job_id,
            "result": result,
            "publish_receipt": receipt,
            "change": change,
            "change_position": int(row["receipt_change_position"]),
            "projection_status": "pending",
        }

    @staticmethod
    def _image_alias_set_in_transaction(
        connection: sqlite3.Connection,
        database_uuid: str,
    ) -> tuple[int, str]:
        rows = connection.execute(
            """SELECT legacy_id,canonical_asset_id,library_root_id,source_id,
                      alias_scope,alias_sha256
                 FROM legacy_image_aliases
                WHERE database_uuid=? ORDER BY legacy_id""",
            (database_uuid,),
        ).fetchall()
        entries: list[dict[str, str]] = []
        for row in rows:
            expected_alias_sha256 = canonical_sha256(
                {
                    "database_uuid": database_uuid,
                    "legacy_id": str(row["legacy_id"]),
                    "canonical_asset_id": str(row["canonical_asset_id"]),
                    "library_root_id": str(row["library_root_id"]),
                    "source_id": str(row["source_id"]),
                    "alias_scope": str(row["alias_scope"]),
                }
            )
            if expected_alias_sha256 != row["alias_sha256"]:
                raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
            entries.append(
                {
                    "legacy_id": str(row["legacy_id"]),
                    "alias_sha256": str(row["alias_sha256"]),
                }
            )
        return len(entries), canonical_sha256(entries)

    @classmethod
    def _verified_active_projection_physical_ids_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
        permitted_missing_asset_ids: Sequence[str] = (),
    ) -> set[str]:
        """Return active IDs after proving the sealed and physical predecessor.

        An explicit legacy-backfill projection may replace one old canonical
        physical row with an exact-source legacy row before convergence.  The
        caller may omit only those absent canonical IDs from the physical
        census; immutable generation and read-manifest verification still
        covers the complete predecessor.
        """
        generation_id = cls._active_image_projection_generation_id_in_transaction(
            connection,
            database_uuid=database_uuid,
        )
        if generation_id is None:
            return set()
        generation, snapshot, _ = (
            cls._stored_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
            )
        )
        if generation["database_uuid"] != database_uuid:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_active_generation_invalid"
            )
        cls._require_image_projection_read_manifest_in_transaction(
            connection,
            generation_id=generation_id,
            require_clean_canonical_match=True,
        )
        permitted_missing = set(permitted_missing_asset_ids)
        snapshot_asset_ids = {str(item["asset_id"]) for item in snapshot}
        if not permitted_missing.issubset(snapshot_asset_ids):
            raise ImageAnalysisPersistenceError(
                "image_analysis_physical_projection_conflict"
            )
        verified_snapshot = [
            item
            for item in snapshot
            if str(item["asset_id"]) not in permitted_missing
        ]
        if not cls._physical_image_projection_semantically_matches_snapshot_in_transaction(
            connection,
            snapshot=verified_snapshot,
            managed_physical_ids=cls._managed_image_projection_physical_ids_in_transaction(
                connection,
                database_uuid=database_uuid,
            ),
            allow_managed_extras=False,
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_physical_projection_conflict"
            )
        return {str(item["asset_id"]) for item in verified_snapshot}

    @classmethod
    def _admit_legacy_projection_aliases_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
        projected_by_asset: Mapping[str, Mapping[str, object]],
        now: str,
    ) -> tuple[int, str]:
        """Alias only exact sources admitted through explicit legacy backfill."""

        projected_by_exact_source: dict[
            tuple[str, str], list[tuple[str, Mapping[str, object]]]
        ] = {}
        for asset_id, projected in projected_by_asset.items():
            key = (
                str(projected["asset_sha256"]),
                str(projected["relative_path"]),
            )
            projected_by_exact_source.setdefault(key, []).append(
                (asset_id, projected)
            )
        legacy_rows = connection.execute(
            """SELECT id,sha256,relative_path FROM image_index ORDER BY id"""
        ).fetchall()
        physical_ids = {str(row["id"]) for row in legacy_rows}
        permitted_missing_asset_ids = {
            asset_id
            for asset_id, projected in projected_by_asset.items()
            if (
                projected["enqueue_scope"]
                == LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE
                and asset_id not in physical_ids
                and any(
                    str(row["id"]) != asset_id
                    and row["sha256"] == projected["asset_sha256"]
                    and row["relative_path"] == projected["relative_path"]
                    for row in legacy_rows
                )
            )
        }
        active_projected_asset_ids = (
            cls._verified_active_projection_physical_ids_in_transaction(
                connection,
                database_uuid=database_uuid,
                permitted_missing_asset_ids=tuple(
                    sorted(permitted_missing_asset_ids)
                ),
            )
        )
        aliases_to_append: list[dict[str, str]] = []
        legacy_rows_to_remove: list[tuple[str, str, str]] = []
        for legacy_row in legacy_rows:
            legacy_id = str(legacy_row["id"])
            legacy_sha256 = str(legacy_row["sha256"])
            legacy_relative_path = str(legacy_row["relative_path"])
            canonical = projected_by_asset.get(legacy_id)
            if canonical is not None:
                if legacy_id in active_projected_asset_ids:
                    continue
                if (
                    legacy_sha256 != canonical["asset_sha256"]
                    or legacy_relative_path != canonical["relative_path"]
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_physical_projection_conflict"
                    )
                if (
                    canonical["enqueue_scope"]
                    != LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_legacy_backfill_required"
                    )
                continue
            exact_matches = projected_by_exact_source.get(
                (legacy_sha256, legacy_relative_path),
                [],
            )
            admitted_matches = [
                match
                for match in exact_matches
                if match[1]["enqueue_scope"]
                == LEGACY_IMAGE_BACKFILL_ENQUEUE_SCOPE
            ]
            if not admitted_matches:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_legacy_backfill_required"
                )
            if len(admitted_matches) != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_legacy_backfill_ambiguous"
                )
            canonical_asset_id, projected = admitted_matches[0]
            identity = {
                "database_uuid": database_uuid,
                "legacy_id": legacy_id,
                "canonical_asset_id": canonical_asset_id,
                "library_root_id": str(projected["library_root_id"]),
                "source_id": str(projected["source_id"]),
                "alias_scope": "legacy-image-index/v1",
            }
            alias_sha256 = canonical_sha256(identity)
            existing_alias = connection.execute(
                """SELECT canonical_asset_id,library_root_id,source_id,
                          alias_scope,alias_sha256
                     FROM legacy_image_aliases
                    WHERE database_uuid=? AND legacy_id=?""",
                (database_uuid, legacy_id),
            ).fetchone()
            if existing_alias is not None and any(
                existing_alias[field] != identity[field]
                for field in (
                    "canonical_asset_id",
                    "library_root_id",
                    "source_id",
                    "alias_scope",
                )
            ):
                raise ImageAnalysisPersistenceError("legacy_alias_conflict")
            if (
                existing_alias is not None
                and existing_alias["alias_sha256"] != alias_sha256
            ):
                raise ImageAnalysisPersistenceError("legacy_alias_conflict")
            if existing_alias is None:
                aliases_to_append.append(
                    {**identity, "alias_sha256": alias_sha256}
                )
            legacy_rows_to_remove.append(
                (legacy_id, legacy_sha256, legacy_relative_path)
            )
        for alias in aliases_to_append:
            connection.execute(
                """INSERT INTO legacy_image_aliases(
                       database_uuid,legacy_id,canonical_asset_id,library_root_id,
                       source_id,alias_scope,alias_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    alias["database_uuid"],
                    alias["legacy_id"],
                    alias["canonical_asset_id"],
                    alias["library_root_id"],
                    alias["source_id"],
                    alias["alias_scope"],
                    alias["alias_sha256"],
                    now,
                ),
            )
        for legacy_id, legacy_sha256, legacy_relative_path in legacy_rows_to_remove:
            removed = connection.execute(
                """DELETE FROM image_index
                    WHERE id=? AND sha256=? AND relative_path=?""",
                (legacy_id, legacy_sha256, legacy_relative_path),
            )
            if removed.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_physical_projection_conflict"
                )
        return cls._image_alias_set_in_transaction(connection, database_uuid)

    @staticmethod
    def _require_exact_projected_head_binding(
        *,
        head: Mapping[str, object],
        result: Mapping[str, object],
        document: Mapping[str, object],
        source_projection: Mapping[str, str],
        expected_analysis_binding: Mapping[str, object],
        projector_version: str,
    ) -> None:
        document_row = document.get("row")
        result_source_binding = result.get("source_binding")
        if type(document_row) is not dict or type(result_source_binding) is not dict:
            raise ImageAnalysisPersistenceError("image_analysis_projection_blocked")
        if (
            document.get("projector_version") != projector_version
            or document.get("asset_id") != head["asset_id"]
            or document.get("analysis_binding") != expected_analysis_binding
            or document.get("source_binding_sha256")
            != source_binding_sha256(result_source_binding)
            or document_row.get("id") != head["asset_id"]
            or document_row.get("sha256") != result.get("asset_sha256")
            or document_row.get("filename") != source_projection["filename"]
            or document_row.get("relative_path")
            != source_projection["relative_path"]
            or head["binding_asset_id"] != head["asset_id"]
            or head["binding_source_id"] != head["source_id"]
            or head["binding_library_root_id"] != head["library_root_id"]
            or head["binding_relative_path"] != head["relative_path"]
            or head["binding_asset_sha256"] != result.get("asset_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_blocked")

    @staticmethod
    def _image_projection_artifacts_in_transaction(
        connection: sqlite3.Connection,
        *,
        asset_id: str,
        analysis_run_id: str,
    ) -> list[dict[str, object]]:
        rows = connection.execute(
            """SELECT stage,name,media_type,signal,model_id,dimensions,
                      artifact_sha256,artifact_blob
                 FROM image_analysis_artifacts
                WHERE asset_id=? AND analysis_run_id=?
                ORDER BY stage,name""",
            (asset_id, analysis_run_id),
        ).fetchall()
        return [
            {
                "stage": str(row["stage"]),
                "name": str(row["name"]),
                "media_type": str(row["media_type"]),
                "signal": row["signal"],
                "model_id": row["model_id"],
                "dimensions": row["dimensions"],
                "artifact_sha256": str(row["artifact_sha256"]),
                "bytes": bytes(row["artifact_blob"]),
            }
            for row in rows
        ]

    @classmethod
    def _verified_projection_generation_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        database_uuid: str,
        projection_contract: str,
        minimum_high_water_position: int,
        asset_id: str,
        analysis_binding: Mapping[str, object],
        source_binding_digest: str,
        source_relative_path: str,
        result: Mapping[str, object],
        expected_projector_version: str | None,
        expected_row_sha256: str | None,
        expected_alias_set_sha256: str | None,
        require_active: bool,
        require_physical: bool,
    ) -> dict[str, object] | None:
        """Verify one immutable generation without treating a receipt as its pointer."""

        generation_rows = connection.execute(
            """SELECT generation.database_uuid,
                      generation.projection_contract,
                      generation.canonical_high_water_position,
                      generation.compiler_id,generation.projector_version,
                      generation.status,generation.is_active,
                      projected.analysis_run_id AS row_analysis_run_id,
                      projected.revision AS row_revision,
                      projected.content_sha256 AS row_content_sha256,
                      projected.source_id AS row_source_id,
                      projected.source_binding_sha256 AS row_source_binding_sha256,
                      projected.row_json,projected.row_sha256,
                      projected.alias_set_sha256 AS row_alias_set_sha256,
                      manifest.database_uuid AS manifest_database_uuid,
                      manifest.canonical_high_water_position AS manifest_high_water_position,
                      manifest.compiler_id AS manifest_compiler_id,
                      manifest.projector_version AS manifest_projector_version,
                      manifest.eligible_count,manifest.projected_count,
                      manifest.alias_count,manifest.missing_count,
                      manifest.unexpected_count,manifest.mismatched_count,
                      manifest.blocked_count,
                      manifest.alias_set_sha256 AS manifest_alias_set_sha256,
                      manifest.row_set_sha256 AS manifest_row_set_sha256,
                      manifest.manifest_json,manifest.manifest_sha256
                 FROM image_projection_generations generation
                 LEFT JOIN image_projection_rows projected
                   ON projected.generation_id=generation.id
                  AND projected.asset_id=?
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=generation.id
                WHERE generation.id=? LIMIT 2""",
            (asset_id, generation_id),
        ).fetchall()
        if len(generation_rows) != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        row = generation_rows[0]
        if (
            row["database_uuid"] != database_uuid
            or row["projection_contract"] != projection_contract
            or int(row["canonical_high_water_position"] or -1)
            < minimum_high_water_position
            or (
                expected_projector_version is not None
                and row["projector_version"] != expected_projector_version
            )
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        if row["status"] != "complete":
            return None
        if require_active and int(row["is_active"] or 0) != 1:
            return None
        result_source_binding = result.get("source_binding")
        if (
            type(result_source_binding) is not dict
            or row["manifest_json"] is None
            or row["row_json"] is None
            or row["manifest_database_uuid"] != database_uuid
            or row["manifest_high_water_position"]
            != row["canonical_high_water_position"]
            or row["manifest_compiler_id"] != row["compiler_id"]
            or row["manifest_projector_version"] != row["projector_version"]
            or row["eligible_count"] != row["projected_count"]
            or any(
                int(row[field] or 0) != 0
                for field in (
                    "missing_count",
                    "unexpected_count",
                    "mismatched_count",
                    "blocked_count",
                )
            )
            or row["row_analysis_run_id"]
            != analysis_binding.get("analysis_run_id")
            or row["row_revision"] != analysis_binding.get("revision")
            or row["row_content_sha256"]
            != analysis_binding.get("content_sha256")
            or row["row_source_id"] != result_source_binding.get("source_id")
            or row["row_source_binding_sha256"] != source_binding_digest
            or (
                expected_row_sha256 is not None
                and row["row_sha256"] != expected_row_sha256
            )
            or (
                expected_alias_set_sha256 is not None
                and row["row_alias_set_sha256"] != expected_alias_set_sha256
            )
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

        manifest = _parse_json(row["manifest_json"])
        projected = _parse_json(row["row_json"])
        if manifest is None or projected is None:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        try:
            require_valid_projection_manifest(manifest)
            require_valid_projected_image_index_row(projected)
        except (ImageAnalysisContractError, ImageProjectionRenderError) as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        if (
            manifest.get("manifest_sha256") != row["manifest_sha256"]
            or manifest.get("projection_contract") != projection_contract
            or manifest.get("database_uuid") != database_uuid
            or manifest.get("canonical_high_water_mark")
            != row["canonical_high_water_position"]
            or manifest.get("compiler_version") != row["compiler_id"]
            or manifest.get("projector_version") != row["projector_version"]
            or manifest.get("alias_set_sha256")
            != row["manifest_alias_set_sha256"]
            or manifest.get("row_set_sha256") != row["manifest_row_set_sha256"]
            or projected.get("asset_id") != asset_id
            or projected.get("analysis_binding") != analysis_binding
            or projected.get("source_binding_sha256") != source_binding_digest
            or projected.get("row_sha256") != row["row_sha256"]
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

        stored_generation_rows = connection.execute(
            """SELECT asset_id,analysis_run_id,revision,content_sha256,row_sha256
                 FROM image_projection_rows
                WHERE generation_id=? ORDER BY asset_id""",
            (generation_id,),
        ).fetchall()
        row_set = [
            {
                "asset_id": str(item["asset_id"]),
                "projected_row_sha256": str(item["row_sha256"]),
            }
            for item in stored_generation_rows
        ]
        expected_entries = [
            {
                "asset_id": str(item["asset_id"]),
                "analysis_binding": {
                    "analysis_run_id": str(item["analysis_run_id"]),
                    "revision": int(item["revision"]),
                    "content_sha256": str(item["content_sha256"]),
                },
                "expected_row_sha256": str(item["row_sha256"]),
                "actual_row_sha256": str(item["row_sha256"]),
                "status": "matched",
                "reason_code": None,
            }
            for item in stored_generation_rows
        ]
        alias_rows = connection.execute(
            """SELECT legacy_id,alias_sha256
                 FROM image_projection_aliases
                WHERE generation_id=? ORDER BY legacy_id""",
            (generation_id,),
        ).fetchall()
        alias_set = [
            {
                "legacy_id": str(alias["legacy_id"]),
                "alias_sha256": str(alias["alias_sha256"]),
            }
            for alias in alias_rows
        ]
        counts = manifest.get("counts")
        entries = manifest.get("entries")
        stored_counts = {
            "eligible": row["eligible_count"],
            "projected": row["projected_count"],
            "aliases": row["alias_count"],
            "missing": row["missing_count"],
            "unexpected": row["unexpected_count"],
            "mismatched": row["mismatched_count"],
            "blocked": row["blocked_count"],
        }
        if (
            type(counts) is not dict
            or counts != stored_counts
            or entries != expected_entries
            or int(row["projected_count"]) != len(row_set)
            or int(row["alias_count"]) != len(alias_set)
            or counts.get("eligible") != len(row_set)
            or counts.get("projected") != len(row_set)
            or counts.get("aliases") != len(alias_set)
            or canonical_sha256(row_set) != row["manifest_row_set_sha256"]
            or canonical_sha256(alias_set) != row["manifest_alias_set_sha256"]
            or row["row_alias_set_sha256"] != row["manifest_alias_set_sha256"]
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

        artifact_rows = cls._image_projection_artifacts_in_transaction(
            connection,
            asset_id=asset_id,
            analysis_run_id=str(analysis_binding["analysis_run_id"]),
        )
        try:
            rendered = render_projected_image_index_row(
                result=result,
                source_projection={
                    "filename": source_relative_path.rsplit("/", 1)[-1],
                    "relative_path": source_relative_path,
                },
                artifacts=artifact_rows,
                projector_version=str(row["projector_version"]),
            )
        except ImageProjectionRenderError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        if rendered != projected:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

        if require_physical:
            physical_columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
            physical = connection.execute(
                f"SELECT {physical_columns} FROM image_index WHERE id=?",
                (asset_id,),
            ).fetchone()
            if (
                physical is None
                or type(physical["created_at"]) is not str
                or not physical["created_at"]
                or type(physical["updated_at"]) is not str
                or not physical["updated_at"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
            expected_physical = materialize_projected_image_index_row(
                projected,
                created_at=str(physical["created_at"]),
                updated_at=str(physical["updated_at"]),
            )
            if any(
                physical[column] != expected_physical[column]
                for column in IMAGE_INDEX_SQL_COLUMNS
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
        if require_active:
            cls._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                require_clean_canonical_match=True,
            )
        return {
            "generation_id": generation_id,
            "row_sha256": str(row["row_sha256"]),
            "canonical_high_water_position": int(
                row["canonical_high_water_position"]
            ),
        }

    @classmethod
    def _verified_removed_projection_generation_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        database_uuid: str,
        minimum_high_water_position: int,
        asset_id: str,
        require_active: bool,
        require_physical_absence: bool,
    ) -> dict[str, object] | None:
        """Verify one complete rowless generation for a removed current asset."""

        rows = connection.execute(
            """SELECT generation.database_uuid,generation.projection_contract,
                      generation.canonical_high_water_position,
                      generation.compiler_id,generation.projector_version,
                      generation.status,generation.is_active,
                      manifest.eligible_count,manifest.projected_count,
                      manifest.alias_count,manifest.missing_count,
                      manifest.unexpected_count,manifest.mismatched_count,
                      manifest.blocked_count,manifest.alias_set_sha256,
                      manifest.row_set_sha256,manifest.manifest_json,
                      manifest.manifest_sha256
                 FROM image_projection_generations generation
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=generation.id
                WHERE generation.id=? LIMIT 2""",
            (generation_id,),
        ).fetchall()
        if len(rows) != 1:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        generation = rows[0]
        if (
            generation["database_uuid"] != database_uuid
            or generation["projection_contract"] != IMAGE_PROJECTION_CONTRACT
            or type(generation["canonical_high_water_position"]) is not int
            or generation["canonical_high_water_position"]
            < minimum_high_water_position
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        if generation["status"] != "complete":
            return None
        if require_active and int(generation["is_active"] or 0) != 1:
            return None
        manifest = _parse_json(generation["manifest_json"])
        if manifest is None:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        try:
            require_valid_projection_manifest(manifest)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        projected_rows = connection.execute(
            """SELECT asset_id,analysis_run_id,revision,content_sha256,
                      source_id,source_binding_sha256,row_json,row_sha256,
                      alias_set_sha256
                 FROM image_projection_rows
                WHERE generation_id=? ORDER BY asset_id""",
            (generation_id,),
        ).fetchall()
        if any(row["asset_id"] == asset_id for row in projected_rows):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        expected_entries: list[dict[str, object]] = []
        row_set: list[dict[str, str]] = []
        for projected_row in projected_rows:
            document = _parse_json(projected_row["row_json"])
            if document is None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
            try:
                require_valid_projected_image_index_row(document)
            except ImageProjectionRenderError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                ) from exc
            analysis_binding = {
                "analysis_run_id": str(projected_row["analysis_run_id"]),
                "revision": int(projected_row["revision"]),
                "content_sha256": str(projected_row["content_sha256"]),
            }
            if (
                document.get("asset_id") != projected_row["asset_id"]
                or document.get("analysis_binding") != analysis_binding
                or document.get("source_binding_sha256")
                != projected_row["source_binding_sha256"]
                or document.get("row_sha256") != projected_row["row_sha256"]
                or projected_row["alias_set_sha256"]
                != generation["alias_set_sha256"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
            expected_entries.append(
                {
                    "asset_id": str(projected_row["asset_id"]),
                    "analysis_binding": analysis_binding,
                    "expected_row_sha256": str(projected_row["row_sha256"]),
                    "actual_row_sha256": str(projected_row["row_sha256"]),
                    "status": "matched",
                    "reason_code": None,
                }
            )
            row_set.append(
                {
                    "asset_id": str(projected_row["asset_id"]),
                    "projected_row_sha256": str(projected_row["row_sha256"]),
                }
            )
        alias_rows = connection.execute(
            """SELECT legacy_id,alias_sha256 FROM image_projection_aliases
                WHERE generation_id=? ORDER BY legacy_id""",
            (generation_id,),
        ).fetchall()
        alias_set = [
            {
                "legacy_id": str(alias["legacy_id"]),
                "alias_sha256": str(alias["alias_sha256"]),
            }
            for alias in alias_rows
        ]
        expected_manifest = build_projection_manifest(
            projector_version=str(generation["projector_version"]),
            compiler_version=str(generation["compiler_id"]),
            database_uuid=database_uuid,
            canonical_high_water_mark=int(
                generation["canonical_high_water_position"]
            ),
            entries=expected_entries,
            alias_count=len(alias_set),
            alias_set_sha256=canonical_sha256(alias_set),
        )
        counts = manifest.get("counts")
        if (
            manifest != expected_manifest
            or manifest.get("manifest_sha256") != generation["manifest_sha256"]
            or manifest.get("row_set_sha256") != generation["row_set_sha256"]
            or manifest.get("alias_set_sha256")
            != generation["alias_set_sha256"]
            or canonical_sha256(row_set) != generation["row_set_sha256"]
            or type(counts) is not dict
            or counts.get("eligible") != generation["eligible_count"]
            or counts.get("projected") != generation["projected_count"]
            or counts.get("aliases") != generation["alias_count"]
            or counts.get("missing") != generation["missing_count"]
            or counts.get("unexpected") != generation["unexpected_count"]
            or counts.get("mismatched") != generation["mismatched_count"]
            or counts.get("blocked") != generation["blocked_count"]
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        if require_physical_absence:
            physical = connection.execute(
                """SELECT 1 FROM image_index WHERE id=? OR id IN (
                       SELECT legacy_id FROM legacy_image_aliases
                        WHERE database_uuid=? AND canonical_asset_id=?)
                    LIMIT 1""",
                (asset_id, database_uuid, asset_id),
            ).fetchone()
            if physical is not None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
        if require_active:
            cls._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                require_clean_canonical_match=True,
            )
        return {
            "generation_id": generation_id,
            "canonical_high_water_position": int(
                generation["canonical_high_water_position"]
            ),
        }

    @classmethod
    def _verified_removed_projection_for_current_head_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        asset_id: str,
        analysis_binding: Mapping[str, object],
        source_id: str,
    ) -> dict[str, object] | None:
        latest = cls._verified_latest_image_projection_change_in_transaction(
            connection,
            asset_id=asset_id,
        )
        if latest is None or latest["operation"] != "remove":
            return None
        meta = cls._image_meta_in_transaction(connection)
        if (
            latest["database_uuid"] != meta["database_uuid"]
            or latest["source_id"] != source_id
            or latest["source_binding_sha256"] is not None
            or latest["change"]["analysis_binding"] != analysis_binding
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        receipt_row = connection.execute(
            """SELECT generation_id,projection_contract,projector_version,
                      database_uuid,asset_id,analysis_run_id,revision,
                      content_sha256,source_binding_sha256,
                      projected_row_sha256,alias_set_sha256,outcome,
                      reason_code,receipt_json,receipt_sha256
                 FROM image_projection_receipts WHERE change_position=?""",
            (latest["position"],),
        ).fetchone()
        if receipt_row is None:
            return {
                "status": "unavailable",
                "outcome": None,
                "reason_code": "image_projection_removal_pending",
                "receipt_sha256": None,
                "generation_id": None,
                "processing_generation_id": None,
                "change_position": int(latest["position"]),
            }
        receipt = _parse_json(receipt_row["receipt_json"])
        if receipt is None:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        try:
            require_valid_projection_receipt(receipt)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        processing_generation_id = receipt_row["generation_id"]
        if (
            type(processing_generation_id) is not str
            or receipt.get("receipt_sha256") != receipt_row["receipt_sha256"]
            or receipt.get("projection_contract")
            != receipt_row["projection_contract"]
            or receipt.get("projector_version")
            != receipt_row["projector_version"]
            or receipt.get("database_uuid") != receipt_row["database_uuid"]
            or receipt.get("database_uuid") != meta["database_uuid"]
            or receipt.get("change_position") != latest["position"]
            or receipt.get("asset_id") != asset_id
            or receipt.get("analysis_binding") != analysis_binding
            or receipt.get("source_binding_sha256") is not None
            or receipt.get("projected_row_sha256") is not None
            or receipt.get("alias_set_sha256") != receipt_row["alias_set_sha256"]
            or receipt.get("outcome") != "removed"
            or receipt.get("reason_code") != receipt_row["reason_code"]
            or receipt_row["asset_id"] != asset_id
            or receipt_row["analysis_run_id"]
            != analysis_binding.get("analysis_run_id")
            or receipt_row["revision"] != analysis_binding.get("revision")
            or receipt_row["content_sha256"]
            != analysis_binding.get("content_sha256")
            or receipt_row["source_binding_sha256"] is not None
            or receipt_row["projected_row_sha256"] is not None
            or receipt_row["outcome"] != "removed"
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        processing = cls._verified_removed_projection_generation_in_transaction(
            connection,
            generation_id=processing_generation_id,
            database_uuid=str(meta["database_uuid"]),
            minimum_high_water_position=int(latest["position"]),
            asset_id=asset_id,
            require_active=False,
            require_physical_absence=False,
        )
        if processing is None:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        active_rows = connection.execute(
            """SELECT id FROM image_projection_generations
                WHERE database_uuid=? AND projection_contract=?
                  AND status='complete' AND is_active=1 LIMIT 2""",
            (meta["database_uuid"], IMAGE_PROJECTION_CONTRACT),
        ).fetchall()
        if not active_rows:
            return {
                "status": "unavailable",
                "outcome": "removed",
                "reason_code": "projection_generation_not_active",
                "receipt_sha256": str(receipt_row["receipt_sha256"]),
                "generation_id": None,
                "processing_generation_id": processing_generation_id,
                "change_position": int(latest["position"]),
            }
        if len(active_rows) != 1:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        active_generation_id = str(active_rows[0]["id"])
        active = cls._verified_removed_projection_generation_in_transaction(
            connection,
            generation_id=active_generation_id,
            database_uuid=str(meta["database_uuid"]),
            minimum_high_water_position=int(latest["position"]),
            asset_id=asset_id,
            require_active=True,
            require_physical_absence=True,
        )
        if active is None:
            return {
                "status": "unavailable",
                "outcome": "removed",
                "reason_code": "projection_generation_not_active",
                "receipt_sha256": str(receipt_row["receipt_sha256"]),
                "generation_id": active_generation_id,
                "processing_generation_id": processing_generation_id,
                "change_position": int(latest["position"]),
            }
        return {
            "status": "unavailable",
            "outcome": "removed",
            "reason_code": str(receipt_row["reason_code"]),
            "receipt_sha256": str(receipt_row["receipt_sha256"]),
            "generation_id": active_generation_id,
            "processing_generation_id": processing_generation_id,
            "change_position": int(latest["position"]),
        }

    @classmethod
    def _verified_projection_for_change_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        publication: Mapping[str, object],
        source_relative_path: str,
    ) -> dict[str, object] | None:
        """Verify historical processing proof and current projection independently."""

        change_position = publication.get("change_position")
        change = publication.get("change")
        result = publication.get("result")
        if type(change_position) is not int or type(change) is not dict or type(result) is not dict:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        row = connection.execute(
            """SELECT change.change_json,change.change_sha256,
                      meta.database_uuid AS meta_database_uuid,
                      receipt.generation_id AS processing_generation_id,
                      receipt.projection_contract,
                      receipt.projector_version,receipt.database_uuid,
                      receipt.asset_id,receipt.analysis_run_id,receipt.revision,
                      receipt.content_sha256,receipt.source_binding_sha256,
                      receipt.projected_row_sha256,receipt.alias_set_sha256,
                      receipt.outcome,receipt.reason_code,receipt.receipt_json,
                      receipt.receipt_sha256,
                      head.analysis_run_id AS head_analysis_run_id,
                      head.revision AS head_revision,
                      head.content_sha256 AS head_content_sha256
                 FROM image_projection_changes change
                 JOIN database_meta meta ON meta.singleton=1
                 LEFT JOIN image_projection_receipts receipt
                   ON receipt.change_position=change.position
                 LEFT JOIN image_analysis_heads head ON head.asset_id=change.asset_id
                WHERE change.position=?""",
            (change_position,),
        ).fetchone()
        if row is None:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        stored_change = _parse_json(row["change_json"])
        if stored_change != change or row["change_sha256"] != change.get("change_sha256"):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        if row["receipt_json"] is None:
            return None
        receipt = _parse_json(row["receipt_json"])
        if receipt is None:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        try:
            require_valid_projection_receipt(receipt)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt") from exc
        analysis_binding = change.get("analysis_binding")
        if (
            row["meta_database_uuid"] != change.get("database_uuid")
            or receipt.get("receipt_sha256") != row["receipt_sha256"]
            or receipt.get("projection_contract") != row["projection_contract"]
            or receipt.get("projector_version") != row["projector_version"]
            or receipt.get("database_uuid") != row["database_uuid"]
            or receipt.get("database_uuid") != change.get("database_uuid")
            or receipt.get("change_position") != change_position
            or receipt.get("asset_id") != change.get("asset_id")
            or receipt.get("analysis_binding") != analysis_binding
            or receipt.get("source_binding_sha256")
            != change.get("source_binding_sha256")
            or receipt.get("projected_row_sha256") != row["projected_row_sha256"]
            or receipt.get("alias_set_sha256") != row["alias_set_sha256"]
            or receipt.get("outcome") != row["outcome"]
            or receipt.get("reason_code") != row["reason_code"]
            or row["asset_id"] != change.get("asset_id")
            or type(analysis_binding) is not dict
            or row["analysis_run_id"] != analysis_binding.get("analysis_run_id")
            or row["revision"] != analysis_binding.get("revision")
            or row["content_sha256"] != analysis_binding.get("content_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        outcome = str(row["outcome"])
        if outcome not in {"applied", "no_change"}:
            return {
                "status": "blocked" if outcome == "blocked" else "pending",
                "outcome": outcome,
                "reason_code": row["reason_code"],
                "receipt_sha256": str(row["receipt_sha256"]),
                "generation_id": None,
                "processing_generation_id": row["processing_generation_id"],
            }

        processing_generation_id = row["processing_generation_id"]
        if type(processing_generation_id) is not str:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        processing = cls._verified_projection_generation_in_transaction(
            connection,
            generation_id=processing_generation_id,
            database_uuid=str(row["database_uuid"]),
            projection_contract=str(row["projection_contract"]),
            minimum_high_water_position=change_position,
            asset_id=str(change["asset_id"]),
            analysis_binding=analysis_binding,
            source_binding_digest=str(change["source_binding_sha256"]),
            source_relative_path=source_relative_path,
            result=result,
            expected_projector_version=str(row["projector_version"]),
            expected_row_sha256=str(row["projected_row_sha256"]),
            expected_alias_set_sha256=str(row["alias_set_sha256"]),
            require_active=False,
            require_physical=False,
        )
        if processing is None:
            return {
                "status": "pending",
                "outcome": outcome,
                "reason_code": "projection_generation_not_active",
                "receipt_sha256": str(row["receipt_sha256"]),
                "generation_id": None,
                "processing_generation_id": processing_generation_id,
            }

        if (
            row["head_analysis_run_id"] != analysis_binding.get("analysis_run_id")
            or row["head_revision"] != analysis_binding.get("revision")
            or row["head_content_sha256"] != analysis_binding.get("content_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

        latest = connection.execute(
            """SELECT MAX(position) AS global_position,
                      MAX(CASE WHEN asset_id=? THEN position END) AS asset_position
                 FROM image_projection_changes""",
            (change["asset_id"],),
        ).fetchone()
        if (
            latest is None
            or type(latest["global_position"]) is not int
            or type(latest["asset_position"]) is not int
            or latest["asset_position"] != change_position
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        global_latest_position = int(latest["global_position"])
        asset_latest_position = int(latest["asset_position"])
        active_rows = connection.execute(
            """SELECT id,canonical_high_water_position
                 FROM image_projection_generations
                WHERE database_uuid=? AND projection_contract=?
                  AND status='complete' AND is_active=1 LIMIT 2""",
            (row["database_uuid"], row["projection_contract"]),
        ).fetchall()
        if not active_rows:
            return {
                "status": "pending",
                "outcome": outcome,
                "reason_code": "projection_generation_not_active",
                "receipt_sha256": str(row["receipt_sha256"]),
                "generation_id": None,
                "processing_generation_id": processing_generation_id,
            }
        if len(active_rows) != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        active_generation_id = str(active_rows[0]["id"])
        active_high_water_value = active_rows[0]["canonical_high_water_position"]
        if type(active_high_water_value) is not int or active_high_water_value < 0:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        active_high_water = active_high_water_value
        if active_high_water > global_latest_position:
            raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
        if active_high_water < asset_latest_position:
            return {
                "status": "pending",
                "outcome": outcome,
                "reason_code": "projection_generation_not_current",
                "receipt_sha256": str(row["receipt_sha256"]),
                "generation_id": active_generation_id,
                "processing_generation_id": processing_generation_id,
            }
        active = cls._verified_projection_generation_in_transaction(
            connection,
            generation_id=active_generation_id,
            database_uuid=str(row["database_uuid"]),
            projection_contract=str(row["projection_contract"]),
            minimum_high_water_position=asset_latest_position,
            asset_id=str(change["asset_id"]),
            analysis_binding=analysis_binding,
            source_binding_digest=str(change["source_binding_sha256"]),
            source_relative_path=source_relative_path,
            result=result,
            expected_projector_version=None,
            expected_row_sha256=None,
            expected_alias_set_sha256=None,
            require_active=True,
            require_physical=True,
        )
        if active is None:
            return {
                "status": "pending",
                "outcome": outcome,
                "reason_code": "projection_generation_not_active",
                "receipt_sha256": str(row["receipt_sha256"]),
                "generation_id": active_generation_id,
                "processing_generation_id": processing_generation_id,
            }
        return {
            "status": "current",
            "outcome": outcome,
            "reason_code": None,
            "receipt_sha256": str(row["receipt_sha256"]),
            "generation_id": active_generation_id,
            "processing_generation_id": processing_generation_id,
            "row_sha256": str(active["row_sha256"]),
        }

    def _require_exact_image_publication_replay_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        expected_attempt: int,
        current_binding: Mapping[str, object],
        detached_result: Mapping[str, object],
        normalized_artifacts: Sequence[Mapping[str, object]],
        expected_stage_outcomes: Sequence[Mapping[str, object]],
        expected_prepublication_artifacts: Sequence[Mapping[str, object]],
        replay: Mapping[str, object],
    ) -> dict[str, object]:
        """Verify immutable publication evidence before recovering one attempt."""

        if replay.get("result") != detached_result:
            raise ImageAnalysisPersistenceError(
                "image_analysis_publication_conflict"
            )
        stored_request = _parse_json(current_binding["request_json"])
        if stored_request is None:
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        try:
            require_valid_image_analysis_request(stored_request)
        except ImageAnalysisJobContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_binding_corrupt"
            ) from exc
        if (
            image_analysis_request_sha256(stored_request)
            != current_binding["request_sha256"]
            or current_binding["attempt_request_sha256"]
            != current_binding["request_sha256"]
            or current_binding["attempt_database_uuid"]
            != current_binding["database_uuid"]
            or int(current_binding["attempt_database_device"])
            != int(current_binding["database_device"])
            or int(current_binding["attempt_database_inode"])
            != int(current_binding["database_inode"])
        ):
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        expected_stored_artifacts = [
            {
                key: artifact[key]
                for key in (
                    "stage",
                    "name",
                    "media_type",
                    "signal",
                    "model_id",
                    "dimensions",
                    "artifact_sha256",
                    "bytes",
                )
            }
            for artifact in normalized_artifacts
        ]
        stored_artifacts = self._image_projection_artifacts_in_transaction(
            connection,
            asset_id=str(current_binding["asset_id"]),
            analysis_run_id=str(current_binding["analysis_run_id"]),
        )
        if stored_artifacts != expected_stored_artifacts:
            raise ImageAnalysisPersistenceError("image_analysis_artifact_mismatch")
        change = replay.get("change")
        publish_receipt = replay.get("publish_receipt")
        if type(change) is not dict or type(publish_receipt) is not dict:
            raise ImageAnalysisPersistenceError(
                "image_analysis_publication_corrupt"
            )
        publication_binding = {
            "analysis_run_id": current_binding["analysis_run_id"],
            "revision": current_binding["intended_revision"],
            "content_sha256": detached_result["content_sha256"],
            "change_position": replay["change_position"],
            "publish_receipt_sha256": publish_receipt[
                "publish_receipt_sha256"
            ],
            "projection_receipt_sha256": None,
        }
        expected_published_artifacts = [
            *expected_prepublication_artifacts,
            {
                "stage": "publish",
                "name": "change",
                "sha256": change["change_sha256"],
            },
            {
                "stage": "publish",
                "name": "receipt",
                "sha256": publish_receipt["publish_receipt_sha256"],
            },
        ]
        expected_published_artifacts.sort(
            key=lambda item: (str(item["stage"]), str(item["name"]))
        )
        expected_checkpoint = {
            "object": "memolens.image_analysis_checkpoint",
            "schema_version": "1",
            "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
            "job_id": job_id,
            "attempt": expected_attempt,
            "current_stage": "shadow_projection",
            "completed_stages": [
                "source_admission",
                "metadata",
                "geocode",
                "vision",
                "embedding",
                "quality",
                "publish",
            ],
            "stage_outcomes": [
                *expected_stage_outcomes,
                {
                    "stage": "publish",
                    "outcome": "succeeded",
                    "outcome_sha256": canonical_job_sha256(
                        publication_binding
                    ),
                    "reason_code": None,
                },
            ],
            "artifact_digests": expected_published_artifacts,
            "publish_binding": publication_binding,
        }
        require_valid_image_analysis_checkpoint(expected_checkpoint)
        observed_head = self._expected_image_head_in_transaction(
            connection,
            str(current_binding["asset_id"]),
        )
        if observed_head != change.get("analysis_binding"):
            raise ImageAnalysisPersistenceError("image_analysis_head_conflict")
        projection_receipt = connection.execute(
            """SELECT 1 FROM image_projection_receipts
                WHERE change_position=?""",
            (replay["change_position"],),
        ).fetchone()
        if projection_receipt is not None:
            raise ImageAnalysisPersistenceError(
                "image_analysis_checkpoint_result_mismatch"
            )
        return expected_checkpoint

    @staticmethod
    def _advance_recovered_image_publication_in_transaction(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        runtime_generation: str,
        expected_attempt: int,
        current_binding: Mapping[str, object],
        expected_checkpoint: Mapping[str, object],
        now: str,
    ) -> None:
        """Advance only the current claimed attempt; immutable evidence is untouched."""

        state_update = connection.execute(
            """UPDATE image_analysis_attempt_states
                  SET heartbeat_sequence=heartbeat_sequence+1,heartbeat_at=?
                WHERE job_id=? AND attempt=? AND state='claimed'
                  AND heartbeat_sequence=? AND authority_sha256=?
                  AND EXISTS(
                    SELECT 1 FROM image_analysis_attempt_authorities authority
                     WHERE authority.job_id=? AND authority.attempt=?
                       AND authority.authority_sha256=?
                       AND authority.runtime_generation=?)""",
            (
                now,
                job_id,
                expected_attempt,
                current_binding["heartbeat_sequence"],
                current_binding["attempt_authority_sha256"],
                job_id,
                expected_attempt,
                current_binding["attempt_authority_sha256"],
                runtime_generation,
            ),
        )
        if state_update.rowcount != 1:
            raise ImageAnalysisPersistenceError(
                "image_analysis_checkpoint_conflict"
            )
        job_update = connection.execute(
            """UPDATE media_jobs
                  SET status='running',stage='shadow_projection',progress=0.9,
                      checkpoint_json=?,heartbeat_at=?
                WHERE id=? AND kind='image_analysis' AND attempt=?
                  AND status='running' AND stage='publish'
                  AND cancel_requested=0""",
            (
                canonical_job_json(expected_checkpoint),
                now,
                job_id,
                expected_attempt,
            ),
        )
        if job_update.rowcount != 1:
            raise ImageAnalysisPersistenceError(
                "image_analysis_job_changed_during_publish"
            )

    def publish_image_analysis(
        self,
        *,
        job_id: str,
        runtime_generation: str,
        expected_attempt: int,
        result: Mapping[str, object],
        artifacts: Sequence[Mapping[str, object]] = (),
    ) -> dict[str, object]:
        """Atomically append result/head/change/receipt and a projection checkpoint."""

        detached_result = dict(result)
        require_valid_image_analysis_result(detached_result)
        normalized_artifacts = self._normalize_image_analysis_artifacts(
            detached_result,
            artifacts,
        )
        binding = self.validate_image_analysis_job_admission(
            job_id,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        if not self._image_result_matches_binding(detached_result, binding):
            raise ImageAnalysisPersistenceError("image_analysis_result_binding_mismatch")
        result_stages = detached_result.get("stages")
        if type(result_stages) is not dict:
            raise ImageAnalysisPersistenceError("image_analysis_result_not_publishable")
        expected_stage_outcomes: list[dict[str, object]] = [
            {
                "stage": "source_admission",
                "outcome": "succeeded",
                "outcome_sha256": canonical_job_sha256(
                    {"source_binding_sha256": binding["source_binding_sha256"]}
                ),
                "reason_code": None,
            }
        ]
        for stage in ("metadata", "geocode", "vision", "embedding", "quality"):
            stage_value = result_stages.get(stage)
            if type(stage_value) is not dict:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_result_not_publishable"
                )
            outcome = stage_value.get("status")
            if outcome not in {
                "succeeded",
                "partial",
                "unsupported",
                "disabled",
            }:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_result_not_publishable"
                )
            expected_stage_outcomes.append(
                {
                    "stage": stage,
                    "outcome": outcome,
                    "outcome_sha256": canonical_job_sha256(stage_value),
                    "reason_code": stage_value.get("reason_code"),
                }
            )
        expected_prepublication_artifacts: list[dict[str, object]] = [
            {
                "stage": "source_admission",
                "name": "file_identity",
                "sha256": binding["file_identity_sha256"],
            },
            *(
                {
                    "stage": artifact["stage"],
                    "name": artifact["name"],
                    "sha256": artifact["artifact_sha256"],
                }
                for artifact in normalized_artifacts
            ),
        ]
        expected_prepublication_artifacts.sort(
            key=lambda item: (str(item["stage"]), str(item["name"]))
        )

        # Compatibility for the older low-level persistence tests and callers:
        # a pristine direct publication may seal the six supplied final
        # outcomes inside the *same* transaction as publication.  It cannot
        # resume or complete any non-empty checkpoint.  Production execution
        # uses the runner's durable stage-by-stage path above.
        legacy_direct_publish = binding.get("job_status") == "queued"
        expected_database_identity = (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        )
        with self._open_admitted_image_source(
            source_id=str(binding["source_id"]),
            expected_asset_id=str(binding["asset_id"]),
            expected_input_sha256=str(binding["input_asset_sha256"]),
        ) as pinned:
            with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
                if self._admit_image_database_identity(expected_database_identity) != expected_database_identity:
                    raise ImageAnalysisPersistenceError("image_database_scope_changed")
                meta = self._image_meta_in_transaction(connection)
                current = connection.execute(
                    """SELECT binding.*,job.kind AS job_kind,
                              job.status AS job_status,job.stage AS job_stage,
                              job.attempt AS job_attempt,
                              job.cancel_requested AS job_cancel_requested,
                              job.database_uuid AS job_database_uuid,
                              job.asset_id AS job_asset_id,
                              job.analysis_run_id AS job_analysis_run_id,
                              job.checkpoint_json,
                              authority.runtime_generation AS runtime_generation,
                              authority.database_uuid AS attempt_database_uuid,
                              authority.database_device AS attempt_database_device,
                              authority.database_inode AS attempt_database_inode,
                              authority.request_sha256 AS attempt_request_sha256,
                              authority.authority_sha256 AS attempt_authority_sha256,
                              state.state AS attempt_state,
                              state.heartbeat_sequence AS heartbeat_sequence,
                              run.revision AS run_revision,
                              run.input_asset_sha256 AS run_input_asset_sha256
                         FROM image_analysis_job_bindings binding
                         JOIN media_jobs job ON job.id=binding.job_id
                         JOIN image_analysis_attempt_authorities authority
                           ON authority.job_id=job.id AND authority.attempt=job.attempt
                         JOIN image_analysis_attempt_states state
                           ON state.job_id=authority.job_id
                          AND state.attempt=authority.attempt
                          AND state.authority_sha256=authority.authority_sha256
                         JOIN analysis_runs run ON run.id=binding.analysis_run_id
                        WHERE binding.job_id=?""",
                    (job_id,),
                ).fetchone()
                if current is None:
                    raise ImageAnalysisPersistenceError("image_analysis_job_missing")
                current_binding = dict(current)
                self._require_image_job_row_matches_binding(
                    current_binding,
                    runtime_generation=runtime_generation,
                    expected_attempt=expected_attempt,
                )
                if meta["database_uuid"] != current_binding["database_uuid"]:
                    raise ImageAnalysisPersistenceError("image_database_scope_changed")
                self._require_bound_image_source_in_transaction(
                    connection,
                    current_binding,
                )
                checkpoint = _parse_json(current_binding["checkpoint_json"])
                if checkpoint is None:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_checkpoint_corrupt"
                    )
                try:
                    require_valid_image_analysis_checkpoint(checkpoint)
                except ImageAnalysisJobContractError as exc:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_checkpoint_corrupt"
                    ) from exc
                source_stat = os.fstat(pinned.handle.fileno())
                if (
                    int(source_stat.st_dev) != int(current_binding["source_device"])
                    or int(source_stat.st_ino) != int(current_binding["source_inode"])
                    or int(source_stat.st_size) != int(current_binding["observed_size"])
                    or int(source_stat.st_mtime_ns)
                    != int(current_binding["observed_mtime_ns"])
                    or int(source_stat.st_ctime_ns)
                    != int(current_binding["observed_ctime_ns"])
                ):
                    raise ImageAnalysisPersistenceError("image_source_changed")
                replay = self._published_image_response_in_transaction(
                    connection,
                    job_id,
                )
                if replay is not None:
                    expected_published_checkpoint = (
                        self._require_exact_image_publication_replay_in_transaction(
                            connection,
                            job_id=job_id,
                            expected_attempt=expected_attempt,
                            current_binding=current_binding,
                            detached_result=detached_result,
                            normalized_artifacts=normalized_artifacts,
                            expected_stage_outcomes=expected_stage_outcomes,
                            expected_prepublication_artifacts=(
                                expected_prepublication_artifacts
                            ),
                            replay=replay,
                        )
                    )
                    if current_binding["job_stage"] == "shadow_projection":
                        if checkpoint != expected_published_checkpoint:
                            raise ImageAnalysisPersistenceError(
                                "image_analysis_checkpoint_result_mismatch"
                            )
                    elif current_binding["job_stage"] == "publish":
                        if (
                            checkpoint.get("job_id") != job_id
                            or checkpoint.get("attempt") != expected_attempt
                            or checkpoint.get("current_stage") != "publish"
                            or checkpoint.get("completed_stages")
                            != [
                                "source_admission",
                                "metadata",
                                "geocode",
                                "vision",
                                "embedding",
                                "quality",
                            ]
                            or checkpoint.get("stage_outcomes")
                            != expected_stage_outcomes
                            or checkpoint.get("artifact_digests")
                            != expected_prepublication_artifacts
                            or checkpoint.get("publish_binding") is not None
                        ):
                            raise ImageAnalysisPersistenceError(
                                "image_analysis_checkpoint_result_mismatch"
                            )
                        self._advance_recovered_image_publication_in_transaction(
                            connection,
                            job_id=job_id,
                            runtime_generation=runtime_generation,
                            expected_attempt=expected_attempt,
                            current_binding=current_binding,
                            expected_checkpoint=expected_published_checkpoint,
                            now=_utc_now_iso(),
                        )
                    else:
                        raise ImageAnalysisPersistenceError(
                            "image_analysis_checkpoint_result_mismatch"
                        )
                    after = os.fstat(pinned.handle.fileno())
                    if (
                        int(after.st_dev)
                        != int(current_binding["source_device"])
                        or int(after.st_ino)
                        != int(current_binding["source_inode"])
                        or int(after.st_size)
                        != int(current_binding["observed_size"])
                        or int(after.st_mtime_ns)
                        != int(current_binding["observed_mtime_ns"])
                        or int(after.st_ctime_ns)
                        != int(current_binding["observed_ctime_ns"])
                        or self._admit_image_database_identity(
                            expected_database_identity
                        )
                        != expected_database_identity
                    ):
                        raise ImageAnalysisPersistenceError(
                            "image_source_changed"
                        )
                    self._require_bound_image_source_in_transaction(
                        connection,
                        current_binding,
                    )
                    replay["replayed"] = True
                    return replay
                if legacy_direct_publish:
                    if (
                        current_binding["job_status"] != "queued"
                        or current_binding["job_stage"] != "queued"
                        or current_binding["attempt_state"] != "queued"
                        or checkpoint
                        != self._image_attempt_checkpoint(
                            job_id=job_id,
                            attempt=expected_attempt,
                        )
                    ):
                        raise ImageAnalysisPersistenceError(
                            "image_analysis_checkpoint_result_mismatch"
                        )
                    checkpoint["current_stage"] = "publish"
                    checkpoint["completed_stages"] = [
                        "source_admission",
                        "metadata",
                        "geocode",
                        "vision",
                        "embedding",
                        "quality",
                    ]
                    checkpoint["stage_outcomes"] = expected_stage_outcomes
                    checkpoint["artifact_digests"] = (
                        expected_prepublication_artifacts
                    )
                    require_valid_image_analysis_checkpoint(checkpoint)
                    claimed_at = _utc_now_iso()
                    claimed = connection.execute(
                        """UPDATE image_analysis_attempt_states
                              SET state='claimed',heartbeat_sequence=heartbeat_sequence+1,
                                  claimed_at=COALESCE(claimed_at,?),heartbeat_at=?
                            WHERE job_id=? AND attempt=? AND state='queued'
                              AND heartbeat_sequence=0 AND authority_sha256=?""",
                        (
                            claimed_at,
                            claimed_at,
                            job_id,
                            expected_attempt,
                            current_binding["attempt_authority_sha256"],
                        ),
                    )
                    if claimed.rowcount != 1:
                        raise ImageAnalysisPersistenceError(
                            "image_analysis_attempt_claim_conflict"
                        )
                    current_binding["attempt_state"] = "claimed"
                    current_binding["heartbeat_sequence"] = 1
                elif (
                    current_binding["job_status"] != "running"
                    or current_binding["job_stage"] != "publish"
                    or current_binding["attempt_state"] != "claimed"
                    or checkpoint.get("job_id") != job_id
                    or checkpoint.get("attempt") != expected_attempt
                    or checkpoint.get("current_stage") != "publish"
                    or checkpoint.get("completed_stages")
                    != [
                        "source_admission",
                        "metadata",
                        "geocode",
                        "vision",
                        "embedding",
                        "quality",
                    ]
                    or checkpoint.get("stage_outcomes")
                    != expected_stage_outcomes
                    or checkpoint.get("artifact_digests")
                    != expected_prepublication_artifacts
                    or checkpoint.get("publish_binding") is not None
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_checkpoint_result_mismatch"
                    )
                expected_prior = (
                    None
                    if current_binding["expected_head_state"] == "missing"
                    else {
                        "analysis_run_id": current_binding["expected_head_run_id"],
                        "revision": current_binding["expected_head_revision"],
                        "content_sha256": current_binding["expected_head_content_sha256"],
                    }
                )
                observed_head = self._expected_image_head_in_transaction(
                    connection,
                    str(current_binding["asset_id"]),
                )
                if observed_head != expected_prior:
                    raise ImageAnalysisPersistenceError("image_analysis_head_conflict")
                now = _utc_now_iso()
                run_update = connection.execute(
                    """UPDATE analysis_runs
                          SET status='succeeded',transcript_status='unavailable',
                              visual_status='ready',finished_at=?
                        WHERE id=? AND asset_id=? AND revision=?
                          AND input_asset_sha256=?
                          AND status IN ('queued','running')""",
                    (
                        now,
                        current_binding["analysis_run_id"],
                        current_binding["asset_id"],
                        current_binding["run_revision"],
                        current_binding["input_asset_sha256"],
                    ),
                )
                if run_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError("image_analysis_run_not_publishable")
                connection.execute(
                    """INSERT INTO image_analysis_results(
                           asset_id,revision,analysis_run_id,job_id,schema_version,
                           input_asset_sha256,source_id,source_binding_sha256,
                           analysis_profile_id,analysis_profile_version,
                           analysis_profile_sha256,parent_analysis_run_id,
                           parent_revision,parent_content_sha256,result_json,
                           content_sha256,published_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        current_binding["asset_id"],
                        current_binding["intended_revision"],
                        current_binding["analysis_run_id"],
                        job_id,
                        IMAGE_ANALYSIS_RESULT_SCHEMA_VERSION,
                        current_binding["input_asset_sha256"],
                        current_binding["source_id"],
                        current_binding["source_binding_sha256"],
                        current_binding["analysis_profile_id"],
                        current_binding["analysis_profile_version"],
                        current_binding["analysis_profile_sha256"],
                        expected_prior["analysis_run_id"] if expected_prior else None,
                        expected_prior["revision"] if expected_prior else None,
                        expected_prior["content_sha256"] if expected_prior else None,
                        canonical_image_analysis_result_json(detached_result),
                        detached_result["content_sha256"],
                        now,
                    ),
                )
                for artifact in normalized_artifacts:
                    connection.execute(
                        """INSERT INTO image_analysis_artifacts(
                               asset_id,analysis_run_id,stage,name,media_type,signal,
                               model_id,dimensions,artifact_sha256,size_bytes,
                               artifact_blob,published_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            current_binding["asset_id"],
                            current_binding["analysis_run_id"],
                            artifact["stage"],
                            artifact["name"],
                            artifact["media_type"],
                            artifact["signal"],
                            artifact["model_id"],
                            artifact["dimensions"],
                            artifact["artifact_sha256"],
                            artifact["size_bytes"],
                            artifact["bytes"],
                            now,
                        ),
                    )
                if expected_prior is None:
                    head_update = connection.execute(
                        """INSERT INTO image_analysis_heads(
                               asset_id,analysis_run_id,revision,content_sha256,updated_at)
                           SELECT ?,?,?,?,?
                            WHERE NOT EXISTS(
                              SELECT 1 FROM image_analysis_heads WHERE asset_id=?)""",
                        (
                            current_binding["asset_id"],
                            current_binding["analysis_run_id"],
                            current_binding["intended_revision"],
                            detached_result["content_sha256"],
                            now,
                            current_binding["asset_id"],
                        ),
                    )
                else:
                    head_update = connection.execute(
                        """UPDATE image_analysis_heads
                              SET analysis_run_id=?,revision=?,content_sha256=?,updated_at=?
                            WHERE asset_id=? AND analysis_run_id=? AND revision=?
                              AND content_sha256=?""",
                        (
                            current_binding["analysis_run_id"],
                            current_binding["intended_revision"],
                            detached_result["content_sha256"],
                            now,
                            current_binding["asset_id"],
                            expected_prior["analysis_run_id"],
                            expected_prior["revision"],
                            expected_prior["content_sha256"],
                        ),
                    )
                if head_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError("image_analysis_head_conflict")
                position_row = connection.execute(
                    "SELECT COALESCE(MAX(position),0)+1 AS position FROM image_projection_changes"
                ).fetchone()
                assert position_row is not None
                change_position = int(position_row["position"])
                change = seal_projection_change(
                    {
                        "object": "memolens.image_projection_change",
                        "schema_version": "1",
                        "projection_contract": "legacy-image-index-shadow/v1",
                        "database_uuid": meta["database_uuid"],
                        "change_position": change_position,
                        "asset_id": current_binding["asset_id"],
                        "analysis_binding": {
                            "analysis_run_id": current_binding["analysis_run_id"],
                            "revision": current_binding["intended_revision"],
                            "content_sha256": detached_result["content_sha256"],
                        },
                        "source_binding_sha256": current_binding["source_binding_sha256"],
                        "operation": "upsert",
                    }
                )
                require_valid_projection_change(change)
                connection.execute(
                    """INSERT INTO image_projection_changes(
                           position,change_id,database_uuid,projection_contract,
                           asset_id,analysis_run_id,revision,content_sha256,
                           source_id,source_binding_sha256,operation,change_json,
                           change_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        change_position,
                        f"ichange_{str(change['change_sha256'])[:24]}",
                        meta["database_uuid"],
                        change["projection_contract"],
                        current_binding["asset_id"],
                        current_binding["analysis_run_id"],
                        current_binding["intended_revision"],
                        detached_result["content_sha256"],
                        current_binding["source_id"],
                        current_binding["source_binding_sha256"],
                        change["operation"],
                        canonical_projection_change_json(change),
                        change["change_sha256"],
                        now,
                    ),
                )
                publish_receipt = seal_publish_receipt(
                    {
                        "object": "memolens.image_publish_receipt",
                        "schema_version": "1",
                        "database_uuid": meta["database_uuid"],
                        "job_id": job_id,
                        "request_sha256": current_binding["request_sha256"],
                        "asset_id": current_binding["asset_id"],
                        "source_binding_sha256": current_binding["source_binding_sha256"],
                        "expected_prior_head": expected_prior,
                        "result_head": change["analysis_binding"],
                        "change_position": change_position,
                        "change_sha256": change["change_sha256"],
                    }
                )
                connection.execute(
                    """INSERT INTO image_publish_receipts(
                           job_id,database_uuid,asset_id,analysis_run_id,revision,
                           content_sha256,change_position,request_sha256,
                           publish_receipt_json,publish_receipt_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        meta["database_uuid"],
                        current_binding["asset_id"],
                        current_binding["analysis_run_id"],
                        current_binding["intended_revision"],
                        detached_result["content_sha256"],
                        change_position,
                        current_binding["request_sha256"],
                        canonical_publish_receipt_json(publish_receipt),
                        publish_receipt["publish_receipt_sha256"],
                        now,
                    ),
                )
                publication_binding = {
                    "analysis_run_id": current_binding["analysis_run_id"],
                    "revision": current_binding["intended_revision"],
                    "content_sha256": detached_result["content_sha256"],
                    "change_position": change_position,
                    "publish_receipt_sha256": publish_receipt["publish_receipt_sha256"],
                    "projection_receipt_sha256": None,
                }
                stage_outcomes = [*expected_stage_outcomes]
                stage_outcomes.append(
                    {
                        "stage": "publish",
                        "outcome": "succeeded",
                        "outcome_sha256": canonical_job_sha256(publication_binding),
                        "reason_code": None,
                    }
                )
                artifact_digests: list[dict[str, object]] = [
                    *expected_prepublication_artifacts,
                    {
                        "stage": "publish",
                        "name": "change",
                        "sha256": change["change_sha256"],
                    },
                    {
                        "stage": "publish",
                        "name": "receipt",
                        "sha256": publish_receipt["publish_receipt_sha256"],
                    },
                ]
                artifact_digests.sort(key=lambda item: (str(item["stage"]), str(item["name"])))
                checkpoint["current_stage"] = "shadow_projection"
                checkpoint["completed_stages"] = [
                    *checkpoint["completed_stages"],
                    "publish",
                ]
                checkpoint["stage_outcomes"] = stage_outcomes
                checkpoint["artifact_digests"] = artifact_digests
                checkpoint["publish_binding"] = publication_binding
                require_valid_image_analysis_checkpoint(checkpoint)
                state_update = connection.execute(
                    """UPDATE image_analysis_attempt_states
                          SET heartbeat_sequence=heartbeat_sequence+1,heartbeat_at=?
                        WHERE job_id=? AND attempt=? AND state='claimed'
                          AND heartbeat_sequence=? AND authority_sha256=?
                          AND EXISTS(
                            SELECT 1 FROM image_analysis_attempt_authorities authority
                             WHERE authority.job_id=? AND authority.attempt=?
                               AND authority.authority_sha256=?
                               AND authority.runtime_generation=?)""",
                    (
                        now,
                        job_id,
                        expected_attempt,
                        current_binding["heartbeat_sequence"],
                        current_binding["attempt_authority_sha256"],
                        job_id,
                        expected_attempt,
                        current_binding["attempt_authority_sha256"],
                        runtime_generation,
                    ),
                )
                if state_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_checkpoint_conflict"
                    )
                job_update = connection.execute(
                    """UPDATE media_jobs
                          SET status='running',stage='shadow_projection',progress=0.9,
                              checkpoint_json=?,heartbeat_at=?
                        WHERE id=? AND attempt=? AND status IN ('queued','running')
                          AND cancel_requested=0""",
                    (
                        canonical_job_json(checkpoint),
                        now,
                        job_id,
                        expected_attempt,
                    ),
                )
                if job_update.rowcount != 1:
                    raise ImageAnalysisPersistenceError("image_analysis_job_changed_during_publish")
                after = os.fstat(pinned.handle.fileno())
                if (
                    int(after.st_dev) != int(current_binding["source_device"])
                    or int(after.st_ino) != int(current_binding["source_inode"])
                    or int(after.st_size) != int(current_binding["observed_size"])
                    or int(after.st_mtime_ns) != int(current_binding["observed_mtime_ns"])
                    or int(after.st_ctime_ns) != int(current_binding["observed_ctime_ns"])
                    or self._admit_image_database_identity(expected_database_identity) != expected_database_identity
                ):
                    raise ImageAnalysisPersistenceError("image_source_changed")
                self._require_bound_image_source_in_transaction(
                    connection,
                    current_binding,
                )
        return {
            "job_id": job_id,
            "result": detached_result,
            "publish_receipt": publish_receipt,
            "change_position": change_position,
            "projection_status": "pending",
            "replayed": False,
        }

    @classmethod
    def _finalize_image_projection_job_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        receipt: Mapping[str, object],
        now: str,
    ) -> None:
        job = connection.execute(
            """SELECT attempt,status,stage,checkpoint_json
                 FROM media_jobs WHERE id=? AND kind='image_analysis'""",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        if job["status"] == "succeeded":
            return
        checkpoint = _parse_json(job["checkpoint_json"])
        if checkpoint is None:
            raise ImageAnalysisPersistenceError("image_analysis_checkpoint_corrupt")
        require_valid_image_analysis_checkpoint(checkpoint)
        if (
            job["status"] != "running"
            or job["stage"] != "shadow_projection"
            or checkpoint.get("attempt") != job["attempt"]
            or checkpoint.get("current_stage") != "shadow_projection"
            or checkpoint.get("completed_stages")
            != [
                "source_admission",
                "metadata",
                "geocode",
                "vision",
                "embedding",
                "quality",
                "publish",
            ]
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        outcome = receipt.get("outcome")
        if outcome not in {"applied", "no_change", "superseded"}:
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        stage_outcome = "partial" if outcome == "superseded" else "succeeded"
        stage_reason = receipt.get("reason_code") if outcome == "superseded" else None
        if outcome == "superseded" and stage_reason != "newer_head_published":
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        publish_binding = checkpoint.get("publish_binding")
        stage_outcomes = checkpoint.get("stage_outcomes")
        artifact_digests = checkpoint.get("artifact_digests")
        if (
            type(publish_binding) is not dict
            or type(stage_outcomes) is not list
            or type(artifact_digests) is not list
        ):
            raise ImageAnalysisPersistenceError("image_analysis_checkpoint_corrupt")
        publish_binding["projection_receipt_sha256"] = receipt["receipt_sha256"]
        checkpoint["current_stage"] = "completed"
        checkpoint["completed_stages"] = [
            *checkpoint["completed_stages"],
            "shadow_projection",
        ]
        stage_outcomes.append(
            {
                "stage": "shadow_projection",
                "outcome": stage_outcome,
                "outcome_sha256": canonical_job_sha256(receipt),
                "reason_code": stage_reason,
            }
        )
        artifact_digests.append(
            {
                "stage": "shadow_projection",
                "name": "projection_receipt",
                "sha256": receipt["receipt_sha256"],
            }
        )
        artifact_digests.sort(
            key=lambda item: (str(item["stage"]), str(item["name"]))
        )
        require_valid_image_analysis_checkpoint(checkpoint)
        updated = connection.execute(
            """UPDATE media_jobs SET status='succeeded',stage='completed',progress=1,
                      checkpoint_json=?,error_json=NULL,heartbeat_at=?,finished_at=?
                WHERE id=? AND attempt=? AND status='running'
                  AND stage='shadow_projection' AND cancel_requested=0""",
            (
                canonical_job_json(checkpoint),
                now,
                now,
                job_id,
                job["attempt"],
            ),
        )
        if updated.rowcount != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        state = connection.execute(
            """UPDATE image_analysis_attempt_states
                  SET state='finished',heartbeat_at=?,finished_at=?
                WHERE job_id=? AND attempt=? AND state='claimed'""",
            (now, now, job_id, job["attempt"]),
        )
        if state.rowcount != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")

    @classmethod
    def _fail_blocked_image_projection_job_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        receipt: Mapping[str, object],
        now: str,
    ) -> None:
        """Close a deterministic projection block without rewriting its published run."""

        job = connection.execute(
            """SELECT attempt,status,stage,checkpoint_json
                 FROM media_jobs WHERE id=? AND kind='image_analysis'""",
            (job_id,),
        ).fetchone()
        if job is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        if job["status"] == "failed":
            return
        checkpoint = _parse_json(job["checkpoint_json"])
        if checkpoint is None:
            raise ImageAnalysisPersistenceError("image_analysis_checkpoint_corrupt")
        require_valid_image_analysis_checkpoint(checkpoint)
        if (
            job["status"] != "running"
            or job["stage"] != "shadow_projection"
            or checkpoint.get("attempt") != job["attempt"]
            or checkpoint.get("current_stage") != "shadow_projection"
            or receipt.get("outcome") != "blocked"
            or type(receipt.get("reason_code")) is not str
        ):
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        reason_code = str(receipt["reason_code"])
        error = cls._image_attempt_error(
            code="projection_failed",
            stage="shadow_projection",
            retryable=False,
            detail=reason_code,
        )
        updated = connection.execute(
            """UPDATE media_jobs SET status='failed',error_json=?,heartbeat_at=?,
                      finished_at=?
                WHERE id=? AND attempt=? AND status='running'
                  AND stage='shadow_projection' AND cancel_requested=0""",
            (
                canonical_job_json(error),
                now,
                now,
                job_id,
                job["attempt"],
            ),
        )
        if updated.rowcount != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")
        state = connection.execute(
            """UPDATE image_analysis_attempt_states
                  SET state='finished',heartbeat_at=?,finished_at=?
                WHERE job_id=? AND attempt=? AND state='claimed'""",
            (now, now, job_id, job["attempt"]),
        )
        if state.rowcount != 1:
            raise ImageAnalysisPersistenceError("image_analysis_projection_job_changed")

    def mark_image_source_unavailable(
        self,
        *,
        source_id: str,
        expected_asset_id: str,
        expected_analysis_binding: Mapping[str, object],
        expected_source_updated_at: str,
        availability: str,
    ) -> dict[str, object]:
        """Atomically mark one exact current image source terminal and append remove."""

        if type(source_id) is not str or not source_id:
            raise ImageAnalysisPersistenceError("image_source_removal_source_invalid")
        if type(expected_asset_id) is not str or not expected_asset_id:
            raise ImageAnalysisPersistenceError("image_source_removal_asset_invalid")
        expected_head = _require_image_source_removal_binding(
            dict(expected_analysis_binding)
            if isinstance(expected_analysis_binding, Mapping)
            else expected_analysis_binding
        )
        if (
            type(expected_source_updated_at) is not str
            or not expected_source_updated_at
        ):
            raise ImageAnalysisPersistenceError(
                "image_source_removal_source_invalid"
            )
        availability = _require_image_source_terminal_availability(availability)
        expected_database_identity = self._admit_image_database_identity()
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            source = connection.execute(
                """SELECT id,asset_id,availability,updated_at
                     FROM asset_sources WHERE id=?""",
                (source_id,),
            ).fetchone()
            if source is None:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_source_changed"
                )
            if source["asset_id"] != expected_asset_id:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_asset_changed"
                )
            current_head = self._expected_image_head_in_transaction(
                connection,
                expected_asset_id,
            )
            if current_head != expected_head:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_head_changed"
                )
            current_result = connection.execute(
                """SELECT job_id,source_id FROM image_analysis_results
                    WHERE asset_id=? AND analysis_run_id=? AND revision=?
                      AND content_sha256=?""",
                (
                    expected_asset_id,
                    expected_head["analysis_run_id"],
                    expected_head["revision"],
                    expected_head["content_sha256"],
                ),
            ).fetchone()
            if current_result is None or current_result["source_id"] != source_id:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_source_changed"
                )
            publication = self._published_image_response_in_transaction(
                connection,
                str(current_result["job_id"]),
            )
            publication_result = (
                publication.get("result") if publication is not None else None
            )
            publication_change = (
                publication.get("change") if publication is not None else None
            )
            if (
                type(publication_result) is not dict
                or type(publication_change) is not dict
                or publication_result.get("source_binding", {}).get("source_id")
                != source_id
                or publication_change.get("analysis_binding") != expected_head
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_publication_corrupt"
                )
            latest = self._verified_latest_image_projection_change_in_transaction(
                connection,
                asset_id=expected_asset_id,
            )
            exact_remove = bool(
                latest is not None
                and latest["database_uuid"] == meta["database_uuid"]
                and latest["source_id"] == source_id
                and latest["operation"] == "remove"
                and latest["source_binding_sha256"] is None
                and latest["change"]["analysis_binding"] == expected_head
            )
            if exact_remove:
                if (
                    source["availability"] != availability
                    or source["updated_at"] != latest["created_at"]
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_source_removal_source_changed"
                    )
                self._admit_image_database_identity(expected_database_identity)
                assert latest is not None
                return {
                    "asset_id": expected_asset_id,
                    "source_id": source_id,
                    "availability": availability,
                    "analysis_binding": expected_head,
                    "change_position": int(latest["position"]),
                    "change": latest["change"],
                    "replayed": True,
                }
            if source["updated_at"] != expected_source_updated_at:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_source_changed"
                )
            transitioned = connection.execute(
                """UPDATE asset_sources
                      SET availability=?,last_verified_at=?,updated_at=?
                    WHERE id=? AND asset_id=? AND updated_at=?""",
                (
                    availability,
                    now,
                    now,
                    source_id,
                    expected_asset_id,
                    expected_source_updated_at,
                ),
            )
            if transitioned.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_source_changed"
                )
            position_row = connection.execute(
                """SELECT COALESCE(MAX(position),0)+1 AS position
                     FROM image_projection_changes"""
            ).fetchone()
            assert position_row is not None
            change_position = int(position_row["position"])
            change = seal_projection_change(
                {
                    "object": "memolens.image_projection_change",
                    "schema_version": "1",
                    "projection_contract": IMAGE_PROJECTION_CONTRACT,
                    "database_uuid": meta["database_uuid"],
                    "change_position": change_position,
                    "asset_id": expected_asset_id,
                    "analysis_binding": expected_head,
                    "source_binding_sha256": None,
                    "operation": "remove",
                }
            )
            require_valid_projection_change(change)
            connection.execute(
                """INSERT INTO image_projection_changes(
                       position,change_id,database_uuid,projection_contract,
                       asset_id,analysis_run_id,revision,content_sha256,
                       source_id,source_binding_sha256,operation,change_json,
                       change_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    change_position,
                    f"ichange_{str(change['change_sha256'])[:24]}",
                    meta["database_uuid"],
                    change["projection_contract"],
                    expected_asset_id,
                    expected_head["analysis_run_id"],
                    expected_head["revision"],
                    expected_head["content_sha256"],
                    source_id,
                    None,
                    change["operation"],
                    canonical_projection_change_json(change),
                    change["change_sha256"],
                    now,
                ),
            )
            self._admit_image_database_identity(expected_database_identity)
        return {
            "asset_id": expected_asset_id,
            "source_id": source_id,
            "availability": availability,
            "analysis_binding": expected_head,
            "change_position": change_position,
            "change": change,
            "replayed": False,
        }

    @classmethod
    def _projection_head_is_removed_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        head: Mapping[str, object],
        database_uuid: str,
        high_water: int,
    ) -> bool:
        """Admit one head only through its latest fixed-H canonical change."""

        latest = cls._verified_latest_image_projection_change_in_transaction(
            connection,
            asset_id=str(head["asset_id"]),
            at_or_before=high_water,
        )
        expected_binding = {
            "analysis_run_id": str(head["analysis_run_id"]),
            "revision": int(head["revision"]),
            "content_sha256": str(head["content_sha256"]),
        }
        if (
            latest is None
            or latest["database_uuid"] != database_uuid
            or latest["source_id"] != head["source_id"]
            or latest["change"]["analysis_binding"] != expected_binding
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        if latest["operation"] == "remove":
            if latest["source_binding_sha256"] is not None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_corrupt"
                )
            return True
        if (
            latest["operation"] != "upsert"
            or latest["source_binding_sha256"] is None
            or head["availability"] != "available"
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            )
        return False

    @staticmethod
    def _require_completed_projection_job_state(
        binding: Mapping[str, object],
        *,
        outcome: object,
    ) -> None:
        blocked = (
            outcome == "blocked"
            and binding.get("job_status") == "failed"
            and binding.get("job_stage") == "shadow_projection"
        )
        completed = (
            outcome in {"applied", "no_change", "superseded"}
            and binding.get("job_status") == "succeeded"
            and binding.get("job_stage") == "completed"
        )
        if not (blocked or completed) or binding.get("attempt_state") != "finished":
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_job_changed"
            )

    def _image_projection_replay_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        binding: Mapping[str, object],
    ) -> dict[str, object] | None:
        publication = self._published_image_response_in_transaction(
            connection,
            job_id,
        )
        if publication is None:
            return None
        source = connection.execute(
            """SELECT source.relative_path
                 FROM image_analysis_results result
                 JOIN asset_sources source ON source.id=result.source_id
                WHERE result.job_id=?""",
            (job_id,),
        ).fetchone()
        receipt_row = connection.execute(
            """SELECT receipt.receipt_json,manifest.manifest_json,
                      receipt.generation_id
                 FROM image_projection_receipts receipt
                 LEFT JOIN image_projection_manifests manifest
                   ON manifest.generation_id=receipt.generation_id
                WHERE receipt.change_position=?""",
            (publication["change_position"],),
        ).fetchone()
        if receipt_row is None or source is None:
            return None
        receipt = _parse_json(receipt_row["receipt_json"])
        if receipt is None:
            return None
        try:
            require_valid_projection_receipt(receipt)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_corrupt"
            ) from exc
        publication_change = publication.get("change")
        if (
            receipt.get("outcome") in {"applied", "no_change"}
            and type(publication_change) is dict
        ):
            latest = self._verified_latest_image_projection_change_in_transaction(
                connection,
                asset_id=str(publication_change.get("asset_id") or ""),
            )
            if (
                latest is not None
                and latest["position"] > publication["change_position"]
                and latest["operation"] == "remove"
            ):
                # A canonical remove permanently supersedes the old upsert.
                # Merely toggling the source row back to ``available`` cannot
                # make the completed job binding replayable or resurrect it.
                raise ImageAnalysisPersistenceError(
                    "image_analysis_binding_corrupt"
                )
        projection = self._verified_projection_for_change_in_transaction(
            connection,
            publication=publication,
            source_relative_path=str(source["relative_path"]),
        )
        manifest = _parse_json(receipt_row["manifest_json"])
        if projection is None or receipt is None:
            return None
        outcome = receipt.get("outcome")
        self._require_completed_projection_job_state(binding, outcome=outcome)
        if outcome != "blocked" and manifest is None:
            return None
        return {
            "job_id": job_id,
            "change_position": publication["change_position"],
            "generation_id": receipt_row["generation_id"],
            "projection_receipt": receipt,
            "manifest": manifest,
            "replayed": True,
        }

    def _read_image_projection_replay(
        self,
        *,
        job_id: str,
        binding: Mapping[str, object],
        replay_identity: tuple[int, int],
    ) -> dict[str, object] | None:
        with self._open_admitted_image_source(
            source_id=str(binding["source_id"]),
            expected_asset_id=str(binding["asset_id"]),
            expected_input_sha256=str(binding["input_asset_sha256"]),
        ) as exact_source:
            exact_fields = (
                "source_id",
                "library_root_id",
                "relative_path",
                "source_device",
                "source_inode",
                "observed_size",
                "observed_mtime_ns",
                "observed_ctime_ns",
                "file_identity_sha256",
                "root_permission_fingerprint",
            )
            if any(
                exact_source.binding.get(key) != binding.get(key)
                for key in exact_fields
            ):
                raise ImageAnalysisPersistenceError("image_source_changed")
            with self.transaction() as connection:  # type: ignore[attr-defined]
                meta = self._image_meta_in_transaction(connection)
                if meta["database_uuid"] != binding["database_uuid"]:
                    raise ImageAnalysisPersistenceError(
                        "image_database_scope_changed"
                    )
                self._require_bound_image_source_in_transaction(connection, binding)
                replay = self._image_projection_replay_in_transaction(
                    connection,
                    job_id=job_id,
                    binding=binding,
                )
        if replay is not None:
            self._admit_image_database_identity(replay_identity)
        return replay

    @classmethod
    def _persist_blocked_full_read_cutover_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        target_position: int,
        generation_id: str,
        previous_generation_id: str | None,
        canonical_manifest: Mapping[str, object],
        read_manifest: Mapping[str, object],
        managed_ids: Sequence[str],
        now: str,
    ) -> dict[str, object]:
        """Persist a parity-failed candidate without consuming its change."""

        canonical_counts = canonical_manifest["counts"]
        assert type(canonical_counts) is dict
        connection.execute(
            """INSERT INTO image_projection_manifests(
                   generation_id,database_uuid,
                   canonical_high_water_position,compiler_id,
                   projector_version,eligible_count,projected_count,
                   alias_count,missing_count,unexpected_count,
                   mismatched_count,blocked_count,alias_set_sha256,
                   row_set_sha256,manifest_json,manifest_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                canonical_manifest["database_uuid"],
                canonical_manifest["canonical_high_water_mark"],
                canonical_manifest["compiler_version"],
                canonical_manifest["projector_version"],
                canonical_counts["eligible"],
                canonical_counts["projected"],
                canonical_counts["aliases"],
                canonical_counts["missing"],
                canonical_counts["unexpected"],
                canonical_counts["mismatched"],
                canonical_counts["blocked"],
                canonical_manifest["alias_set_sha256"],
                canonical_manifest["row_set_sha256"],
                canonical_projection_manifest_json(canonical_manifest),
                canonical_manifest["manifest_sha256"],
                now,
            ),
        )
        completed = connection.execute(
            """UPDATE image_projection_generations
                  SET status='complete',completed_at=?
                WHERE id=? AND status='building' AND is_active=0""",
            (now, generation_id),
        )
        if completed.rowcount != 1:
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_generation_complete_failed"
            )
        cls._insert_image_projection_read_manifest_in_transaction(
            connection,
            generation_id=generation_id,
            previous_generation_id=previous_generation_id,
            manifest=read_manifest,
            created_at=now,
        )
        if previous_generation_id is None:
            cls._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=[],
                managed_physical_ids=managed_ids,
                materialized_at=now,
            )
        else:
            cls._restore_projection_generation_physical_snapshot_in_transaction(
                connection,
                generation_id=previous_generation_id,
            )
        return {
            "job_id": job_id,
            "change_position": target_position,
            "generation_id": generation_id,
            "previous_generation_id": previous_generation_id,
            "projection_receipt": None,
            "manifest": dict(read_manifest),
            "canonical_manifest": dict(canonical_manifest),
            "is_active": False,
            "replayed": False,
        }

    @staticmethod
    def _active_image_projection_generation_id_in_transaction(
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
    ) -> str | None:
        active_rows = connection.execute(
            """SELECT id,status FROM image_projection_generations
                WHERE database_uuid=? AND projection_contract=?
                  AND is_active=1 LIMIT 2""",
            (database_uuid, IMAGE_PROJECTION_CONTRACT),
        ).fetchall()
        if len(active_rows) > 1 or (
            active_rows and active_rows[0]["status"] != "complete"
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_active_generation_invalid"
            )
        return str(active_rows[0]["id"]) if active_rows else None

    @classmethod
    def _projection_read_manifest_matches_canonical(
        cls,
        read_manifest: Mapping[str, object],
        canonical_manifest: Mapping[str, object],
    ) -> bool:
        return bool(
            cls._projection_manifest_is_clean(read_manifest)
            and canonical_projection_manifest_json(read_manifest)
            == canonical_projection_manifest_json(canonical_manifest)
            and read_manifest.get("manifest_sha256")
            == canonical_manifest.get("manifest_sha256")
        )

    def _require_projectable_image_analysis_binding(
        self,
        *,
        job_id: str,
        runtime_generation: str,
        expected_attempt: int,
    ) -> tuple[dict[str, object], tuple[int, int]]:
        binding = self.get_image_analysis_job_binding(job_id)  # type: ignore[attr-defined]
        if binding is None:
            raise ImageAnalysisPersistenceError("image_analysis_job_missing")
        replay_identity = self._admit_image_database_identity()
        if (
            binding.get("job_kind") != IMAGE_ANALYSIS_WORKER_KIND
            or binding.get("job_database_uuid") != binding.get("database_uuid")
            or binding.get("job_asset_id") != binding.get("asset_id")
            or binding.get("job_analysis_run_id")
            != binding.get("analysis_run_id")
            or binding.get("run_input_asset_sha256")
            != binding.get("input_asset_sha256")
        ):
            raise ImageAnalysisPersistenceError("image_analysis_binding_corrupt")
        if (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        ) != replay_identity:
            raise ImageAnalysisPersistenceError("image_database_scope_changed")
        if binding.get("runtime_generation") != runtime_generation:
            raise ImageAnalysisPersistenceError("image_runtime_generation_changed")
        if (
            type(expected_attempt) is not int
            or binding.get("job_attempt") != expected_attempt
        ):
            raise ImageAnalysisPersistenceError("image_analysis_attempt_changed")
        if bool(binding.get("job_cancel_requested")):
            raise ImageAnalysisPersistenceError("image_analysis_cancel_requested")
        return binding, replay_identity

    def project_image_analysis_change(
        self,
        *,
        job_id: str,
        runtime_generation: str,
        expected_attempt: int,
        projector_version: str = IMAGE_PROJECTOR_VERSION,
        compiler_id: str = "memolens.image-manifest/v1",
    ) -> dict[str, object]:
        """Rebuild and atomically activate one exact canonical projection generation."""

        binding, replay_identity = self._require_projectable_image_analysis_binding(
            job_id=job_id,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        replay = self._read_image_projection_replay(
            job_id=job_id,
            binding=binding,
            replay_identity=replay_identity,
        )
        if replay is not None:
            return replay
        self._require_image_job_row_matches_binding(
            binding,
            runtime_generation=runtime_generation,
            expected_attempt=expected_attempt,
        )
        if (
            binding["job_status"] != "running"
            or binding["job_stage"] != "shadow_projection"
        ):
            raise ImageAnalysisPersistenceError("image_analysis_job_not_projectable")
        expected_database_identity = (
            int(binding["database_device"]),
            int(binding["database_inode"]),
        )
        self._admit_image_database_identity(expected_database_identity)
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            if meta["database_uuid"] != binding["database_uuid"]:
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
            self._require_bound_image_source_in_transaction(connection, binding)
            publication = self._published_image_response_in_transaction(
                connection,
                job_id,
            )
            if publication is None:
                raise ImageAnalysisPersistenceError("image_analysis_publication_missing")
            target_position = int(publication["change_position"])
            existing = connection.execute(
                """SELECT receipt_json FROM image_projection_receipts
                    WHERE change_position=?""",
                (target_position,),
            ).fetchone()
            if existing is not None:
                receipt = _parse_json(existing["receipt_json"])
                if receipt is None:
                    raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
                require_valid_projection_receipt(receipt)
                return {
                    "job_id": job_id,
                    "change_position": target_position,
                    "projection_receipt": receipt,
                    "replayed": True,
                }
            high_water_row = connection.execute(
                "SELECT MAX(position) AS high_water FROM image_projection_changes"
            ).fetchone()
            high_water = int(high_water_row["high_water"] or 0)
            if high_water < target_position:
                raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")

            alias_count, alias_set_sha256 = self._image_alias_set_in_transaction(
                connection,
                str(meta["database_uuid"]),
            )
            heads = connection.execute(
                """SELECT head.asset_id,head.analysis_run_id,head.revision,
                          head.content_sha256,result.job_id,result.source_id,
                          result.result_json,source.library_root_id,
                          source.relative_path,source.availability,
                          binding.enqueue_scope,
                          binding.asset_id AS binding_asset_id,
                          binding.source_id AS binding_source_id,
                          binding.library_root_id AS binding_library_root_id,
                          binding.relative_path AS binding_relative_path,
                          binding.input_asset_sha256 AS binding_asset_sha256
                     FROM image_analysis_heads head
                     JOIN image_analysis_results result
                       ON result.asset_id=head.asset_id
                      AND result.analysis_run_id=head.analysis_run_id
                      AND result.revision=head.revision
                      AND result.content_sha256=head.content_sha256
                     JOIN asset_sources source ON source.id=result.source_id
                     JOIN image_analysis_job_bindings binding
                       ON binding.job_id=result.job_id
                    ORDER BY head.asset_id"""
            ).fetchall()
            if not heads:
                raise ImageAnalysisPersistenceError("image_analysis_projection_empty")
            projected_by_asset: dict[str, dict[str, object]] = {}
            manifest_entries: list[dict[str, object]] = []
            for head in heads:
                expected_head_binding = {
                    "analysis_run_id": str(head["analysis_run_id"]),
                    "revision": int(head["revision"]),
                    "content_sha256": str(head["content_sha256"]),
                }
                if self._projection_head_is_removed_in_transaction(
                    connection,
                    head=head,
                    database_uuid=str(meta["database_uuid"]),
                    high_water=high_water,
                ):
                    continue
                result = _parse_json(head["result_json"])
                if result is None:
                    raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
                source_projection = {
                    "filename": str(head["relative_path"]).rsplit("/", 1)[-1],
                    "relative_path": str(head["relative_path"]),
                }
                try:
                    require_valid_image_analysis_result(result)
                    artifacts = self._image_projection_artifacts_in_transaction(
                        connection,
                        asset_id=str(head["asset_id"]),
                        analysis_run_id=str(head["analysis_run_id"]),
                    )
                    document = render_projected_image_index_row(
                        result=result,
                        source_projection=source_projection,
                        artifacts=artifacts,
                        projector_version=projector_version,
                    )
                except (ImageAnalysisContractError, ImageProjectionRenderError) as exc:
                    reason_code = (
                        exc.code
                        if isinstance(exc, ImageProjectionRenderError)
                        else "image_projection_result_invalid"
                    )
                    publication_change = publication.get("change")
                    if (
                        type(publication_change) is not dict
                        or publication_change.get("asset_id") != head["asset_id"]
                    ):
                        raise ImageAnalysisPersistenceError(
                            "image_analysis_projection_blocked"
                        ) from exc
                    blocked_receipt = seal_projection_receipt(
                        {
                            "object": "memolens.image_projection_receipt",
                            "schema_version": "1",
                            "projection_contract": "legacy-image-index-shadow/v1",
                            "projector_version": projector_version,
                            "database_uuid": meta["database_uuid"],
                            "change_position": target_position,
                            "asset_id": str(head["asset_id"]),
                            "analysis_binding": publication_change["analysis_binding"],
                            "source_binding_sha256": publication_change[
                                "source_binding_sha256"
                            ],
                            "projected_row_sha256": None,
                            "alias_set_sha256": alias_set_sha256,
                            "outcome": "blocked",
                            "reason_code": reason_code,
                        }
                    )
                    connection.execute(
                        """INSERT INTO image_projection_receipts(
                               change_position,generation_id,projection_contract,
                               projector_version,database_uuid,asset_id,analysis_run_id,
                               revision,content_sha256,source_binding_sha256,
                               projected_row_sha256,alias_set_sha256,outcome,reason_code,
                               receipt_json,receipt_sha256,created_at)
                           VALUES(?,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            target_position,
                            blocked_receipt["projection_contract"],
                            blocked_receipt["projector_version"],
                            blocked_receipt["database_uuid"],
                            blocked_receipt["asset_id"],
                            publication_change["analysis_binding"]["analysis_run_id"],
                            publication_change["analysis_binding"]["revision"],
                            publication_change["analysis_binding"]["content_sha256"],
                            blocked_receipt["source_binding_sha256"],
                            blocked_receipt["projected_row_sha256"],
                            blocked_receipt["alias_set_sha256"],
                            blocked_receipt["outcome"],
                            blocked_receipt["reason_code"],
                            canonical_projection_receipt_json(blocked_receipt),
                            blocked_receipt["receipt_sha256"],
                            now,
                        ),
                    )
                    self._fail_blocked_image_projection_job_in_transaction(
                        connection,
                        job_id=job_id,
                        receipt=blocked_receipt,
                        now=now,
                    )
                    self._admit_image_database_identity(expected_database_identity)
                    return {
                        "job_id": job_id,
                        "change_position": target_position,
                        "generation_id": None,
                        "projection_receipt": blocked_receipt,
                        "manifest": None,
                        "replayed": False,
                    }
                # The renderer is a deterministic transformer, not an authority
                # source.  Recheck every identity-bearing output against the
                # canonical rows used by this transaction before persisting it.
                require_valid_projected_image_index_row(document)
                expected_analysis_binding = expected_head_binding
                self._require_exact_projected_head_binding(
                    head=head,
                    result=result,
                    document=document,
                    source_projection=source_projection,
                    expected_analysis_binding=expected_analysis_binding,
                    projector_version=projector_version,
                )
                analysis_binding = {
                    "analysis_run_id": str(head["analysis_run_id"]),
                    "revision": int(head["revision"]),
                    "content_sha256": str(head["content_sha256"]),
                }
                row_sha256 = str(document["row_sha256"])
                source_digest = str(document["source_binding_sha256"])
                projected_by_asset[str(head["asset_id"])] = {
                    "document": document,
                    "analysis_binding": analysis_binding,
                    "row_sha256": row_sha256,
                    "source_binding_sha256": source_digest,
                    "source_id": str(head["source_id"]),
                    "library_root_id": str(head["library_root_id"]),
                    "asset_sha256": str(result["asset_sha256"]),
                    "relative_path": str(head["relative_path"]),
                    "enqueue_scope": str(head["enqueue_scope"]),
                }
                manifest_entries.append(
                    {
                        "asset_id": str(head["asset_id"]),
                        "analysis_binding": analysis_binding,
                        "expected_row_sha256": row_sha256,
                        "actual_row_sha256": row_sha256,
                        "status": "matched",
                        "reason_code": None,
                    }
                )

            alias_count, alias_set_sha256 = (
                self._admit_legacy_projection_aliases_in_transaction(
                    connection,
                    database_uuid=str(meta["database_uuid"]),
                    projected_by_asset=projected_by_asset,
                    now=now,
                )
            )

            previous_generation_id = (
                self._active_image_projection_generation_id_in_transaction(
                    connection,
                    database_uuid=str(meta["database_uuid"]),
                )
            )

            generation_id = _identifier("ipgen")
            connection.execute(
                """INSERT INTO image_projection_generations(
                       id,database_uuid,projection_contract,
                       canonical_high_water_position,compiler_id,projector_version,
                       status,is_active,created_at,completed_at)
                   VALUES(?,?,'legacy-image-index-shadow/v1',?,?,?,
                          'building',0,?,NULL)""",
                (
                    generation_id,
                    meta["database_uuid"],
                    high_water,
                    compiler_id,
                    projector_version,
                    now,
                ),
            )
            for projected_asset_id in sorted(projected_by_asset):
                projected = projected_by_asset[projected_asset_id]
                projected_binding = projected["analysis_binding"]
                projected_document = projected["document"]
                assert type(projected_binding) is dict
                assert type(projected_document) is dict
                connection.execute(
                    """INSERT INTO image_projection_rows(
                           generation_id,asset_id,analysis_run_id,revision,
                           content_sha256,source_id,source_binding_sha256,row_json,
                           row_sha256,alias_set_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        projected_asset_id,
                        projected_binding["analysis_run_id"],
                        projected_binding["revision"],
                        projected_binding["content_sha256"],
                        projected["source_id"],
                        projected["source_binding_sha256"],
                        canonical_job_json(projected_document),
                        projected["row_sha256"],
                        alias_set_sha256,
                        now,
                    ),
                )
            managed_ids = self._managed_image_projection_physical_ids_in_transaction(
                connection,
                database_uuid=str(meta["database_uuid"]),
            )
            self._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=(projection_snapshot := [
                    {
                        "asset_id": asset_id,
                        "analysis_binding": projected["analysis_binding"],
                        "row_sha256": projected["row_sha256"],
                        "document": projected["document"],
                    }
                    for asset_id, projected in sorted(projected_by_asset.items())
                ]),
                managed_physical_ids=managed_ids,
                materialized_at=now,
            )

            canonical_manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=str(meta["database_uuid"]),
                canonical_high_water_mark=high_water,
                entries=manifest_entries,
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            provisional_read_manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=str(meta["database_uuid"]),
                canonical_high_water_mark=high_water,
                entries=(
                    self._projection_manifest_entries_for_full_physical_snapshot_in_transaction(
                        connection,
                        snapshot=projection_snapshot,
                    )
                ),
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            provisional_cutover_clean = (
                self._projection_read_manifest_matches_canonical(
                    provisional_read_manifest,
                    canonical_manifest,
                )
            )
            if not provisional_cutover_clean:
                response = self._persist_blocked_full_read_cutover_in_transaction(
                    connection,
                    job_id=job_id,
                    target_position=target_position,
                    generation_id=generation_id,
                    previous_generation_id=previous_generation_id,
                    canonical_manifest=canonical_manifest,
                    read_manifest=provisional_read_manifest,
                    managed_ids=managed_ids,
                    now=now,
                )
                if (
                    self._admit_image_database_identity(expected_database_identity)
                    != expected_database_identity
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_database_scope_changed"
                    )
                return response

            pending_changes = connection.execute(
                """SELECT change.* FROM image_projection_changes change
                     LEFT JOIN image_projection_receipts receipt
                       ON receipt.change_position=change.position
                    WHERE receipt.change_position IS NULL
                      AND change.position<=?
                    ORDER BY change.position""",
                (high_water,),
            ).fetchall()
            receipts_by_position: dict[int, dict[str, object]] = {}
            jobs_by_position: dict[int, str] = {}
            for change_row in pending_changes:
                change = _parse_json(change_row["change_json"])
                if change is None:
                    raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
                require_valid_projection_change(change)
                projected = projected_by_asset.get(str(change_row["asset_id"]))
                if projected is None:
                    raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
                exact_current = change["analysis_binding"] == projected["analysis_binding"]
                receipt = seal_projection_receipt(
                    {
                        "object": "memolens.image_projection_receipt",
                        "schema_version": "1",
                        "projection_contract": "legacy-image-index-shadow/v1",
                        "projector_version": projector_version,
                        "database_uuid": meta["database_uuid"],
                        "change_position": int(change_row["position"]),
                        "asset_id": str(change_row["asset_id"]),
                        "analysis_binding": change["analysis_binding"],
                        "source_binding_sha256": change["source_binding_sha256"],
                        "projected_row_sha256": projected["row_sha256"],
                        "alias_set_sha256": alias_set_sha256,
                        "outcome": "applied" if exact_current else "superseded",
                        "reason_code": None if exact_current else "newer_head_published",
                    }
                )
                connection.execute(
                    """INSERT INTO image_projection_receipts(
                           change_position,generation_id,projection_contract,
                           projector_version,database_uuid,asset_id,analysis_run_id,
                           revision,content_sha256,source_binding_sha256,
                           projected_row_sha256,alias_set_sha256,outcome,reason_code,
                           receipt_json,receipt_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        change_row["position"],
                        generation_id,
                        receipt["projection_contract"],
                        receipt["projector_version"],
                        receipt["database_uuid"],
                        receipt["asset_id"],
                        change_row["analysis_run_id"],
                        change_row["revision"],
                        change_row["content_sha256"],
                        receipt["source_binding_sha256"],
                        receipt["projected_row_sha256"],
                        receipt["alias_set_sha256"],
                        receipt["outcome"],
                        receipt["reason_code"],
                        canonical_projection_receipt_json(receipt),
                        receipt["receipt_sha256"],
                        now,
                    ),
                )
                receipt_position = int(change_row["position"])
                receipts_by_position[receipt_position] = receipt
                publish = connection.execute(
                    """SELECT job_id FROM image_publish_receipts
                        WHERE change_position=?""",
                    (receipt_position,),
                ).fetchone()
                if publish is not None:
                    jobs_by_position[receipt_position] = str(publish["job_id"])

            manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=str(meta["database_uuid"]),
                canonical_high_water_mark=high_water,
                entries=manifest_entries,
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            counts = manifest["counts"]
            assert type(counts) is dict
            connection.execute(
                """INSERT INTO image_projection_manifests(
                       generation_id,database_uuid,canonical_high_water_position,
                       compiler_id,projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,mismatched_count,
                       blocked_count,alias_set_sha256,row_set_sha256,manifest_json,
                       manifest_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    manifest["database_uuid"],
                    manifest["canonical_high_water_mark"],
                    manifest["compiler_version"],
                    manifest["projector_version"],
                    counts["eligible"],
                    counts["projected"],
                    counts["aliases"],
                    counts["missing"],
                    counts["unexpected"],
                    counts["mismatched"],
                    counts["blocked"],
                    manifest["alias_set_sha256"],
                    manifest["row_set_sha256"],
                    canonical_projection_manifest_json(manifest),
                    manifest["manifest_sha256"],
                    now,
                ),
            )
            connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=? WHERE id=?""",
                (now, generation_id),
            )
            read_manifest = (
                self._build_full_image_projection_read_manifest_in_transaction(
                    connection,
                    generation={
                        "database_uuid": str(meta["database_uuid"]),
                        "canonical_high_water_position": high_water,
                        "compiler_id": compiler_id,
                        "projector_version": projector_version,
                    },
                    snapshot=projection_snapshot,
                    canonical_manifest=manifest,
                )
            )
            if not self._projection_read_manifest_matches_canonical(
                read_manifest,
                manifest,
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_read_cutover_changed"
                )
            self._insert_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                previous_generation_id=previous_generation_id,
                manifest=read_manifest,
                created_at=now,
            )
            connection.execute(
                """UPDATE image_projection_generations SET is_active=0
                    WHERE database_uuid=? AND is_active=1 AND id!=?""",
                (meta["database_uuid"], generation_id),
            )
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete'""",
                (generation_id,),
            )
            if activated.rowcount != 1:
                raise ImageAnalysisPersistenceError("image_analysis_projection_activation_failed")
            for position, receipt in receipts_by_position.items():
                if receipt["outcome"] in {"applied", "no_change", "superseded"}:
                    published_job_id = jobs_by_position.get(position)
                    if published_job_id is not None:
                        self._finalize_image_projection_job_in_transaction(
                            connection,
                            job_id=published_job_id,
                            receipt=receipt,
                            now=now,
                        )
            target_receipt = receipts_by_position.get(target_position)
            if target_receipt is None:
                raise ImageAnalysisPersistenceError("image_analysis_projection_corrupt")
            if self._admit_image_database_identity(expected_database_identity) != expected_database_identity:
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
        return {
            "job_id": job_id,
            "change_position": target_position,
            "generation_id": generation_id,
            "previous_generation_id": previous_generation_id,
            "projection_receipt": target_receipt,
            "manifest": read_manifest,
            "canonical_manifest": manifest,
            "is_active": True,
            "replayed": False,
        }

    @classmethod
    def _canonical_projection_rebuild_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
        fixed_high_water: int,
        projector_version: str,
    ) -> list[dict[str, object]]:
        """Render a rebuild snapshot only from current canonical rows and artifacts."""

        heads = connection.execute(
            """SELECT head.asset_id,head.analysis_run_id,head.revision,
                      head.content_sha256,result.job_id,result.source_id,
                      source.library_root_id,source.relative_path,
                      source.availability AS source_availability,
                      root.status AS root_status,
                      root.permission_fingerprint AS current_root_fingerprint,
                      binding.database_uuid AS binding_database_uuid,
                      binding.asset_id AS binding_asset_id,
                      binding.source_id AS binding_source_id,
                      binding.library_root_id AS binding_library_root_id,
                      binding.relative_path AS binding_relative_path,
                      binding.input_asset_sha256 AS binding_asset_sha256,
                      binding.root_permission_fingerprint
                 FROM image_analysis_heads head
                 JOIN image_analysis_results result
                   ON result.asset_id=head.asset_id
                  AND result.analysis_run_id=head.analysis_run_id
                  AND result.revision=head.revision
                  AND result.content_sha256=head.content_sha256
                 JOIN image_analysis_job_bindings binding
                   ON binding.job_id=result.job_id
                 JOIN asset_sources source ON source.id=result.source_id
                 JOIN library_roots root ON root.id=source.library_root_id
                ORDER BY head.asset_id"""
        ).fetchall()
        if not heads:
            raise ImageAnalysisPersistenceError("image_analysis_projection_empty")
        current_high_water_row = connection.execute(
            "SELECT MAX(position) AS high_water FROM image_projection_changes"
        ).fetchone()
        if (
            current_high_water_row is None
            or type(current_high_water_row["high_water"]) is not int
            or fixed_high_water > current_high_water_row["high_water"]
        ):
            raise ImageAnalysisPersistenceError(
                "image_analysis_projection_rebuild_change_invalid"
            )
        current_high_water = int(current_high_water_row["high_water"])
        snapshot: list[dict[str, object]] = []
        for head in heads:
            publication = cls._published_image_response_in_transaction(
                connection,
                str(head["job_id"]),
            )
            if publication is None:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_publication_missing"
                )
            result = publication.get("result")
            change = publication.get("change")
            change_position = publication.get("change_position")
            if (
                type(result) is not dict
                or type(change) is not dict
                or type(change_position) is not int
                or change_position > fixed_high_water
                or result.get("asset_id") != head["asset_id"]
                or result.get("analysis_run_id") != head["analysis_run_id"]
                or result.get("revision") != head["revision"]
                or result.get("content_sha256") != head["content_sha256"]
                or result.get("source_binding", {}).get("source_id")
                != head["source_id"]
                or head["binding_database_uuid"] != database_uuid
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_rebuild_source_invalid"
                )
            latest_change = (
                cls._verified_latest_image_projection_change_in_transaction(
                    connection,
                    asset_id=str(head["asset_id"]),
                    at_or_before=fixed_high_water,
                )
            )
            if (
                latest_change is None
                or latest_change["database_uuid"] != database_uuid
                or latest_change["asset_id"] != head["asset_id"]
                or latest_change["analysis_run_id"] != head["analysis_run_id"]
                or latest_change["revision"] != head["revision"]
                or latest_change["content_sha256"] != head["content_sha256"]
                or latest_change["source_id"] != head["source_id"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_rebuild_change_invalid"
                )
            if latest_change["operation"] == "remove":
                if latest_change["source_binding_sha256"] is not None:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_projection_rebuild_change_invalid"
                    )
                continue
            if (
                latest_change["position"] != change_position
                or latest_change["source_binding_sha256"]
                != change.get("source_binding_sha256")
                or latest_change["operation"] != "upsert"
                or latest_change["change_sha256"]
                != change.get("change_sha256")
                or (
                    fixed_high_water == current_high_water
                    and (
                        head["source_availability"] != "available"
                        or head["root_status"] != "active"
                        or head["current_root_fingerprint"]
                        != head["root_permission_fingerprint"]
                    )
                )
            ):
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_rebuild_change_invalid"
                )
            source_projection = {
                "filename": str(head["relative_path"]).rsplit("/", 1)[-1],
                "relative_path": str(head["relative_path"]),
            }
            artifacts = cls._image_projection_artifacts_in_transaction(
                connection,
                asset_id=str(head["asset_id"]),
                analysis_run_id=str(head["analysis_run_id"]),
            )
            try:
                require_valid_image_analysis_result(result)
                document = render_projected_image_index_row(
                    result=result,
                    source_projection=source_projection,
                    artifacts=artifacts,
                    projector_version=projector_version,
                )
                require_valid_projected_image_index_row(document)
            except (ImageAnalysisContractError, ImageProjectionRenderError) as exc:
                raise ImageAnalysisPersistenceError(
                    "image_analysis_projection_rebuild_render_failed"
                ) from exc
            analysis_binding = {
                "analysis_run_id": str(head["analysis_run_id"]),
                "revision": int(head["revision"]),
                "content_sha256": str(head["content_sha256"]),
            }
            cls._require_exact_projected_head_binding(
                head=head,
                result=result,
                document=document,
                source_projection=source_projection,
                expected_analysis_binding=analysis_binding,
                projector_version=projector_version,
            )
            snapshot.append(
                {
                    "asset_id": str(head["asset_id"]),
                    "analysis_binding": analysis_binding,
                    "source_id": str(head["source_id"]),
                    "source_binding_sha256": str(
                        document["source_binding_sha256"]
                    ),
                    "source_relative_path": str(head["relative_path"]),
                    "result": result,
                    "document": document,
                    "row_sha256": str(document["row_sha256"]),
                    "change_position": change_position,
                }
            )
        return snapshot

    @staticmethod
    def _managed_image_projection_physical_ids_in_transaction(
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
    ) -> list[str]:
        """Return only IDs whose physical compatibility rows A0 may replace."""

        return [
            str(row["managed_id"])
            for row in connection.execute(
                """SELECT managed_id FROM (
                       SELECT projected.asset_id AS managed_id
                         FROM image_projection_rows projected
                         JOIN image_projection_generations generation
                           ON generation.id=projected.generation_id
                        WHERE generation.database_uuid=?
                       UNION
                       SELECT head.asset_id AS managed_id
                         FROM image_analysis_heads head
                       UNION
                       SELECT alias.legacy_id AS managed_id
                         FROM legacy_image_aliases alias
                        WHERE alias.database_uuid=?
                   ) ORDER BY managed_id""",
                (database_uuid, database_uuid),
            ).fetchall()
        ]

    @staticmethod
    def _physical_image_projection_matches_snapshot_in_transaction(
        connection: sqlite3.Connection,
        *,
        snapshot: Sequence[Mapping[str, object]],
        managed_physical_ids: Sequence[str],
        materialized_at: str,
    ) -> bool:
        """Check exact managed physical state while ignoring unmanaged legacy rows."""

        physical_ids = [
            str(row["id"])
            for row in connection.execute(
                "SELECT id FROM image_index ORDER BY id"
            ).fetchall()
        ]
        managed_physical_id_set = set(managed_physical_ids)
        expected_ids = sorted(str(item["asset_id"]) for item in snapshot)
        if (
            sorted(
                physical_id
                for physical_id in physical_ids
                if physical_id in managed_physical_id_set
            )
            != expected_ids
        ):
            return False
        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        for item in snapshot:
            physical = connection.execute(
                f"SELECT {columns_sql} FROM image_index WHERE id=?",
                (item["asset_id"],),
            ).fetchone()
            document = item["document"]
            assert type(document) is dict
            expected = materialize_projected_image_index_row(
                document,
                created_at=materialized_at,
                updated_at=materialized_at,
            )
            if physical is None or any(
                physical[column] != expected[column]
                for column in IMAGE_INDEX_SQL_COLUMNS
            ):
                return False
        return True

    @staticmethod
    def _materialize_physical_image_projection_snapshot_in_transaction(
        connection: sqlite3.Connection,
        *,
        snapshot: Sequence[Mapping[str, object]],
        managed_physical_ids: Sequence[str],
        materialized_at: str,
    ) -> None:
        """Replace only A0-managed compatibility rows from canonical documents."""

        for managed_id in managed_physical_ids:
            connection.execute(
                "DELETE FROM image_index WHERE id=?",
                (managed_id,),
            )
        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders_sql = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        for item in snapshot:
            document = item["document"]
            assert type(document) is dict
            connection.execute(
                f"INSERT INTO image_index({columns_sql}) "
                f"VALUES({placeholders_sql})",
                materialize_projected_image_index_values(
                    document,
                    created_at=materialized_at,
                    updated_at=materialized_at,
                ),
            )

    @staticmethod
    def _physical_image_projection_semantically_matches_snapshot_in_transaction(
        connection: sqlite3.Connection,
        *,
        snapshot: Sequence[Mapping[str, object]],
        managed_physical_ids: Sequence[str],
        allow_managed_extras: bool,
    ) -> bool:
        """Verify canonical physical values without treating timestamps as facts."""

        managed_id_set = set(managed_physical_ids)
        physical_ids = {
            str(row["id"])
            for row in connection.execute("SELECT id FROM image_index").fetchall()
        }
        expected_ids = {str(item["asset_id"]) for item in snapshot}
        observed_managed = physical_ids & managed_id_set
        if not expected_ids.issubset(observed_managed) or (
            not allow_managed_extras and observed_managed != expected_ids
        ):
            return False
        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        for item in snapshot:
            physical = connection.execute(
                f"SELECT {columns_sql} FROM image_index WHERE id=?",
                (item["asset_id"],),
            ).fetchone()
            document = item["document"]
            assert type(document) is dict
            if (
                physical is None
                or type(physical["created_at"]) is not str
                or type(physical["updated_at"]) is not str
            ):
                return False
            expected = materialize_projected_image_index_row(
                document,
                created_at=str(physical["created_at"]),
                updated_at=str(physical["updated_at"]),
            )
            if any(
                physical[column] != expected[column]
                for column in IMAGE_INDEX_SQL_COLUMNS
            ):
                return False
        return True

    @staticmethod
    def _projection_manifest_entries_for_snapshot(
        snapshot: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        return [
            {
                "asset_id": item["asset_id"],
                "analysis_binding": item["analysis_binding"],
                "expected_row_sha256": item["row_sha256"],
                "actual_row_sha256": item["row_sha256"],
                "status": "matched",
                "reason_code": None,
            }
            for item in snapshot
        ]

    @staticmethod
    def _projection_manifest_is_clean(manifest: Mapping[str, object]) -> bool:
        counts = manifest.get("counts")
        return bool(
            type(counts) is dict
            and counts.get("eligible") == counts.get("projected")
            and all(
                counts.get(key) == 0
                for key in ("missing", "unexpected", "mismatched", "blocked")
            )
        )

    @staticmethod
    def _physical_image_index_semantic_sha256(row: Mapping[str, object]) -> str:
        """Hash one physical row without leaking values or envelope timestamps."""

        normalized: list[dict[str, object]] = []
        for column in IMAGE_INDEX_SQL_COLUMNS:
            if column in {"created_at", "updated_at"}:
                continue
            value = row[column]
            if value is None:
                encoded: dict[str, object] = {"type": "null"}
            elif type(value) is bytes:
                encoded = {
                    "type": "bytes",
                    "size": len(value),
                    "sha256": hashlib.sha256(value).hexdigest(),
                }
            elif type(value) is str:
                encoded = {"type": "text", "value": value}
            elif type(value) is int:
                encoded = {"type": "integer", "value": value}
            elif type(value) is float and math.isfinite(value):
                encoded = {"type": "real", "value": value}
            else:
                raise ImageAnalysisPersistenceError(
                    "image_projection_physical_row_invalid"
                )
            normalized.append({"column": column, "value": encoded})
        return canonical_sha256(
            {
                "object": "memolens.image_index_semantic_row_digest",
                "schema_version": "1",
                "columns": normalized,
            }
        )

    @staticmethod
    def _unexpected_projection_manifest_asset_id(
        physical_id: str,
        *,
        used_ids: set[str],
    ) -> str:
        if (
            len(physical_id) == 30
            and physical_id.startswith("asset_")
            and all(
                character in "0123456789abcdef"
                for character in physical_id[6:]
            )
            and physical_id not in used_ids
        ):
            return physical_id
        surrogate = "asset_" + hashlib.sha256(
            ("memolens.unexpected-image-index-id\0" + physical_id).encode("utf-8")
        ).hexdigest()[:24]
        if surrogate in used_ids:
            raise ImageAnalysisPersistenceError(
                "image_projection_unexpected_identity_collision"
            )
        return surrogate

    @classmethod
    def _projection_manifest_entries_for_full_physical_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        snapshot: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        """Compare the complete physical compatibility read set to canonical rows."""

        expected_by_id = {str(item["asset_id"]): item for item in snapshot}
        if len(expected_by_id) != len(snapshot):
            raise ImageAnalysisPersistenceError(
                "image_projection_snapshot_duplicate_asset"
            )
        columns_sql = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        physical_by_id = {
            str(row["id"]): row
            for row in connection.execute(
                f"SELECT {columns_sql} FROM image_index ORDER BY id"
            ).fetchall()
        }
        entries: list[dict[str, object]] = []
        used_ids = set(expected_by_id)
        for asset_id in sorted(expected_by_id):
            item = expected_by_id[asset_id]
            analysis_binding = item["analysis_binding"]
            expected_sha256 = str(item["row_sha256"])
            physical = physical_by_id.pop(asset_id, None)
            if physical is None:
                entries.append(
                    {
                        "asset_id": asset_id,
                        "analysis_binding": analysis_binding,
                        "expected_row_sha256": expected_sha256,
                        "actual_row_sha256": None,
                        "status": "missing",
                        "reason_code": "physical_projection_missing",
                    }
                )
                continue
            document = item["document"]
            assert type(document) is dict
            if (
                type(physical["created_at"]) is str
                and type(physical["updated_at"]) is str
            ):
                expected_physical = materialize_projected_image_index_row(
                    document,
                    created_at=str(physical["created_at"]),
                    updated_at=str(physical["updated_at"]),
                )
            else:
                expected_physical = {}
            if expected_physical and all(
                physical[column] == expected_physical[column]
                for column in IMAGE_INDEX_SQL_COLUMNS
            ):
                entries.append(
                    {
                        "asset_id": asset_id,
                        "analysis_binding": analysis_binding,
                        "expected_row_sha256": expected_sha256,
                        "actual_row_sha256": expected_sha256,
                        "status": "matched",
                        "reason_code": None,
                    }
                )
            else:
                actual_sha256 = cls._physical_image_index_semantic_sha256(
                    physical
                )
                if actual_sha256 == expected_sha256:
                    actual_sha256 = canonical_sha256(
                        {
                            "object": "memolens.image_index_mismatch_digest",
                            "schema_version": "1",
                            "physical_sha256": actual_sha256,
                        }
                    )
                entries.append(
                    {
                        "asset_id": asset_id,
                        "analysis_binding": analysis_binding,
                        "expected_row_sha256": expected_sha256,
                        "actual_row_sha256": actual_sha256,
                        "status": "mismatched",
                        "reason_code": "physical_projection_mismatch",
                    }
                )
        for physical_id in sorted(physical_by_id):
            manifest_asset_id = cls._unexpected_projection_manifest_asset_id(
                physical_id,
                used_ids=used_ids,
            )
            used_ids.add(manifest_asset_id)
            entries.append(
                {
                    "asset_id": manifest_asset_id,
                    "analysis_binding": None,
                    "expected_row_sha256": None,
                    "actual_row_sha256": cls._physical_image_index_semantic_sha256(
                        physical_by_id[physical_id]
                    ),
                    "status": "unexpected",
                    "reason_code": "unexpected_physical_projection_row",
                }
            )
        return entries

    @classmethod
    def _stored_projection_generation_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
    ) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
        """Cold-verify one immutable generation without consulting current heads."""

        generation_rows = connection.execute(
            """SELECT id,database_uuid,projection_contract,
                      canonical_high_water_position,compiler_id,
                      projector_version,status,is_active,created_at,completed_at
                 FROM image_projection_generations WHERE id=? LIMIT 2""",
            (generation_id,),
        ).fetchall()
        if len(generation_rows) != 1:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        generation = dict(generation_rows[0])
        if (
            generation["projection_contract"] != IMAGE_PROJECTION_CONTRACT
            or generation["status"] != "complete"
            or type(generation["canonical_high_water_position"]) is not int
            or int(generation["canonical_high_water_position"]) < 0
            or type(generation["compiler_id"]) is not str
            or not generation["compiler_id"]
            or type(generation["projector_version"]) is not str
            or not generation["projector_version"]
            or type(generation["created_at"]) is not str
            or not generation["created_at"]
            or type(generation["completed_at"]) is not str
            or not generation["completed_at"]
        ):
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )

        stored_rows = connection.execute(
            """SELECT asset_id,analysis_run_id,revision,content_sha256,
                      source_id,source_binding_sha256,row_json,row_sha256,
                      alias_set_sha256
                 FROM image_projection_rows
                WHERE generation_id=? ORDER BY asset_id""",
            (generation_id,),
        ).fetchall()
        snapshot: list[dict[str, object]] = []
        row_set: list[dict[str, object]] = []
        row_alias_digests: set[str] = set()
        for stored in stored_rows:
            document = _parse_json(stored["row_json"])
            if document is None:
                raise ImageAnalysisPersistenceError(
                    "image_projection_generation_snapshot_corrupt"
                )
            try:
                require_valid_projected_image_index_row(document)
            except ImageProjectionRenderError as exc:
                raise ImageAnalysisPersistenceError(
                    "image_projection_generation_snapshot_corrupt"
                ) from exc
            analysis_binding = {
                "analysis_run_id": str(stored["analysis_run_id"]),
                "revision": int(stored["revision"]),
                "content_sha256": str(stored["content_sha256"]),
            }
            if (
                document.get("asset_id") != stored["asset_id"]
                or document.get("analysis_binding") != analysis_binding
                or document.get("source_binding_sha256")
                != stored["source_binding_sha256"]
                or document.get("row_sha256") != stored["row_sha256"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_generation_snapshot_corrupt"
                )
            snapshot.append(
                {
                    "asset_id": str(stored["asset_id"]),
                    "analysis_binding": analysis_binding,
                    "source_id": str(stored["source_id"]),
                    "source_binding_sha256": str(
                        stored["source_binding_sha256"]
                    ),
                    "row_sha256": str(stored["row_sha256"]),
                    "document": document,
                }
            )
            row_set.append(
                {
                    "asset_id": str(stored["asset_id"]),
                    "projected_row_sha256": str(stored["row_sha256"]),
                }
            )
            row_alias_digests.add(str(stored["alias_set_sha256"]))

        alias_rows = connection.execute(
            """SELECT legacy_id,alias_sha256
                 FROM image_projection_aliases
                WHERE generation_id=? ORDER BY legacy_id""",
            (generation_id,),
        ).fetchall()
        alias_set = [
            {
                "legacy_id": str(alias["legacy_id"]),
                "alias_sha256": str(alias["alias_sha256"]),
            }
            for alias in alias_rows
        ]
        alias_set_sha256 = canonical_sha256(alias_set)
        if row_alias_digests not in (set(), {alias_set_sha256}):
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )

        manifest_row = connection.execute(
            """SELECT database_uuid,canonical_high_water_position,
                      compiler_id,projector_version,eligible_count,
                      projected_count,alias_count,missing_count,
                      unexpected_count,mismatched_count,blocked_count,
                      alias_set_sha256,row_set_sha256,manifest_json,
                      manifest_sha256
                 FROM image_projection_manifests WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()
        manifest = (
            _parse_json(manifest_row["manifest_json"])
            if manifest_row is not None
            else None
        )
        if manifest_row is None or manifest is None:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        try:
            require_valid_projection_manifest(manifest)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            ) from exc
        counts = manifest.get("counts")
        expected_counts = {
            "eligible": len(snapshot),
            "projected": len(snapshot),
            "aliases": len(alias_set),
            "missing": 0,
            "unexpected": 0,
            "mismatched": 0,
            "blocked": 0,
        }
        if (
            counts != expected_counts
            or manifest.get("database_uuid") != generation["database_uuid"]
            or manifest.get("canonical_high_water_mark")
            != generation["canonical_high_water_position"]
            or manifest.get("compiler_version") != generation["compiler_id"]
            or manifest.get("projector_version")
            != generation["projector_version"]
            or manifest.get("alias_set_sha256") != alias_set_sha256
            or manifest.get("row_set_sha256") != canonical_sha256(row_set)
            or manifest.get("manifest_sha256")
            != manifest_row["manifest_sha256"]
            or canonical_projection_manifest_json(manifest)
            != manifest_row["manifest_json"]
            or manifest_row["database_uuid"] != generation["database_uuid"]
            or manifest_row["canonical_high_water_position"]
            != generation["canonical_high_water_position"]
            or manifest_row["compiler_id"] != generation["compiler_id"]
            or manifest_row["projector_version"]
            != generation["projector_version"]
            or manifest_row["eligible_count"] != expected_counts["eligible"]
            or manifest_row["projected_count"] != expected_counts["projected"]
            or manifest_row["alias_count"] != expected_counts["aliases"]
            or any(
                int(manifest_row[field] or 0) != 0
                for field in (
                    "missing_count",
                    "unexpected_count",
                    "mismatched_count",
                    "blocked_count",
                )
            )
            or manifest_row["alias_set_sha256"] != alias_set_sha256
            or manifest_row["row_set_sha256"] != canonical_sha256(row_set)
        ):
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        return generation, snapshot, manifest

    @staticmethod
    def _insert_image_projection_read_manifest_in_transaction(
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        previous_generation_id: str | None,
        manifest: Mapping[str, object],
        created_at: str,
    ) -> None:
        counts = manifest.get("counts")
        if type(counts) is not dict:
            raise ImageAnalysisPersistenceError(
                "image_projection_read_manifest_invalid"
            )
        try:
            require_valid_projection_manifest(manifest)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_projection_read_manifest_invalid"
            ) from exc
        connection.execute(
            """INSERT INTO image_projection_read_manifests(
                   generation_id,previous_generation_id,database_uuid,
                   canonical_high_water_position,compiler_id,projector_version,
                   eligible_count,projected_count,alias_count,missing_count,
                   unexpected_count,mismatched_count,blocked_count,
                   alias_set_sha256,row_set_sha256,manifest_json,
                   manifest_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                previous_generation_id,
                manifest["database_uuid"],
                manifest["canonical_high_water_mark"],
                manifest["compiler_version"],
                manifest["projector_version"],
                counts["eligible"],
                counts["projected"],
                counts["aliases"],
                counts["missing"],
                counts["unexpected"],
                counts["mismatched"],
                counts["blocked"],
                manifest["alias_set_sha256"],
                manifest["row_set_sha256"],
                canonical_projection_manifest_json(manifest),
                manifest["manifest_sha256"],
                created_at,
            ),
        )

    @classmethod
    def _require_image_projection_read_manifest_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        require_clean_canonical_match: bool,
    ) -> dict[str, object]:
        row = connection.execute(
            """SELECT read_manifest.previous_generation_id,
                      read_manifest.database_uuid,
                      read_manifest.canonical_high_water_position,
                      read_manifest.compiler_id,read_manifest.projector_version,
                      read_manifest.eligible_count,read_manifest.projected_count,
                      read_manifest.alias_count,read_manifest.missing_count,
                      read_manifest.unexpected_count,
                      read_manifest.mismatched_count,read_manifest.blocked_count,
                      read_manifest.alias_set_sha256,
                      read_manifest.row_set_sha256,
                      read_manifest.manifest_json,
                      read_manifest.manifest_sha256,
                      canonical.manifest_json AS canonical_manifest_json,
                      canonical.manifest_sha256 AS canonical_manifest_sha256,
                      generation.database_uuid AS generation_database_uuid,
                      generation.projection_contract AS generation_contract,
                      generation.canonical_high_water_position AS generation_high_water,
                      generation.compiler_id AS generation_compiler_id,
                      generation.projector_version AS generation_projector_version,
                      generation.status AS generation_status,
                      predecessor.database_uuid AS predecessor_database_uuid,
                      predecessor.projection_contract AS predecessor_contract,
                      predecessor.status AS predecessor_status
                 FROM image_projection_read_manifests read_manifest
                 JOIN image_projection_manifests canonical
                   ON canonical.generation_id=read_manifest.generation_id
                 JOIN image_projection_generations generation
                   ON generation.id=read_manifest.generation_id
            LEFT JOIN image_projection_generations predecessor
                   ON predecessor.id=read_manifest.previous_generation_id
                WHERE read_manifest.generation_id=?""",
            (generation_id,),
        ).fetchone()
        manifest = _parse_json(row["manifest_json"]) if row is not None else None
        if row is None or manifest is None:
            raise ImageAnalysisPersistenceError(
                "image_projection_read_manifest_corrupt"
            )
        try:
            require_valid_projection_manifest(manifest)
        except ImageAnalysisContractError as exc:
            raise ImageAnalysisPersistenceError(
                "image_projection_read_manifest_corrupt"
            ) from exc
        counts = manifest.get("counts")
        stored_counts = {
            "eligible": row["eligible_count"],
            "projected": row["projected_count"],
            "aliases": row["alias_count"],
            "missing": row["missing_count"],
            "unexpected": row["unexpected_count"],
            "mismatched": row["mismatched_count"],
            "blocked": row["blocked_count"],
        }
        if (
            counts != stored_counts
            or row["generation_database_uuid"] != row["database_uuid"]
            or row["generation_contract"] != IMAGE_PROJECTION_CONTRACT
            or row["generation_high_water"]
            != row["canonical_high_water_position"]
            or row["generation_compiler_id"] != row["compiler_id"]
            or row["generation_projector_version"]
            != row["projector_version"]
            or row["generation_status"] != "complete"
            or (
                row["previous_generation_id"] is not None
                and (
                    row["previous_generation_id"] == generation_id
                    or row["predecessor_database_uuid"] != row["database_uuid"]
                    or row["predecessor_contract"] != IMAGE_PROJECTION_CONTRACT
                    or row["predecessor_status"] != "complete"
                )
            )
            or manifest.get("database_uuid") != row["database_uuid"]
            or manifest.get("canonical_high_water_mark")
            != row["canonical_high_water_position"]
            or manifest.get("compiler_version") != row["compiler_id"]
            or manifest.get("projector_version") != row["projector_version"]
            or manifest.get("alias_set_sha256") != row["alias_set_sha256"]
            or manifest.get("row_set_sha256") != row["row_set_sha256"]
            or manifest.get("manifest_sha256") != row["manifest_sha256"]
            or canonical_projection_manifest_json(manifest)
            != row["manifest_json"]
            or (
                require_clean_canonical_match
                and (
                    not cls._projection_manifest_is_clean(manifest)
                    or row["manifest_json"] != row["canonical_manifest_json"]
                    or row["manifest_sha256"]
                    != row["canonical_manifest_sha256"]
                )
            )
        ):
            raise ImageAnalysisPersistenceError(
                "image_projection_read_manifest_corrupt"
            )
        return {
            "previous_generation_id": row["previous_generation_id"],
            "manifest": manifest,
        }

    @classmethod
    def _build_full_image_projection_read_manifest_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation: Mapping[str, object],
        snapshot: Sequence[Mapping[str, object]],
        canonical_manifest: Mapping[str, object],
    ) -> dict[str, object]:
        counts = canonical_manifest.get("counts")
        if type(counts) is not dict:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        return build_projection_manifest(
            projector_version=str(generation["projector_version"]),
            compiler_version=str(generation["compiler_id"]),
            database_uuid=str(generation["database_uuid"]),
            canonical_high_water_mark=int(
                generation["canonical_high_water_position"]
            ),
            entries=(
                cls._projection_manifest_entries_for_full_physical_snapshot_in_transaction(
                    connection,
                    snapshot=snapshot,
                )
            ),
            alias_count=int(counts["aliases"]),
            alias_set_sha256=str(canonical_manifest["alias_set_sha256"]),
        )

    @classmethod
    def _restore_projection_generation_physical_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
    ) -> tuple[dict[str, object], list[dict[str, object]], dict[str, object]]:
        generation, snapshot, manifest = (
            cls._stored_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
            )
        )
        managed_ids = cls._managed_image_projection_physical_ids_in_transaction(
            connection,
            database_uuid=str(generation["database_uuid"]),
        )
        cls._materialize_physical_image_projection_snapshot_in_transaction(
            connection,
            snapshot=snapshot,
            managed_physical_ids=managed_ids,
            materialized_at=str(generation["created_at"]),
        )
        return generation, snapshot, manifest

    @classmethod
    def _backfill_v17_active_read_manifest_in_transaction(
        cls,
        connection: sqlite3.Connection,
    ) -> None:
        """Bind the V16 active generation to a complete physical read census."""

        active_rows = connection.execute(
            """SELECT id FROM image_projection_generations
                WHERE projection_contract=? AND is_active=1 LIMIT 2""",
            (IMAGE_PROJECTION_CONTRACT,),
        ).fetchall()
        if not active_rows:
            return
        if len(active_rows) != 1:
            raise ImageAnalysisPersistenceError(
                "image_projection_active_generation_invalid"
            )
        generation_id = str(active_rows[0]["id"])
        generation, snapshot, canonical_manifest = (
            cls._stored_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
            )
        )
        read_manifest = (
            cls._build_full_image_projection_read_manifest_in_transaction(
                connection,
                generation=generation,
                snapshot=snapshot,
                canonical_manifest=canonical_manifest,
            )
        )
        cls._insert_image_projection_read_manifest_in_transaction(
            connection,
            generation_id=generation_id,
            previous_generation_id=None,
            manifest=read_manifest,
            created_at=_utc_now_iso(),
        )
        if (
            not cls._projection_manifest_is_clean(read_manifest)
            or canonical_projection_manifest_json(read_manifest)
            != canonical_projection_manifest_json(canonical_manifest)
        ):
            changed = connection.execute(
                """UPDATE image_projection_generations SET is_active=0
                    WHERE id=? AND status='complete' AND is_active=1""",
                (generation_id,),
            )
            if changed.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_v17_cutover_deactivation_failed"
                )

    @classmethod
    def _require_projection_generation_snapshot_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        database_uuid: str,
        fixed_high_water: int,
        compiler_id: str,
        projector_version: str,
        snapshot: Sequence[Mapping[str, object]],
        require_active: bool,
        require_physical: bool,
        allow_managed_physical_extras: bool = False,
    ) -> dict[str, object]:
        """Verify one generation against a fresh canonical re-render snapshot."""

        generation_rows = connection.execute(
            """SELECT database_uuid,projection_contract,
                      canonical_high_water_position,compiler_id,
                      projector_version,status,is_active,created_at,completed_at
                 FROM image_projection_generations WHERE id=? LIMIT 2""",
            (generation_id,),
        ).fetchall()
        if len(generation_rows) != 1:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        generation = generation_rows[0]
        if (
            generation["database_uuid"] != database_uuid
            or generation["projection_contract"] != IMAGE_PROJECTION_CONTRACT
            or generation["canonical_high_water_position"] != fixed_high_water
            or generation["compiler_id"] != compiler_id
            or generation["projector_version"] != projector_version
            or generation["status"] != "complete"
            or type(generation["created_at"]) is not str
            or not generation["created_at"]
            or type(generation["completed_at"]) is not str
            or not generation["completed_at"]
            or (
                require_active
                and int(generation["is_active"] or 0) != 1
            )
        ):
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        canonical_aliases = [
            {
                "legacy_id": str(alias["legacy_id"]),
                "canonical_asset_id": str(alias["canonical_asset_id"]),
                "library_root_id": str(alias["library_root_id"]),
                "source_id": str(alias["source_id"]),
                "alias_scope": str(alias["alias_scope"]),
                "alias_sha256": str(alias["alias_sha256"]),
            }
            for alias in connection.execute(
                """SELECT legacy_id,canonical_asset_id,library_root_id,
                          source_id,alias_scope,alias_sha256
                     FROM legacy_image_aliases
                    WHERE database_uuid=? ORDER BY legacy_id""",
                (database_uuid,),
            ).fetchall()
        ]
        stored_aliases = [
            dict(alias)
            for alias in connection.execute(
                """SELECT legacy_id,canonical_asset_id,library_root_id,
                          source_id,alias_scope,alias_sha256
                     FROM image_projection_aliases
                    WHERE generation_id=? ORDER BY legacy_id""",
                (generation_id,),
            ).fetchall()
        ]
        if stored_aliases != canonical_aliases:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        alias_set = [
            {
                "legacy_id": alias["legacy_id"],
                "alias_sha256": alias["alias_sha256"],
            }
            for alias in canonical_aliases
        ]
        alias_set_sha256 = canonical_sha256(alias_set)
        expected_rows = [
            {
                "asset_id": item["asset_id"],
                "analysis_run_id": item["analysis_binding"]["analysis_run_id"],
                "revision": item["analysis_binding"]["revision"],
                "content_sha256": item["analysis_binding"]["content_sha256"],
                "source_id": item["source_id"],
                "source_binding_sha256": item["source_binding_sha256"],
                "row_json": canonical_job_json(item["document"]),
                "row_sha256": item["row_sha256"],
                "alias_set_sha256": alias_set_sha256,
            }
            for item in snapshot
        ]
        stored_rows = [
            dict(row)
            for row in connection.execute(
                """SELECT asset_id,analysis_run_id,revision,content_sha256,
                          source_id,source_binding_sha256,row_json,row_sha256,
                          alias_set_sha256
                     FROM image_projection_rows
                    WHERE generation_id=? ORDER BY asset_id""",
                (generation_id,),
            ).fetchall()
        ]
        if stored_rows != expected_rows:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        manifest = build_projection_manifest(
            projector_version=projector_version,
            compiler_version=compiler_id,
            database_uuid=database_uuid,
            canonical_high_water_mark=fixed_high_water,
            entries=cls._projection_manifest_entries_for_snapshot(snapshot),
            alias_count=len(alias_set),
            alias_set_sha256=alias_set_sha256,
        )
        stored_manifest = connection.execute(
            """SELECT database_uuid,canonical_high_water_position,
                      compiler_id,projector_version,eligible_count,
                      projected_count,alias_count,missing_count,
                      unexpected_count,mismatched_count,blocked_count,
                      alias_set_sha256,row_set_sha256,manifest_json,
                      manifest_sha256
                 FROM image_projection_manifests WHERE generation_id=?""",
            (generation_id,),
        ).fetchone()
        counts = manifest["counts"]
        assert type(counts) is dict
        expected_manifest = {
            "database_uuid": database_uuid,
            "canonical_high_water_position": fixed_high_water,
            "compiler_id": compiler_id,
            "projector_version": projector_version,
            "eligible_count": counts["eligible"],
            "projected_count": counts["projected"],
            "alias_count": counts["aliases"],
            "missing_count": counts["missing"],
            "unexpected_count": counts["unexpected"],
            "mismatched_count": counts["mismatched"],
            "blocked_count": counts["blocked"],
            "alias_set_sha256": manifest["alias_set_sha256"],
            "row_set_sha256": manifest["row_set_sha256"],
            "manifest_json": canonical_projection_manifest_json(manifest),
            "manifest_sha256": manifest["manifest_sha256"],
        }
        if stored_manifest is None or dict(stored_manifest) != expected_manifest:
            raise ImageAnalysisPersistenceError(
                "image_projection_generation_snapshot_corrupt"
            )
        if require_physical:
            managed_ids = cls._managed_image_projection_physical_ids_in_transaction(
                connection,
                database_uuid=database_uuid,
            )
            if not cls._physical_image_projection_semantically_matches_snapshot_in_transaction(
                connection,
                snapshot=snapshot,
                managed_physical_ids=managed_ids,
                allow_managed_extras=allow_managed_physical_extras,
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_generation_snapshot_corrupt"
                )
        if require_active:
            cls._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                require_clean_canonical_match=True,
            )
        return {
            "manifest": manifest,
            "alias_set_sha256": alias_set_sha256,
            "alias_count": len(alias_set),
            "is_active": int(generation["is_active"] or 0) == 1,
        }

    @staticmethod
    def _insert_removed_projection_generation_in_transaction(
        connection: sqlite3.Connection,
        *,
        generation_id: str,
        database_uuid: str,
        high_water: int,
        compiler_id: str,
        projector_version: str,
        snapshot: Sequence[Mapping[str, object]],
        manifest: Mapping[str, object],
        change: Mapping[str, object],
        now: str,
    ) -> dict[str, object]:
        """Persist one building successor and its rowless removal receipt."""

        connection.execute(
            """INSERT INTO image_projection_generations(
                   id,database_uuid,projection_contract,
                   canonical_high_water_position,compiler_id,projector_version,
                   status,is_active,created_at,completed_at)
               VALUES(?,?,?,?,?,?,'building',0,?,NULL)""",
            (
                generation_id,
                database_uuid,
                IMAGE_PROJECTION_CONTRACT,
                high_water,
                compiler_id,
                projector_version,
                now,
            ),
        )
        alias_set_sha256 = str(manifest["alias_set_sha256"])
        for item in snapshot:
            analysis_binding = item["analysis_binding"]
            assert type(analysis_binding) is dict
            connection.execute(
                """INSERT INTO image_projection_rows(
                       generation_id,asset_id,analysis_run_id,revision,
                       content_sha256,source_id,source_binding_sha256,row_json,
                       row_sha256,alias_set_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    item["asset_id"],
                    analysis_binding["analysis_run_id"],
                    analysis_binding["revision"],
                    analysis_binding["content_sha256"],
                    item["source_id"],
                    item["source_binding_sha256"],
                    canonical_job_json(item["document"]),
                    item["row_sha256"],
                    alias_set_sha256,
                    now,
                ),
            )
        analysis_binding = change["analysis_binding"]
        assert type(analysis_binding) is dict
        receipt = seal_projection_receipt(
            {
                "object": "memolens.image_projection_receipt",
                "schema_version": "1",
                "projection_contract": IMAGE_PROJECTION_CONTRACT,
                "projector_version": projector_version,
                "database_uuid": database_uuid,
                "change_position": change["change_position"],
                "asset_id": change["asset_id"],
                "analysis_binding": analysis_binding,
                "source_binding_sha256": None,
                "projected_row_sha256": None,
                "alias_set_sha256": alias_set_sha256,
                "outcome": "removed",
                "reason_code": "image_source_unavailable",
            }
        )
        connection.execute(
            """INSERT INTO image_projection_receipts(
                   change_position,generation_id,projection_contract,
                   projector_version,database_uuid,asset_id,analysis_run_id,
                   revision,content_sha256,source_binding_sha256,
                   projected_row_sha256,alias_set_sha256,outcome,reason_code,
                   receipt_json,receipt_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                change["change_position"],
                generation_id,
                receipt["projection_contract"],
                receipt["projector_version"],
                receipt["database_uuid"],
                receipt["asset_id"],
                analysis_binding["analysis_run_id"],
                analysis_binding["revision"],
                analysis_binding["content_sha256"],
                receipt["source_binding_sha256"],
                receipt["projected_row_sha256"],
                receipt["alias_set_sha256"],
                receipt["outcome"],
                receipt["reason_code"],
                canonical_projection_receipt_json(receipt),
                receipt["receipt_sha256"],
                now,
            ),
        )
        counts = manifest["counts"]
        assert type(counts) is dict
        connection.execute(
            """INSERT INTO image_projection_manifests(
                   generation_id,database_uuid,canonical_high_water_position,
                   compiler_id,projector_version,eligible_count,projected_count,
                   alias_count,missing_count,unexpected_count,mismatched_count,
                   blocked_count,alias_set_sha256,row_set_sha256,manifest_json,
                   manifest_sha256,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                generation_id,
                database_uuid,
                high_water,
                compiler_id,
                projector_version,
                counts["eligible"],
                counts["projected"],
                counts["aliases"],
                counts["missing"],
                counts["unexpected"],
                counts["mismatched"],
                counts["blocked"],
                manifest["alias_set_sha256"],
                manifest["row_set_sha256"],
                canonical_projection_manifest_json(manifest),
                manifest["manifest_sha256"],
                now,
            ),
        )
        completed = connection.execute(
            """UPDATE image_projection_generations
                  SET status='complete',completed_at=?
                WHERE id=? AND status='building' AND is_active=0""",
            (now, generation_id),
        )
        if completed.rowcount != 1:
            raise ImageAnalysisPersistenceError(
                "image_source_removal_generation_complete_failed"
            )
        return receipt

    def project_image_source_removal(
        self,
        *,
        change_position: int,
        projector_version: str = IMAGE_PROJECTOR_VERSION,
        compiler_id: str = IMAGE_PROJECTION_COMPILER_ID,
    ) -> dict[str, object]:
        """Project one canonical remove change in a separate successor transaction."""

        if type(change_position) is not int or change_position < 1:
            raise ImageAnalysisPersistenceError(
                "image_source_removal_change_invalid"
            )
        projector_version = _require_rebuild_token(projector_version)
        compiler_id = _require_rebuild_token(compiler_id)
        expected_database_identity = self._admit_image_database_identity()
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            target = connection.execute(
                """SELECT asset_id FROM image_projection_changes
                    WHERE position=?""",
                (change_position,),
            ).fetchone()
            if target is None:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_change_invalid"
                )
            change_row = (
                self._verified_latest_image_projection_change_in_transaction(
                    connection,
                    asset_id=str(target["asset_id"]),
                )
            )
            if (
                change_row is None
                or change_row["position"] != change_position
                or change_row["database_uuid"] != meta["database_uuid"]
                or change_row["operation"] != "remove"
                or change_row["source_binding_sha256"] is not None
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_change_not_current"
                )
            change = change_row["change"]
            analysis_binding = change["analysis_binding"]
            assert type(analysis_binding) is dict
            current_head = self._expected_image_head_in_transaction(
                connection,
                str(change_row["asset_id"]),
            )
            current_result = connection.execute(
                """SELECT source_id FROM image_analysis_results
                    WHERE asset_id=? AND analysis_run_id=? AND revision=?
                      AND content_sha256=?""",
                (
                    change_row["asset_id"],
                    analysis_binding["analysis_run_id"],
                    analysis_binding["revision"],
                    analysis_binding["content_sha256"],
                ),
            ).fetchone()
            if (
                current_head != analysis_binding
                or current_result is None
                or current_result["source_id"] != change_row["source_id"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_head_changed"
                )
            existing = connection.execute(
                """SELECT generation_id,receipt_json FROM image_projection_receipts
                    WHERE change_position=?""",
                (change_position,),
            ).fetchone()
            if existing is not None:
                verified = (
                    self._verified_removed_projection_for_current_head_in_transaction(
                        connection,
                        asset_id=str(change_row["asset_id"]),
                        analysis_binding=analysis_binding,
                        source_id=str(change_row["source_id"]),
                    )
                )
                receipt = _parse_json(existing["receipt_json"])
                manifest_row = connection.execute(
                    """SELECT manifest.manifest_json,generation.is_active
                         FROM image_projection_manifests manifest
                         JOIN image_projection_generations generation
                           ON generation.id=manifest.generation_id
                        WHERE manifest.generation_id=?""",
                    (existing["generation_id"],),
                ).fetchone()
                manifest = (
                    _parse_json(manifest_row["manifest_json"])
                    if manifest_row is not None
                    else None
                )
                if verified is None or receipt is None or manifest is None:
                    raise ImageAnalysisPersistenceError(
                        "image_analysis_projection_corrupt"
                    )
                stored_read = (
                    self._require_image_projection_read_manifest_in_transaction(
                        connection,
                        generation_id=str(existing["generation_id"]),
                        require_clean_canonical_match=True,
                    )
                )
                self._admit_image_database_identity(expected_database_identity)
                return {
                    "change_position": change_position,
                    "generation_id": str(existing["generation_id"]),
                    "previous_generation_id": stored_read[
                        "previous_generation_id"
                    ],
                    "projection_receipt": receipt,
                    "manifest": stored_read["manifest"],
                    "canonical_manifest": manifest,
                    "is_active": bool(int(manifest_row["is_active"] or 0)),
                    "replayed": True,
                }
            high_water_row = connection.execute(
                "SELECT MAX(position) AS high_water FROM image_projection_changes"
            ).fetchone()
            if (
                high_water_row is None
                or high_water_row["high_water"] != change_position
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_change_not_current"
                )
            pending = connection.execute(
                """SELECT change.position FROM image_projection_changes change
                     LEFT JOIN image_projection_receipts receipt
                       ON receipt.change_position=change.position
                    WHERE change.position<=? AND receipt.change_position IS NULL
                    ORDER BY change.position""",
                (change_position,),
            ).fetchall()
            if [int(row["position"]) for row in pending] != [change_position]:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_projection_pending_conflict"
                )
            active_rows = connection.execute(
                """SELECT id,canonical_high_water_position,compiler_id,
                          projector_version,status
                     FROM image_projection_generations
                    WHERE database_uuid=? AND projection_contract=?
                      AND is_active=1 LIMIT 2""",
                (meta["database_uuid"], IMAGE_PROJECTION_CONTRACT),
            ).fetchall()
            if len(active_rows) != 1:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_active_generation_invalid"
                )
            previous = active_rows[0]
            previous_high_water = previous["canonical_high_water_position"]
            if (
                previous["status"] != "complete"
                or type(previous_high_water) is not int
                or not 1 <= previous_high_water <= change_position
                or previous["compiler_id"] != compiler_id
                or previous["projector_version"] != projector_version
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_active_generation_invalid"
                )
            database_uuid = str(meta["database_uuid"])
            previous_generation_id = str(previous["id"])
            previous_snapshot = (
                self._canonical_projection_rebuild_snapshot_in_transaction(
                    connection,
                    database_uuid=database_uuid,
                    fixed_high_water=previous_high_water,
                    projector_version=projector_version,
                )
            )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=previous_generation_id,
                database_uuid=database_uuid,
                fixed_high_water=previous_high_water,
                compiler_id=compiler_id,
                projector_version=projector_version,
                snapshot=previous_snapshot,
                require_active=True,
                require_physical=True,
                allow_managed_physical_extras=True,
            )
            snapshot = self._canonical_projection_rebuild_snapshot_in_transaction(
                connection,
                database_uuid=database_uuid,
                fixed_high_water=change_position,
                projector_version=projector_version,
            )
            if any(
                item["asset_id"] == change_row["asset_id"] for item in snapshot
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_projection_not_rowless"
                )
            alias_count, alias_set_sha256 = self._image_alias_set_in_transaction(
                connection,
                database_uuid,
            )
            manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=database_uuid,
                canonical_high_water_mark=change_position,
                entries=self._projection_manifest_entries_for_snapshot(snapshot),
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            managed_ids = self._managed_image_projection_physical_ids_in_transaction(
                connection,
                database_uuid=database_uuid,
            )
            self._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=snapshot,
                managed_physical_ids=managed_ids,
                materialized_at=now,
            )
            read_manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=database_uuid,
                canonical_high_water_mark=change_position,
                entries=(
                    self._projection_manifest_entries_for_full_physical_snapshot_in_transaction(
                        connection,
                        snapshot=snapshot,
                    )
                ),
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            if not self._projection_read_manifest_matches_canonical(
                read_manifest,
                manifest,
            ):
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_read_cutover_blocked"
                )
            generation_identity = {
                "object": "memolens.image_projection_removal_generation",
                "schema_version": "1",
                "database_uuid": database_uuid,
                "projection_contract": IMAGE_PROJECTION_CONTRACT,
                "change_sha256": change["change_sha256"],
                "projector_version": projector_version,
                "compiler_id": compiler_id,
                "alias_set_sha256": alias_set_sha256,
            }
            generation_id = (
                f"ipgen_remove_{canonical_sha256(generation_identity)}"
            )
            if connection.execute(
                "SELECT 1 FROM image_projection_generations WHERE id=?",
                (generation_id,),
            ).fetchone() is not None:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_generation_conflict"
                )
            receipt = self._insert_removed_projection_generation_in_transaction(
                connection,
                generation_id=generation_id,
                database_uuid=database_uuid,
                high_water=change_position,
                compiler_id=compiler_id,
                projector_version=projector_version,
                snapshot=snapshot,
                manifest=manifest,
                change=change,
                now=now,
            )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
                database_uuid=database_uuid,
                fixed_high_water=change_position,
                compiler_id=compiler_id,
                projector_version=projector_version,
                snapshot=snapshot,
                require_active=False,
                require_physical=False,
            )
            self._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=snapshot,
                managed_physical_ids=managed_ids,
                materialized_at=now,
            )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
                database_uuid=database_uuid,
                fixed_high_water=change_position,
                compiler_id=compiler_id,
                projector_version=projector_version,
                snapshot=snapshot,
                require_active=False,
                require_physical=True,
            )
            self._insert_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                previous_generation_id=previous_generation_id,
                manifest=read_manifest,
                created_at=now,
            )
            deactivated = connection.execute(
                """UPDATE image_projection_generations SET is_active=0
                    WHERE id=? AND status='complete' AND is_active=1""",
                (previous_generation_id,),
            )
            if deactivated.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_deactivation_failed"
                )
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (generation_id,),
            )
            if activated.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_source_removal_activation_failed"
                )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=generation_id,
                database_uuid=database_uuid,
                fixed_high_water=change_position,
                compiler_id=compiler_id,
                projector_version=projector_version,
                snapshot=snapshot,
                require_active=True,
                require_physical=True,
            )
            self._admit_image_database_identity(expected_database_identity)
        return {
            "change_position": change_position,
            "generation_id": generation_id,
            "previous_generation_id": previous_generation_id,
            "projection_receipt": receipt,
            "manifest": read_manifest,
            "canonical_manifest": manifest,
            "is_active": True,
            "replayed": False,
        }

    @classmethod
    def _projection_rebuild_predecessor_in_transaction(
        cls,
        connection: sqlite3.Connection,
        *,
        database_uuid: str,
        fixed_high_water: int,
        compiler_id: str,
        projector_version: str,
    ) -> tuple[sqlite3.Row, bool]:
        active_rows = connection.execute(
            """SELECT id,canonical_high_water_position,compiler_id,
                      projector_version,status,created_at
                 FROM image_projection_generations
                WHERE database_uuid=? AND projection_contract=?
                  AND is_active=1 LIMIT 2""",
            (database_uuid, IMAGE_PROJECTION_CONTRACT),
        ).fetchall()
        if len(active_rows) > 1:
            raise ImageAnalysisPersistenceError(
                "image_projection_rebuild_active_generation_invalid"
            )
        predecessor_was_active = len(active_rows) == 1
        if predecessor_was_active:
            predecessor = active_rows[0]
        else:
            recovery_rows = connection.execute(
                """SELECT generation.id,
                          generation.canonical_high_water_position,
                          generation.compiler_id,generation.projector_version,
                          generation.status,generation.created_at
                     FROM image_projection_generations generation
                     JOIN image_projection_read_manifests read_manifest
                       ON read_manifest.generation_id=generation.id
                    WHERE generation.database_uuid=?
                      AND generation.projection_contract=?
                      AND generation.status='complete'
                      AND generation.is_active=0
                    LIMIT 2""",
                (database_uuid, IMAGE_PROJECTION_CONTRACT),
            ).fetchall()
            if len(recovery_rows) != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_active_generation_invalid"
                )
            predecessor = recovery_rows[0]
            recovery_read = cls._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=str(predecessor["id"]),
                require_clean_canonical_match=False,
            )
            if (
                recovery_read["previous_generation_id"] is not None
                or cls._projection_manifest_is_clean(recovery_read["manifest"])
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_active_generation_invalid"
                )
        if (
            predecessor["status"] != "complete"
            or predecessor["canonical_high_water_position"] != fixed_high_water
            or predecessor["compiler_id"] != compiler_id
            or predecessor["projector_version"] != projector_version
        ):
            raise ImageAnalysisPersistenceError(
                "image_projection_rebuild_active_generation_mismatch"
            )
        return predecessor, predecessor_was_active

    def rebuild_image_projection_generation(
        self,
        *,
        fixed_high_water: int,
        expected_alias_set_sha256: str,
        rebuild_nonce: str,
        runtime_generation: str,
        projector_version: str = IMAGE_PROJECTOR_VERSION,
        compiler_id: str = IMAGE_PROJECTION_COMPILER_ID,
    ) -> dict[str, object]:
        """Build and atomically activate one same-high-water successor generation."""

        if type(fixed_high_water) is not int or fixed_high_water < 1:
            raise ImageAnalysisPersistenceError(
                "image_projection_rebuild_high_water_invalid"
            )
        runtime_generation = _require_rebuild_runtime_generation(
            runtime_generation
        )
        rebuild_nonce = _require_rebuild_nonce(rebuild_nonce)
        expected_alias_set_sha256 = _require_rebuild_digest(
            expected_alias_set_sha256
        )
        projector_version = _require_rebuild_token(projector_version)
        compiler_id = _require_rebuild_token(compiler_id)
        expected_database_identity = self._admit_image_database_identity()
        now = _utc_now_iso()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            database_uuid = str(meta["database_uuid"])
            high_water_row = connection.execute(
                "SELECT MAX(position) AS high_water FROM image_projection_changes"
            ).fetchone()
            if high_water_row is None or type(high_water_row["high_water"]) is not int:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_high_water_invalid"
                )
            current_high_water = int(high_water_row["high_water"])
            if fixed_high_water < current_high_water:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_high_water_stale"
                )
            if fixed_high_water > current_high_water:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_high_water_future"
                )
            alias_count, alias_set_sha256 = self._image_alias_set_in_transaction(
                connection,
                database_uuid,
            )
            if alias_set_sha256 != expected_alias_set_sha256:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_alias_set_changed"
                )
            snapshot = self._canonical_projection_rebuild_snapshot_in_transaction(
                connection,
                database_uuid=database_uuid,
                fixed_high_water=fixed_high_water,
                projector_version=projector_version,
            )
            manifest_entries = [
                {
                    "asset_id": item["asset_id"],
                    "analysis_binding": item["analysis_binding"],
                    "expected_row_sha256": item["row_sha256"],
                    "actual_row_sha256": item["row_sha256"],
                    "status": "matched",
                    "reason_code": None,
                }
                for item in snapshot
            ]
            manifest = build_projection_manifest(
                projector_version=projector_version,
                compiler_version=compiler_id,
                database_uuid=database_uuid,
                canonical_high_water_mark=fixed_high_water,
                entries=manifest_entries,
                alias_count=alias_count,
                alias_set_sha256=alias_set_sha256,
            )
            manifest_json = canonical_projection_manifest_json(manifest)
            runtime_generation_sha256 = hashlib.sha256(
                runtime_generation.encode("ascii")
            ).hexdigest()
            rebuild_identity = {
                "object": "memolens.image_projection_rebuild_identity",
                "schema_version": "1",
                "database_uuid": database_uuid,
                "projection_contract": IMAGE_PROJECTION_CONTRACT,
                "fixed_high_water": fixed_high_water,
                "projector_version": projector_version,
                "compiler_id": compiler_id,
                "expected_alias_set_sha256": expected_alias_set_sha256,
                "rebuild_nonce": rebuild_nonce,
                "runtime_generation_sha256": runtime_generation_sha256,
            }
            rebuild_sha256 = canonical_sha256(rebuild_identity)
            generation_id = f"ipgen_rebuild_{rebuild_sha256}"

            active, previous_generation_was_active = (
                self._projection_rebuild_predecessor_in_transaction(
                    connection,
                    database_uuid=database_uuid,
                    fixed_high_water=fixed_high_water,
                    compiler_id=compiler_id,
                    projector_version=projector_version,
                )
            )
            previous_generation_id = str(active["id"])
            lineage_previous_generation_id = (
                previous_generation_id
                if previous_generation_was_active
                else None
            )

            canonical_aliases = [
                {
                    "legacy_id": str(alias["legacy_id"]),
                    "canonical_asset_id": str(alias["canonical_asset_id"]),
                    "library_root_id": str(alias["library_root_id"]),
                    "source_id": str(alias["source_id"]),
                    "alias_scope": str(alias["alias_scope"]),
                    "alias_sha256": str(alias["alias_sha256"]),
                }
                for alias in connection.execute(
                    """SELECT legacy_id,canonical_asset_id,library_root_id,
                              source_id,alias_scope,alias_sha256
                         FROM legacy_image_aliases
                        WHERE database_uuid=? ORDER BY legacy_id""",
                    (database_uuid,),
                ).fetchall()
            ]

            def verify_generation(
                verified_generation_id: str,
                *,
                require_active: bool,
                require_physical: bool,
            ) -> dict[str, object]:
                generation_rows = connection.execute(
                    """SELECT database_uuid,projection_contract,
                              canonical_high_water_position,compiler_id,
                              projector_version,status,is_active,created_at,
                              completed_at
                         FROM image_projection_generations WHERE id=? LIMIT 2""",
                    (verified_generation_id,),
                ).fetchall()
                if len(generation_rows) != 1:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_generation_corrupt"
                    )
                generation = generation_rows[0]
                if (
                    generation["database_uuid"] != database_uuid
                    or generation["projection_contract"]
                    != IMAGE_PROJECTION_CONTRACT
                    or generation["canonical_high_water_position"]
                    != fixed_high_water
                    or generation["compiler_id"] != compiler_id
                    or generation["projector_version"] != projector_version
                    or generation["status"] != "complete"
                    or type(generation["created_at"]) is not str
                    or not generation["created_at"]
                    or type(generation["completed_at"]) is not str
                    or not generation["completed_at"]
                    or (
                        require_active
                        and int(generation["is_active"] or 0) != 1
                    )
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_generation_corrupt"
                    )
                stored_rows = connection.execute(
                    """SELECT asset_id,analysis_run_id,revision,content_sha256,
                              source_id,source_binding_sha256,row_json,row_sha256,
                              alias_set_sha256
                         FROM image_projection_rows
                        WHERE generation_id=? ORDER BY asset_id""",
                    (verified_generation_id,),
                ).fetchall()
                expected_rows = [
                    {
                        "asset_id": item["asset_id"],
                        "analysis_run_id": item["analysis_binding"][
                            "analysis_run_id"
                        ],
                        "revision": item["analysis_binding"]["revision"],
                        "content_sha256": item["analysis_binding"][
                            "content_sha256"
                        ],
                        "source_id": item["source_id"],
                        "source_binding_sha256": item[
                            "source_binding_sha256"
                        ],
                        "row_json": canonical_job_json(item["document"]),
                        "row_sha256": item["row_sha256"],
                        "alias_set_sha256": alias_set_sha256,
                    }
                    for item in snapshot
                ]
                if [dict(row) for row in stored_rows] != expected_rows:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_generation_corrupt"
                    )
                stored_aliases = [
                    dict(alias)
                    for alias in connection.execute(
                        """SELECT legacy_id,canonical_asset_id,library_root_id,
                                  source_id,alias_scope,alias_sha256
                             FROM image_projection_aliases
                            WHERE generation_id=? ORDER BY legacy_id""",
                        (verified_generation_id,),
                    ).fetchall()
                ]
                if stored_aliases != canonical_aliases:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_generation_corrupt"
                    )
                stored_manifest = connection.execute(
                    """SELECT database_uuid,canonical_high_water_position,
                              compiler_id,projector_version,eligible_count,
                              projected_count,alias_count,missing_count,
                              unexpected_count,mismatched_count,blocked_count,
                              alias_set_sha256,row_set_sha256,manifest_json,
                              manifest_sha256
                         FROM image_projection_manifests
                        WHERE generation_id=?""",
                    (verified_generation_id,),
                ).fetchone()
                counts = manifest["counts"]
                assert type(counts) is dict
                expected_manifest = {
                    "database_uuid": database_uuid,
                    "canonical_high_water_position": fixed_high_water,
                    "compiler_id": compiler_id,
                    "projector_version": projector_version,
                    "eligible_count": counts["eligible"],
                    "projected_count": counts["projected"],
                    "alias_count": counts["aliases"],
                    "missing_count": counts["missing"],
                    "unexpected_count": counts["unexpected"],
                    "mismatched_count": counts["mismatched"],
                    "blocked_count": counts["blocked"],
                    "alias_set_sha256": manifest["alias_set_sha256"],
                    "row_set_sha256": manifest["row_set_sha256"],
                    "manifest_json": manifest_json,
                    "manifest_sha256": manifest["manifest_sha256"],
                }
                if (
                    stored_manifest is None
                    or dict(stored_manifest) != expected_manifest
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_generation_corrupt"
                    )
                for item in snapshot:
                    verified = self._verified_projection_generation_in_transaction(
                        connection,
                        generation_id=verified_generation_id,
                        database_uuid=database_uuid,
                        projection_contract=IMAGE_PROJECTION_CONTRACT,
                        minimum_high_water_position=int(item["change_position"]),
                        asset_id=str(item["asset_id"]),
                        analysis_binding=item["analysis_binding"],
                        source_binding_digest=str(
                            item["source_binding_sha256"]
                        ),
                        source_relative_path=str(item["source_relative_path"]),
                        result=item["result"],
                        expected_projector_version=projector_version,
                        expected_row_sha256=str(item["row_sha256"]),
                        expected_alias_set_sha256=alias_set_sha256,
                        require_active=require_active,
                        require_physical=require_physical,
                    )
                    if verified is None:
                        raise ImageAnalysisPersistenceError(
                            "image_projection_rebuild_generation_corrupt"
                        )
                if require_active:
                    self._require_image_projection_read_manifest_in_transaction(
                        connection,
                        generation_id=verified_generation_id,
                        require_clean_canonical_match=True,
                    )
                return {
                    "is_active": int(generation["is_active"] or 0) == 1,
                    "created_at": str(generation["created_at"]),
                }

            managed_physical_ids = (
                self._managed_image_projection_physical_ids_in_transaction(
                    connection,
                    database_uuid=database_uuid,
                )
            )

            existing = connection.execute(
                "SELECT 1 FROM image_projection_generations WHERE id=?",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                if not previous_generation_was_active:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_recovery_identity_conflict"
                    )
                active_generation = verify_generation(
                    previous_generation_id,
                    require_active=True,
                    require_physical=False,
                )
                verified_existing = verify_generation(
                    generation_id,
                    require_active=False,
                    require_physical=False,
                )
                stored_read = (
                    self._require_image_projection_read_manifest_in_transaction(
                        connection,
                        generation_id=generation_id,
                        require_clean_canonical_match=bool(
                            verified_existing["is_active"]
                        ),
                    )
                )
                active_created_at = str(active_generation["created_at"])
                if not self._physical_image_projection_matches_snapshot_in_transaction(
                    connection,
                    snapshot=snapshot,
                    managed_physical_ids=managed_physical_ids,
                    materialized_at=active_created_at,
                ):
                    self._materialize_physical_image_projection_snapshot_in_transaction(
                        connection,
                        snapshot=snapshot,
                        managed_physical_ids=managed_physical_ids,
                        materialized_at=active_created_at,
                    )
                verify_generation(
                    previous_generation_id,
                    require_active=True,
                    require_physical=True,
                )
                if (
                    self._admit_image_database_identity(expected_database_identity)
                    != expected_database_identity
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_database_scope_changed"
                    )
                return {
                    "generation_id": generation_id,
                    "previous_generation_id": stored_read[
                        "previous_generation_id"
                    ],
                    "fixed_high_water": fixed_high_water,
                    "manifest": stored_read["manifest"],
                    "canonical_manifest": manifest,
                    "rebuild_sha256": rebuild_sha256,
                    "runtime_generation_sha256": runtime_generation_sha256,
                    "is_active": bool(verified_existing["is_active"]),
                    "replayed": True,
                }

            connection.execute(
                """INSERT INTO image_projection_generations(
                       id,database_uuid,projection_contract,
                       canonical_high_water_position,compiler_id,projector_version,
                       status,is_active,created_at,completed_at)
                   VALUES(?,?,?,?,?,?,'building',0,?,NULL)""",
                (
                    generation_id,
                    database_uuid,
                    IMAGE_PROJECTION_CONTRACT,
                    fixed_high_water,
                    compiler_id,
                    projector_version,
                    now,
                ),
            )
            for item in snapshot:
                analysis_binding = item["analysis_binding"]
                assert type(analysis_binding) is dict
                connection.execute(
                    """INSERT INTO image_projection_rows(
                           generation_id,asset_id,analysis_run_id,revision,
                           content_sha256,source_id,source_binding_sha256,row_json,
                           row_sha256,alias_set_sha256,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        generation_id,
                        item["asset_id"],
                        analysis_binding["analysis_run_id"],
                        analysis_binding["revision"],
                        analysis_binding["content_sha256"],
                        item["source_id"],
                        item["source_binding_sha256"],
                        canonical_job_json(item["document"]),
                        item["row_sha256"],
                        alias_set_sha256,
                        now,
                    ),
                )
            counts = manifest["counts"]
            assert type(counts) is dict
            connection.execute(
                """INSERT INTO image_projection_manifests(
                       generation_id,database_uuid,canonical_high_water_position,
                       compiler_id,projector_version,eligible_count,projected_count,
                       alias_count,missing_count,unexpected_count,mismatched_count,
                       blocked_count,alias_set_sha256,row_set_sha256,manifest_json,
                       manifest_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    generation_id,
                    database_uuid,
                    fixed_high_water,
                    compiler_id,
                    projector_version,
                    counts["eligible"],
                    counts["projected"],
                    counts["aliases"],
                    counts["missing"],
                    counts["unexpected"],
                    counts["mismatched"],
                    counts["blocked"],
                    manifest["alias_set_sha256"],
                    manifest["row_set_sha256"],
                    manifest_json,
                    manifest["manifest_sha256"],
                    now,
                ),
            )
            completed = connection.execute(
                """UPDATE image_projection_generations
                      SET status='complete',completed_at=?
                    WHERE id=? AND status='building' AND is_active=0""",
                (now, generation_id),
            )
            if completed.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_generation_complete_failed"
                )

            verify_generation(
                previous_generation_id,
                require_active=previous_generation_was_active,
                require_physical=False,
            )
            verify_generation(
                generation_id,
                require_active=False,
                require_physical=False,
            )
            self._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=snapshot,
                managed_physical_ids=managed_physical_ids,
                materialized_at=now,
            )
            verify_generation(
                generation_id,
                require_active=False,
                require_physical=True,
            )

            read_manifest = (
                self._build_full_image_projection_read_manifest_in_transaction(
                    connection,
                    generation={
                        "database_uuid": database_uuid,
                        "canonical_high_water_position": fixed_high_water,
                        "compiler_id": compiler_id,
                        "projector_version": projector_version,
                    },
                    snapshot=snapshot,
                    canonical_manifest=manifest,
                )
            )
            self._insert_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=generation_id,
                previous_generation_id=lineage_previous_generation_id,
                manifest=read_manifest,
                created_at=now,
            )
            clean_cutover = bool(
                self._projection_manifest_is_clean(read_manifest)
                and canonical_projection_manifest_json(read_manifest)
                == manifest_json
                and read_manifest["manifest_sha256"]
                == manifest["manifest_sha256"]
            )
            if not clean_cutover:
                if not previous_generation_was_active:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_read_cutover_blocked"
                    )
                self._restore_projection_generation_physical_snapshot_in_transaction(
                    connection,
                    generation_id=previous_generation_id,
                )
                verify_generation(
                    previous_generation_id,
                    require_active=True,
                    require_physical=True,
                )
                if (
                    self._admit_image_database_identity(expected_database_identity)
                    != expected_database_identity
                ):
                    raise ImageAnalysisPersistenceError(
                        "image_database_scope_changed"
                    )
                return {
                    "generation_id": generation_id,
                    "previous_generation_id": lineage_previous_generation_id,
                    "fixed_high_water": fixed_high_water,
                    "manifest": read_manifest,
                    "canonical_manifest": manifest,
                    "rebuild_sha256": rebuild_sha256,
                    "runtime_generation_sha256": runtime_generation_sha256,
                    "is_active": False,
                    "replayed": False,
                }

            if previous_generation_was_active:
                deactivated = connection.execute(
                    """UPDATE image_projection_generations SET is_active=0
                        WHERE id=? AND status='complete' AND is_active=1""",
                    (previous_generation_id,),
                )
                if deactivated.rowcount != 1:
                    raise ImageAnalysisPersistenceError(
                        "image_projection_rebuild_deactivation_failed"
                    )
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (generation_id,),
            )
            if activated.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rebuild_activation_failed"
                )
            verify_generation(
                generation_id,
                require_active=True,
                require_physical=True,
            )
            if (
                self._admit_image_database_identity(expected_database_identity)
                != expected_database_identity
            ):
                raise ImageAnalysisPersistenceError("image_database_scope_changed")
        return {
            "generation_id": generation_id,
            "previous_generation_id": lineage_previous_generation_id,
            "fixed_high_water": fixed_high_water,
            "manifest": read_manifest,
            "canonical_manifest": manifest,
            "rebuild_sha256": rebuild_sha256,
            "runtime_generation_sha256": runtime_generation_sha256,
            "is_active": True,
            "replayed": False,
        }

    def rollback_image_projection_read_generation(
        self,
        *,
        expected_active_generation_id: str,
        target_generation_id: str,
    ) -> dict[str, object]:
        """Atomically restore the active generation's proved clean predecessor."""

        for value in (expected_active_generation_id, target_generation_id):
            if (
                type(value) is not str
                or not 1 <= len(value) <= 200
                or any(
                    not (
                        character.isascii()
                        and (character.isalnum() or character in {"_", "-"})
                    )
                    for character in value
                )
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_generation_invalid"
                )
        if expected_active_generation_id == target_generation_id:
            raise ImageAnalysisPersistenceError(
                "image_projection_rollback_generation_invalid"
            )

        expected_database_identity = self._admit_image_database_identity()
        with self.transaction(immediate=True) as connection:  # type: ignore[attr-defined]
            meta = self._image_meta_in_transaction(connection)
            active_rows = connection.execute(
                """SELECT id,database_uuid,canonical_high_water_position,
                          compiler_id,projector_version,status
                     FROM image_projection_generations
                    WHERE database_uuid=? AND projection_contract=?
                      AND is_active=1 LIMIT 2""",
                (meta["database_uuid"], IMAGE_PROJECTION_CONTRACT),
            ).fetchall()
            if (
                len(active_rows) != 1
                or active_rows[0]["id"] != expected_active_generation_id
                or active_rows[0]["status"] != "complete"
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_active_generation_changed"
                )
            active = active_rows[0]
            active_read = (
                self._require_image_projection_read_manifest_in_transaction(
                    connection,
                    generation_id=expected_active_generation_id,
                    require_clean_canonical_match=True,
                )
            )
            if active_read["previous_generation_id"] != target_generation_id:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_target_not_predecessor"
                )

            target_generation, target_snapshot, target_manifest = (
                self._stored_projection_generation_snapshot_in_transaction(
                    connection,
                    generation_id=target_generation_id,
                )
            )
            if (
                target_generation["database_uuid"] != meta["database_uuid"]
                or int(target_generation["is_active"] or 0) != 0
                or target_generation["canonical_high_water_position"]
                != active["canonical_high_water_position"]
                or target_generation["compiler_id"] != active["compiler_id"]
                or target_generation["projector_version"]
                != active["projector_version"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_target_invalid"
                )
            target_read = self._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=target_generation_id,
                require_clean_canonical_match=True,
            )
            current_snapshot = (
                self._canonical_projection_rebuild_snapshot_in_transaction(
                    connection,
                    database_uuid=str(meta["database_uuid"]),
                    fixed_high_water=int(active["canonical_high_water_position"]),
                    projector_version=str(active["projector_version"]),
                )
            )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=target_generation_id,
                database_uuid=str(meta["database_uuid"]),
                fixed_high_water=int(active["canonical_high_water_position"]),
                compiler_id=str(active["compiler_id"]),
                projector_version=str(active["projector_version"]),
                snapshot=current_snapshot,
                require_active=False,
                require_physical=False,
            )

            managed_ids = (
                self._managed_image_projection_physical_ids_in_transaction(
                    connection,
                    database_uuid=str(meta["database_uuid"]),
                )
            )
            self._materialize_physical_image_projection_snapshot_in_transaction(
                connection,
                snapshot=target_snapshot,
                managed_physical_ids=managed_ids,
                materialized_at=str(target_generation["created_at"]),
            )
            observed_manifest = (
                self._build_full_image_projection_read_manifest_in_transaction(
                    connection,
                    generation=target_generation,
                    snapshot=target_snapshot,
                    canonical_manifest=target_manifest,
                )
            )
            if (
                not self._projection_manifest_is_clean(observed_manifest)
                or canonical_projection_manifest_json(observed_manifest)
                != canonical_projection_manifest_json(target_manifest)
                or observed_manifest["manifest_sha256"]
                != target_manifest["manifest_sha256"]
            ):
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_physical_parity_failed"
                )

            deactivated = connection.execute(
                """UPDATE image_projection_generations SET is_active=0
                    WHERE id=? AND status='complete' AND is_active=1""",
                (expected_active_generation_id,),
            )
            if deactivated.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_deactivation_failed"
                )
            activated = connection.execute(
                """UPDATE image_projection_generations SET is_active=1
                    WHERE id=? AND status='complete' AND is_active=0""",
                (target_generation_id,),
            )
            if activated.rowcount != 1:
                raise ImageAnalysisPersistenceError(
                    "image_projection_rollback_activation_failed"
                )
            self._require_projection_generation_snapshot_in_transaction(
                connection,
                generation_id=target_generation_id,
                database_uuid=str(meta["database_uuid"]),
                fixed_high_water=int(active["canonical_high_water_position"]),
                compiler_id=str(active["compiler_id"]),
                projector_version=str(active["projector_version"]),
                snapshot=current_snapshot,
                require_active=True,
                require_physical=True,
            )
            self._require_image_projection_read_manifest_in_transaction(
                connection,
                generation_id=target_generation_id,
                require_clean_canonical_match=True,
            )
            if (
                self._admit_image_database_identity(expected_database_identity)
                != expected_database_identity
            ):
                raise ImageAnalysisPersistenceError(
                    "image_database_scope_changed"
                )
        return {
            "generation_id": target_generation_id,
            "previous_generation_id": target_read["previous_generation_id"],
            "rolled_back_from_generation_id": expected_active_generation_id,
            "is_active": True,
        }

    def get_current_image_analysis_in_transaction(
        self,
        connection: sqlite3.Connection,
        asset_id: str,
    ) -> dict[str, object] | None:
        """Verify one current image result inside the caller's read snapshot."""

        row = connection.execute(
            """SELECT head.analysis_run_id,head.revision,head.content_sha256,
                      result.job_id,source.id AS source_id,
                      source.availability AS source_availability,
                      source.relative_path AS source_relative_path
                 FROM image_analysis_heads head
                 JOIN image_analysis_results result
                   ON result.asset_id=head.asset_id
                  AND result.analysis_run_id=head.analysis_run_id
                  AND result.revision=head.revision
                  AND result.content_sha256=head.content_sha256
                 JOIN asset_sources source ON source.id=result.source_id
                WHERE head.asset_id=?
                LIMIT 1""",
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        publication = self._published_image_response_in_transaction(
            connection,
            str(row["job_id"]),
        )
        if publication is None:
            raise ImageAnalysisPersistenceError("image_analysis_current_corrupt")
        result = publication.get("result")
        if (
            type(result) is not dict
            or result.get("analysis_run_id") != row["analysis_run_id"]
            or result.get("revision") != row["revision"]
            or result.get("content_sha256") != row["content_sha256"]
        ):
            raise ImageAnalysisPersistenceError("image_analysis_current_corrupt")
        try:
            analysis_binding = {
                "analysis_run_id": str(row["analysis_run_id"]),
                "revision": int(row["revision"]),
                "content_sha256": str(row["content_sha256"]),
            }
            projection = (
                self._verified_removed_projection_for_current_head_in_transaction(
                    connection,
                    asset_id=asset_id,
                    analysis_binding=analysis_binding,
                    source_id=str(row["source_id"]),
                )
            )
            if projection is None:
                projection = self._verified_projection_for_change_in_transaction(
                    connection,
                    publication=publication,
                    source_relative_path=str(row["source_relative_path"]),
                )
        except ImageAnalysisPersistenceError as exc:
            if exc.code != "image_analysis_projection_corrupt":
                raise
            projection = {
                "status": "unavailable",
                "outcome": None,
                "reason_code": exc.code,
                "receipt_sha256": None,
                "generation_id": None,
                "processing_generation_id": None,
            }
        if projection is None:
            projection = {
                "status": "pending",
                "outcome": None,
                "reason_code": None,
                "receipt_sha256": None,
                "generation_id": None,
                "processing_generation_id": None,
            }
        if row["source_availability"] != "available" and projection["status"] == "current":
            projection = {
                **projection,
                "status": "unavailable",
                "reason_code": "image_source_unavailable",
            }
        return {
            "asset_id": asset_id,
            "analysis_binding": analysis_binding,
            "result": result,
            "source": {
                "source_id": str(row["source_id"]),
                "availability": str(row["source_availability"]),
            },
            "projection": {
                **projection,
                "change_position": int(
                    projection.get(
                        "change_position",
                        publication["change_position"],
                    )
                ),
            },
        }

    def get_current_image_analysis(
        self,
        asset_id: str,
    ) -> dict[str, object] | None:
        """Return a strictly verified current image result and projection freshness."""

        with self.transaction() as connection:  # type: ignore[attr-defined]
            return self.get_current_image_analysis_in_transaction(connection, asset_id)

    def resolve_image_asset_reference(
        self,
        reference_id: str,
        *,
        library_root_id: str | None = None,
    ) -> dict[str, object] | None:
        """Resolve a canonical ID or an exact-scoped immutable legacy alias.

        Alias compatibility stops at this boundary: the returned identity is
        always the canonical asset ID, and any analysis binding comes from the
        same strictly verified read path used by current consumers.
        """

        with self.transaction() as connection:  # type: ignore[attr-defined]
            return self.resolve_image_asset_reference_in_transaction(
                connection,
                reference_id,
                library_root_id=library_root_id,
            )

    def resolve_image_asset_reference_in_transaction(
        self,
        connection: sqlite3.Connection,
        reference_id: str,
        *,
        library_root_id: str | None = None,
    ) -> dict[str, object] | None:
        """Resolve one canonical ID or scoped legacy alias in a caller-owned snapshot."""

        if type(reference_id) is not str or not 1 <= len(reference_id) <= 256:
            raise ImageAnalysisPersistenceError("image_reference_invalid")
        if library_root_id is not None and (
            type(library_root_id) is not str or not library_root_id
        ):
            raise ImageAnalysisPersistenceError("image_alias_scope_invalid")
        meta = self._image_meta_in_transaction(connection)
        canonical = connection.execute(
                "SELECT id FROM assets WHERE id=? AND kind='image'",
                (reference_id,),
            ).fetchone()
        alias = connection.execute(
                """SELECT alias.legacy_id,alias.canonical_asset_id,
                          alias.library_root_id,alias.source_id,
                          alias.alias_scope,alias.alias_sha256,
                          source.asset_id AS source_asset_id,
                          source.library_root_id AS source_library_root_id
                     FROM legacy_image_aliases alias
                     JOIN asset_sources source ON source.id=alias.source_id
                    WHERE alias.database_uuid=? AND alias.legacy_id=?""",
                (meta["database_uuid"], reference_id),
            ).fetchone()
        if canonical is not None and alias is not None:
            if alias["canonical_asset_id"] != canonical["id"]:
                raise ImageAnalysisPersistenceError("image_reference_ambiguous")
            alias = None
        if canonical is None and alias is None:
            return None

        if alias is not None:
            if library_root_id is None:
                raise ImageAnalysisPersistenceError("image_alias_scope_required")
            identity = {
                "database_uuid": str(meta["database_uuid"]),
                "legacy_id": str(alias["legacy_id"]),
                "canonical_asset_id": str(alias["canonical_asset_id"]),
                "library_root_id": str(alias["library_root_id"]),
                "source_id": str(alias["source_id"]),
                "alias_scope": str(alias["alias_scope"]),
            }
            if (
                alias["library_root_id"] != library_root_id
                or alias["source_asset_id"] != alias["canonical_asset_id"]
                or alias["source_library_root_id"] != alias["library_root_id"]
                or alias["alias_scope"] != "legacy-image-index/v1"
                or canonical_sha256(identity) != alias["alias_sha256"]
            ):
                raise ImageAnalysisPersistenceError("legacy_alias_conflict")
            asset_id = str(alias["canonical_asset_id"])
            reference_kind = "legacy_alias"
            alias_sha256 = str(alias["alias_sha256"])
        else:
            assert canonical is not None
            asset_id = str(canonical["id"])
            reference_kind = "canonical"
            alias_sha256 = None

        current = self.get_current_image_analysis_in_transaction(
            connection,
            asset_id,
        )
        return {
            "reference_id": reference_id,
            "reference_kind": reference_kind,
            "asset_id": asset_id,
            "alias_sha256": alias_sha256,
            "analysis_binding": (
                current["analysis_binding"] if current is not None else None
            ),
            "projection": current["projection"] if current is not None else None,
        }


__all__ = [
    "ExactImageSource",
    "ImageAnalysisPersistenceError",
    "ImageAnalysisPersistenceMixin",
]
