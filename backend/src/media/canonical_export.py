"""Native-authorized canonical export orchestration.

This module deliberately does not reuse the legacy ``timelines`` / ``render_jobs``
lane.  Core owns the immutable Export/Usage ledger, the artifact module owns
verified local bytes, and this runner is only the recoverable outbox between
those two authorities.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

from core.media_db import (
    CanonicalExportCommandResult,
    CanonicalExportIntegrityError,
    CanonicalExportJobConflictError,
    CanonicalExportNativeNonceConflictError,
    CanonicalExportReceiptConflictError,
    MediaRepository,
)

from .canonical_export_artifacts import (
    CANONICAL_EXPORT_ARTIFACTS_VERSION,
    CanonicalExportArtifactError,
    build_lightweight_package,
    inspect_existing_package,
    quarantine_published_package,
    render_canonical_master,
    script_artifact_proof,
)
from .video import MediaCancelled, binary_capability, resolve_binary
from .worker_admission import OneShotJobAdmissionAuthority


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_NONCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,199}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
_PACKAGE_BASENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_DECIMAL_DEVICE = re.compile(r"^(?:0|[1-9][0-9]{0,30})$")
_DECIMAL_INODE = re.compile(r"^[1-9][0-9]{0,30}$")
_ACTIVE_STATUSES = frozenset(
    {"requested", "rendering", "packaging", "commit_pending", "cancelling"}
)
_RECOVERY_REQUIRED_STATUSES = frozenset({"interrupted"})
CANONICAL_EXPORT_WORKER_ACTIONS = frozenset({"package"})
CANONICAL_EXPORT_JOB_IDENTITY_FIELDS = (
    "id",
    "project_id",
    "operation_id",
    "presentation_sha256",
    "request_sha256",
    "timeline_revision",
    "timeline_revision_sha256",
    "timeline_content_sha256",
    "timeline_source_bindings_sha256",
    "output_root_id",
    "permission_fingerprint",
    "package_basename",
    "profile",
    "attempt",
    "created_at",
)


def _canonical_export_job_identity(job: Mapping[str, object]) -> tuple[str, ...]:
    """Flatten the immutable worker binding without trusting nested repr order."""

    timeline = job.get("timeline_binding")
    output = job.get("output_binding")
    flattened = {
        **job,
        "timeline_revision": (
            timeline.get("revision") if isinstance(timeline, Mapping) else None
        ),
        "timeline_revision_sha256": (
            timeline.get("revision_sha256")
            if isinstance(timeline, Mapping)
            else None
        ),
        "timeline_content_sha256": (
            timeline.get("timeline_content_sha256")
            if isinstance(timeline, Mapping)
            else None
        ),
        "timeline_source_bindings_sha256": (
            timeline.get("source_bindings_sha256")
            if isinstance(timeline, Mapping)
            else None
        ),
        "output_root_id": (
            output.get("output_root_id") if isinstance(output, Mapping) else None
        ),
        "permission_fingerprint": (
            output.get("permission_fingerprint")
            if isinstance(output, Mapping)
            else None
        ),
        "package_basename": (
            output.get("package_basename") if isinstance(output, Mapping) else None
        ),
        "profile": output.get("profile") if isinstance(output, Mapping) else None,
    }
    return tuple(
        str(flattened.get(field) or "")
        for field in CANONICAL_EXPORT_JOB_IDENTITY_FIELDS
    )


class CanonicalExportServiceError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: object | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details


def _closed_object(
    value: object,
    fields: set[str],
    *,
    code: str,
) -> dict[str, object]:
    if type(value) is not dict or set(value) != fields:
        raise CanonicalExportServiceError(
            code,
            "Canonical export request has an unsupported shape.",
        )
    return value


def _project_id(value: object) -> str:
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise CanonicalExportServiceError(
            "invalid_project_id",
            "Creative project identity is invalid.",
        )
    return value


def _safe_job_projection(job: Mapping[str, object]) -> dict[str, object]:
    """Remove destination authority while preserving verifiable job identity."""

    output = job.get("output_binding")
    timeline = job.get("timeline_binding")
    result = job.get("result")
    if not isinstance(output, Mapping) or not isinstance(timeline, Mapping):
        raise CanonicalExportIntegrityError("canonical_export_job_projection_invalid")
    return {
        "id": job.get("id"),
        "project_id": job.get("project_id"),
        "operation_id": job.get("operation_id"),
        "presentation_sha256": job.get("presentation_sha256"),
        "timeline_binding": deepcopy(dict(timeline)),
        "package": {
            "package_basename": output.get("package_basename"),
            "profile": output.get("profile"),
        },
        "status": job.get("status"),
        "stage": job.get("stage"),
        "progress": job.get("progress"),
        "attempt": job.get("attempt"),
        "cancel_requested": job.get("cancel_requested"),
        "error": deepcopy(job.get("error")),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "heartbeat_at": job.get("heartbeat_at"),
        "finished_at": job.get("finished_at"),
        "result": deepcopy(result),
    }


def canonical_export_workspace_summary_in_transaction(
    repository: MediaRepository,
    connection,
    *,
    project_id: str,
    timeline_summary: Mapping[str, object],
    validated_timeline_head: Mapping[str, object] | None,
) -> tuple[dict[str, object], bool]:
    """Project renderer-safe export state inside the canonical workspace read."""

    jobs = repository.list_canonical_export_jobs_in_transaction(
        connection,
        project_id=project_id,
        limit=1,
        validated_timeline_head=validated_timeline_head,
    )
    latest = jobs[0] if jobs else None
    latest_projection = None
    if latest is not None:
        output = latest.get("output_binding")
        if not isinstance(output, Mapping):
            raise CanonicalExportIntegrityError(
                "canonical_export_workspace_projection_invalid"
            )
        latest_projection = {
            "job_id": latest.get("id"),
            "status": latest.get("status"),
            "package_basename": output.get("package_basename"),
        }

    if latest is not None and latest.get("status") in _RECOVERY_REQUIRED_STATUSES:
        return (
            {
                "state": "blocked",
                "reason_code": "export_recovery_required",
                "latest_job": latest_projection,
            },
            False,
        )

    timeline_state = timeline_summary.get("state")
    if timeline_state != "current":
        return (
            {
                "state": "blocked",
                "reason_code": (
                    "timeline_missing" if timeline_state == "missing" else "timeline_stale"
                ),
                "latest_job": latest_projection,
            },
            False,
        )
    if latest is not None and latest.get("status") in _ACTIVE_STATUSES:
        return (
            {
                "state": "in_progress",
                "reason_code": "export_in_progress",
                "latest_job": latest_projection,
            },
            False,
        )
    return (
        {
            "state": "available",
            "reason_code": "requires_native_confirmation",
            "latest_job": latest_projection,
        },
        True,
    )


class CanonicalExportService:
    def __init__(
        self,
        repository: MediaRepository,
        runner: "CanonicalExportJobRunner",
    ) -> None:
        self.repository = repository
        self.runner = runner

    def presentation(self, project_id: str) -> dict[str, object]:
        self.runner.assert_healthy()
        envelope = self.repository.build_canonical_export_presentation(
            _project_id(project_id)
        )
        return {
            "object": "canonical_export.presentation_envelope",
            "schema_version": "1",
            **envelope,
        }

    def start(
        self,
        project_id: str,
        payload: object,
        *,
        runtime_epoch: str,
        idempotency_key: str,
    ) -> CanonicalExportCommandResult:
        self.runner.assert_healthy()
        normalized_project = _project_id(project_id)
        body = _closed_object(
            payload,
            {
                "presentation",
                "presentation_sha256",
                "native_gesture_nonce",
                "destination",
            },
            code="invalid_canonical_export_request",
        )
        destination = _closed_object(
            body["destination"],
            {"canonical_path", "package_basename", "selection_identity"},
            code="invalid_canonical_export_destination",
        )
        selection_identity = _closed_object(
            destination["selection_identity"],
            {"device", "inode"},
            code="invalid_canonical_export_destination",
        )
        canonical_path = destination["canonical_path"]
        package_basename = destination["package_basename"]
        selected_device = selection_identity["device"]
        selected_inode = selection_identity["inode"]
        if (
            type(canonical_path) is not str
            or not 1 <= len(canonical_path) <= 4_096
            or "\x00" in canonical_path
            or not Path(canonical_path).is_absolute()
        ):
            raise CanonicalExportServiceError(
                "invalid_canonical_export_destination",
                "Native export destination is invalid.",
            )
        if (
            type(package_basename) is not str
            or _PACKAGE_BASENAME.fullmatch(package_basename) is None
        ):
            raise CanonicalExportServiceError(
                "invalid_canonical_export_destination",
                "Canonical export package name is invalid.",
            )
        if (
            type(selected_device) is not str
            or _DECIMAL_DEVICE.fullmatch(selected_device) is None
            or type(selected_inode) is not str
            or _DECIMAL_INODE.fullmatch(selected_inode) is None
        ):
            raise CanonicalExportServiceError(
                "invalid_canonical_export_destination",
                "Native export directory identity is invalid.",
            )
        nonce = body["native_gesture_nonce"]
        if type(nonce) is not str or _NONCE.fullmatch(nonce) is None:
            raise CanonicalExportServiceError(
                "native_confirmation_required",
                "A bounded native gesture nonce is required.",
                status=403,
            )
        if (
            type(runtime_epoch) is not str
            or _IDENTIFIER.fullmatch(runtime_epoch) is None
            or _IDEMPOTENCY_KEY.fullmatch(idempotency_key) is None
        ):
            raise CanonicalExportServiceError(
                "invalid_canonical_export_authority",
                "Canonical export authority context is invalid.",
                status=403,
            )
        if type(body["presentation"]) is not dict or type(
            body["presentation_sha256"]
        ) is not str:
            raise CanonicalExportServiceError(
                "invalid_canonical_export_request",
                "Canonical export presentation is invalid.",
            )

        registered: Mapping[str, object] | None = None
        try:
            try:
                registered = self.repository.register_user_export_root(
                    Path(canonical_path),
                    expected_device=int(selected_device),
                    expected_inode=int(selected_inode),
                )
                envelope = self.repository.build_canonical_export_envelope(
                    presentation=body["presentation"],
                    presentation_sha256=body["presentation_sha256"],
                    main_native_user_gesture=True,
                    runtime_epoch=runtime_epoch,
                    native_nonce=nonce,
                    output_root_id=str(registered["id"]),
                    permission_fingerprint=str(
                        registered["permission_fingerprint"]
                    ),
                    package_basename=package_basename,
                )
                result = self.repository.execute_canonical_export_command(
                    authenticated_principal="main_native_user_gesture",
                    project_id=normalized_project,
                    idempotency_key=idempotency_key,
                    envelope=envelope,
                )
            except CanonicalExportServiceError:
                raise
            except (
                CanonicalExportIntegrityError,
                CanonicalExportJobConflictError,
                CanonicalExportNativeNonceConflictError,
                CanonicalExportReceiptConflictError,
            ):
                raise
            except ValueError as exc:
                raise CanonicalExportServiceError(
                    "invalid_canonical_export_request",
                    "Canonical export request is invalid.",
                ) from exc
        finally:
            if registered is not None:
                self.repository.discard_prepared_user_export_root(
                    str(registered["id"]),
                    permission_fingerprint=str(
                        registered["permission_fingerprint"]
                    ),
                )
        self.runner.submit(result.resource_id)
        return result

    def list_jobs(self, project_id: str, *, limit: int = 50) -> dict[str, object]:
        normalized_project = _project_id(project_id)
        jobs = self.repository.list_canonical_export_jobs(
            project_id=normalized_project,
            limit=limit,
        )
        return {
            "object": "canonical_export.job_list",
            "schema_version": "1",
            "project_id": normalized_project,
            "jobs": [_safe_job_projection(job) for job in jobs],
        }

    def read_job(self, project_id: str, job_id: str) -> dict[str, object]:
        normalized_project = _project_id(project_id)
        if type(job_id) is not str or _IDENTIFIER.fullmatch(job_id) is None:
            raise CanonicalExportServiceError(
                "canonical_export_job_not_found",
                "Canonical export job does not exist.",
                status=404,
            )
        job = self.repository.get_canonical_export_job(job_id)
        if job is None or job.get("project_id") != normalized_project:
            raise CanonicalExportServiceError(
                "canonical_export_job_not_found",
                "Canonical export job does not exist.",
                status=404,
            )
        return {
            "object": "canonical_export.job",
            "schema_version": "1",
            **_safe_job_projection(job),
        }

    def read_revision(self, project_id: str, revision: int) -> dict[str, object]:
        normalized_project = _project_id(project_id)
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise CanonicalExportServiceError(
                "canonical_export_revision_not_found",
                "Canonical Export Revision does not exist.",
                status=404,
            )
        value = self.repository.get_canonical_export_revision(
            project_id=normalized_project,
            revision=revision,
        )
        if value is None:
            raise CanonicalExportServiceError(
                "canonical_export_revision_not_found",
                "Canonical Export Revision does not exist.",
                status=404,
            )
        output = value.get("output_binding")
        if not isinstance(output, Mapping):
            raise CanonicalExportIntegrityError(
                "canonical_export_revision_projection_invalid"
            )
        return {
            "object": "canonical_export.revision",
            "schema_version": "1",
            "project_id": value.get("project_id"),
            "revision": value.get("revision"),
            "revision_sha256": value.get("revision_sha256"),
            "job_id": value.get("job_id"),
            "operation_id": value.get("operation_id"),
            "timeline_binding": deepcopy(value.get("timeline_binding")),
            "package": {
                "package_basename": output.get("package_basename"),
                "profile": output.get("profile"),
            },
            "video": deepcopy(value.get("video")),
            "script": deepcopy(value.get("script")),
            "package_manifest_sha256": value.get("package_manifest_sha256"),
            "usage_occurrences": deepcopy(value.get("usage_occurrences")),
            "usage_sha256": value.get("usage_sha256"),
            "runtime_manifest_sha256": value.get("runtime_manifest_sha256"),
            "human_usage_sha256": value.get("human_usage_sha256"),
            "completion_marker_sha256": value.get(
                "completion_marker_sha256"
            ),
            "completed_at": value.get("completed_at"),
        }


class CanonicalExportJobRunner:
    """Single-worker filesystem outbox for canonical Export/Usage admission."""

    def __init__(self, repository: MediaRepository, cache_root: Path) -> None:
        self.repository = repository
        self.cache_root = cache_root.expanduser().resolve() / "canonical-exports"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="memolens-canonical-export",
        )
        self._submitted: set[str] = set()
        self._unresolved: set[str] = set()
        self._lock = threading.Lock()
        self._admission_authority = OneShotJobAdmissionAuthority()

    def assert_healthy(self) -> None:
        """Fail closed while a worker outcome still needs trusted recovery."""

        with self._lock:
            unresolved = tuple(sorted(self._unresolved))
        if unresolved:
            raise CanonicalExportIntegrityError(
                "canonical_export_runner_recovery_required:"
                + ",".join(unresolved)
            )

    def _record_unresolved(self, job_id: str) -> None:
        with self._lock:
            self._unresolved.add(job_id)

    def _clear_unresolved(self, job_id: str) -> None:
        with self._lock:
            self._unresolved.discard(job_id)

    def _refresh_unresolved_outcomes(self) -> tuple[str, ...]:
        """Clear in-memory uncertainty once Core exposes a terminal fact."""

        with self._lock:
            candidates = tuple(self._unresolved)
        remaining: list[str] = []
        for job_id in candidates:
            try:
                current = self.repository.get_canonical_export_job(job_id)
            except Exception:
                remaining.append(job_id)
                continue
            if current is None or current.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                self._clear_unresolved(job_id)
            else:
                remaining.append(job_id)
        return tuple(sorted(remaining))

    def submit(self, job_id: str) -> None:
        job = self.repository.get_canonical_export_job(job_id)
        if (
            job is None
            or str(job.get("id") or "") != job_id
            or job.get("status") not in {"requested", "interrupted"}
            or bool(job.get("cancel_requested"))
        ):
            return
        identity = _canonical_export_job_identity(job)
        with self._lock:
            if job_id in self._submitted:
                return
            self._submitted.add(job_id)
        ticket = self._admission_authority.issue(job_id, identity)
        try:
            self._executor.submit(self._run_and_release, job_id, ticket)
        except Exception:
            self._admission_authority.revoke(ticket)
            with self._lock:
                self._submitted.discard(job_id)
            if not self._mark_recoverable(
                job_id,
                "canonical_export_submission_failed",
            ):
                self._record_unresolved(job_id)
            raise

    def shutdown(self) -> None:
        with self._lock:
            submitted = tuple(self._submitted)
        for job_id in submitted:
            try:
                self.repository.request_canonical_export_cancel(job_id)
            except Exception:
                pass
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _run_and_release(self, job_id: str, admission_ticket: str) -> None:
        try:
            try:
                self._run(job_id, admission_ticket=admission_ticket)
            except Exception:
                if not self._mark_recoverable(
                    job_id,
                    "canonical_export_worker_interrupted",
                ):
                    self._record_unresolved(job_id)
        finally:
            with self._lock:
                self._submitted.discard(job_id)

    def _cancelled(self, job_id: str) -> bool:
        job = self.repository.get_canonical_export_job(job_id)
        return bool(
            job is None
            or job.get("cancel_requested")
            or job.get("status") in {"cancelling", "cancelled"}
        )

    @staticmethod
    def _error(code: str, *, retryable: bool = False) -> dict[str, object]:
        return {
            "code": code,
            "message": "Canonical export did not complete.",
            "retryable": retryable,
        }

    def _mark_failed(
        self,
        job_id: str,
        code: str,
        *,
        retryable: bool = False,
    ) -> bool:
        try:
            current = self.repository.get_canonical_export_job(job_id)
            if current is None or current.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
            }:
                self._clear_unresolved(job_id)
                return True
            if current.get("status") == "cancelling" or current.get(
                "cancel_requested"
            ):
                self.repository.update_canonical_export_job(
                    job_id=job_id,
                    status="cancelled",
                    stage="cancelled",
                    progress=float(current.get("progress") or 0),
                    expected_status=str(current["status"]),
                )
                self._clear_unresolved(job_id)
                return True
            self.repository.update_canonical_export_job(
                job_id=job_id,
                status="failed",
                stage="failed",
                progress=float(current.get("progress") or 0),
                error=self._error(code, retryable=retryable),
                expected_status=str(current["status"]),
            )
            self._clear_unresolved(job_id)
            return True
        except Exception:
            # Integrity failure is intentionally not hidden by a second write.
            return False

    def _mark_recoverable(self, job_id: str, code: str) -> bool:
        try:
            current = self.repository.get_canonical_export_job(job_id)
            if current is None or current.get("status") in {
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
            }:
                if current is not None and current.get("status") == "interrupted":
                    self._record_unresolved(job_id)
                else:
                    self._clear_unresolved(job_id)
                return True
            self.repository.update_canonical_export_job(
                job_id=job_id,
                status="interrupted",
                stage="interrupted",
                progress=float(current.get("progress") or 0),
                error=self._error(code, retryable=True),
                expected_status=str(current["status"]),
            )
            self._record_unresolved(job_id)
            return True
        except Exception:
            # A persistent DB/integrity failure is surfaced by the activation
            # recovery gate; do not fabricate a terminal state here.
            return False

    @staticmethod
    def _job_matches_timeline(
        job: Mapping[str, object],
        timeline: Mapping[str, object],
    ) -> bool:
        binding = job.get("timeline_binding")
        return bool(
            isinstance(binding, Mapping)
            and binding.get("revision") == timeline.get("revision")
            and binding.get("revision_sha256") == timeline.get("revision_sha256")
            and binding.get("timeline_content_sha256")
            == timeline.get("timeline_content_sha256")
            and binding.get("source_bindings_sha256")
            == timeline.get("source_bindings_sha256")
        )

    def _bound_inputs(
        self,
        job: Mapping[str, object],
    ) -> tuple[
        dict[str, object],
        list[dict[str, object]],
        str,
        int,
    ]:
        project_id = str(job["project_id"])
        binding = job["timeline_binding"]
        if not isinstance(binding, Mapping):
            raise CanonicalExportIntegrityError("canonical_export_job_binding_invalid")
        output = job.get("output_binding")
        if not isinstance(output, Mapping):
            raise CanonicalExportIntegrityError("canonical_export_output_binding_invalid")
        if output.get("profile") != "export-1080p":
            raise CanonicalExportJobConflictError(
                "canonical_export_profile_binding_stale"
            )
        root, _output_path, output_root_fd = self.repository.open_output_root_fd(
            str(output["output_root_id"])
        )
        if (
            root.get("kind") != "user_export"
            or root.get("permission_fingerprint")
            != output.get("permission_fingerprint")
        ):
            os.close(output_root_fd)
            raise CanonicalExportJobConflictError(
                "canonical_export_output_root_stale"
            )
        try:
            occurrences = self.repository.build_canonical_usage_occurrences(
                project_id=project_id,
                timeline_revision=int(binding["revision"]),
                timeline_revision_sha256=str(binding["revision_sha256"]),
            )
            timeline = self.repository.get_canonical_timeline_revision(
                project_id,
                int(binding["revision"]),
            )
            if timeline is None or not self._job_matches_timeline(job, timeline):
                raise CanonicalExportJobConflictError(
                    "canonical_export_timeline_binding_stale"
                )
            full_binding = self.repository._canonical_export_timeline_binding(
                timeline
            )
            blueprint_binding = full_binding["blueprint_binding"]
            script_projection = (
                self.repository.build_canonical_export_script_projection(
                    project_id=project_id,
                    blueprint_binding=blueprint_binding,
                )
            )
            script_text = script_projection.get("text")
            if type(script_text) is not str:
                raise CanonicalExportIntegrityError(
                    "canonical_export_script_projection_invalid"
                )
            return timeline, occurrences, script_text, output_root_fd
        except BaseException:
            os.close(output_root_fd)
            raise

    def _handle_published_exception(
        self,
        *,
        job_id: str,
        cause: Exception,
        output_root_fd: int | None,
        package_basename: str | None,
        expected_manifest: Mapping[str, object] | None,
    ) -> bool:
        """Preserve unknown outcomes; quarantine only a proven rejection."""

        try:
            current = self.repository.get_canonical_export_job(job_id)
        except Exception:
            current = None
            trusted_job_read = False
        else:
            trusted_job_read = current is not None
        if current is not None and current.get("status") == "succeeded":
            # Core success is authoritative. A wrapper/transport failure after
            # that commit must never remove the package it admitted.
            self._clear_unresolved(job_id)
            return True

        deterministic_rejection = isinstance(
            cause,
            (
                CanonicalExportIntegrityError,
                CanonicalExportJobConflictError,
                CanonicalExportArtifactError,
            ),
        ) or bool(
            current is not None
            and current.get("status") in {"failed", "cancelled"}
        )
        if not trusted_job_read or not deterministic_rejection:
            if trusted_job_read:
                if not self._mark_recoverable(
                    job_id,
                    "canonical_export_published_outcome_unknown",
                ):
                    self._record_unresolved(job_id)
            else:
                self._record_unresolved(job_id)
            return False

        if (
            output_root_fd is not None
            and package_basename is not None
            and expected_manifest is not None
        ):
            try:
                quarantine_published_package(
                    output_root_fd=output_root_fd,
                    package_basename=package_basename,
                    expected_package_manifest=expected_manifest,
                )
            except Exception:
                self._mark_recoverable(
                    job_id,
                    "canonical_export_quarantine_pending",
                )
                self._record_unresolved(job_id)
                return False
        marked = self._mark_failed(
            job_id,
            "canonical_export_completion_failed",
        )
        if not marked:
            self._record_unresolved(job_id)
        return marked

    def _run(self, job_id: str, *, admission_ticket: object = None) -> None:
        job = self.repository.get_canonical_export_job(job_id)
        if not self._admission_authority.consume(
            admission_ticket,
            job_id=job_id,
            identity=_canonical_export_job_identity(job or {}),
        ):
            return
        if job is None or job.get("status") in {"succeeded", "failed", "cancelled"}:
            return
        if job.get("status") not in {"requested", "interrupted"}:
            return
        package_published = False
        output_root_fd: int | None = None
        package_basename: str | None = None
        expected_manifest: dict[str, object] | None = None
        # Authority/scope denial is not a render outcome. Keep the requested
        # durable fact unchanged so an invalid root/source binding cannot gain
        # even a worker-owned status mutation as a side effect.
        try:
            timeline, occurrences, script_text, output_root_fd = (
                self._bound_inputs(job)
            )
        except (
            CanonicalExportIntegrityError,
            CanonicalExportJobConflictError,
            ValueError,
        ):
            return
        try:
            # The immutable main-native operation and its full ledger are
            # validated by get_canonical_export_job above.  Bind the native
            # root and canonical source set before the first job mutation,
            # media-byte read, workspace write, or process capability probe.
            admitted_identity = _canonical_export_job_identity(job)
            job = self.repository.update_canonical_export_job(
                job_id=job_id,
                status="rendering",
                stage="rendering",
                progress=0.05,
                expected_status=str(job["status"]),
            )
            if _canonical_export_job_identity(job) != admitted_identity:
                raise CanonicalExportJobConflictError(
                    "canonical_export_job_identity_changed"
                )
            timeline_document = timeline.get("timeline")
            source_bindings = timeline.get("source_bindings")
            if not isinstance(timeline_document, Mapping) or type(
                source_bindings
            ) is not list:
                raise CanonicalExportIntegrityError(
                    "canonical_export_timeline_invalid"
                )
            ffmpeg_binary = resolve_binary("ffmpeg")
            if ffmpeg_binary is None or not binary_capability("ffmpeg").get(
                "available"
            ):
                raise CanonicalExportArtifactError(
                    "ffmpeg_unavailable",
                    "Canonical export renderer is unavailable.",
                )

            with tempfile.TemporaryDirectory(
                prefix=f"{job_id}-",
                dir=self.cache_root,
            ) as directory:
                workspace = Path(directory)
                master = workspace / "master.mp4"
                render_proof = render_canonical_master(
                    timeline_document,
                    source_bindings,
                    source_resolver=self.repository.get_asset_source,
                    job_workspace=workspace,
                    master_output=master,
                    ffmpeg_binary=ffmpeg_binary,
                    cancelled=lambda: self._cancelled(job_id),
                    runtime_identity={
                        "artifacts": CANONICAL_EXPORT_ARTIFACTS_VERSION,
                        "ffmpeg_supported": True,
                        "ffprobe_supported": bool(
                            binary_capability("ffprobe").get("available")
                        ),
                    },
                )
                if render_proof.get("usage_occurrences") != occurrences:
                    raise CanonicalExportIntegrityError(
                        "canonical_export_usage_derivation_mismatch"
                    )
                script_proof = script_artifact_proof(script_text)
                output = job["output_binding"]
                assert isinstance(output, Mapping)
                package_basename = str(output["package_basename"])
                self.repository.update_canonical_export_job(
                    job_id=job_id,
                    status="packaging",
                    stage="packaging",
                    progress=0.80,
                    expected_status="rendering",
                )
                attestation = self.repository.stage_canonical_export_commit(
                    job_id=job_id,
                    video_sha256=str(render_proof["video_sha256"]),
                    video_size_bytes=int(render_proof["video_size_bytes"]),
                    video_duration_ms=int(render_proof["video_duration_ms"]),
                    script_sha256=str(script_proof["sha256"]),
                    script_size_bytes=int(script_proof["size_bytes"]),
                    occurrences=occurrences,
                    runtime_manifest=render_proof["runtime_manifest"],
                )
                artifact_proof = attestation.get("artifact_proof")
                manifest = (
                    artifact_proof.get("package_manifest")
                    if isinstance(artifact_proof, Mapping)
                    else None
                )
                if not isinstance(manifest, Mapping):
                    raise CanonicalExportIntegrityError(
                        "canonical_export_commit_attestation_invalid"
                    )
                expected_manifest = deepcopy(dict(manifest))

                def package_stage(stage: str) -> None:
                    nonlocal package_published
                    if stage == "package_published":
                        package_published = True

                package_proof = build_lightweight_package(
                    verified_master=master,
                    render_proof=render_proof,
                    expected_package_manifest=expected_manifest,
                    script_text=script_text,
                    output_root_fd=output_root_fd,
                    package_basename=package_basename,
                    cancelled=lambda: self._cancelled(job_id),
                    fault_inject=package_stage,
                )
                revision = self.repository.complete_canonical_export_job(
                    job_id=job_id,
                    artifact_proof=package_proof["core_artifact_proof"],
                    occurrences=package_proof["usage_occurrences"],
                    runtime_manifest=package_proof["runtime_manifest"],
                )
                if revision.get("job_id") != job_id:
                    raise CanonicalExportIntegrityError(
                        "canonical_export_completion_identity_mismatch"
                    )
                self._clear_unresolved(job_id)
        except MediaCancelled:
            if package_published:
                if not self._mark_recoverable(
                    job_id,
                    "canonical_export_published_outcome_unknown",
                ):
                    self._record_unresolved(job_id)
            elif not self._mark_failed(job_id, "canonical_export_cancelled"):
                self._record_unresolved(job_id)
        except CanonicalExportArtifactError as exc:
            if package_published:
                if not self._mark_recoverable(
                    job_id,
                    "canonical_export_published_outcome_unknown",
                ):
                    self._record_unresolved(job_id)
            elif not self._mark_failed(
                job_id,
                exc.code,
                retryable=exc.code.endswith("unavailable"),
            ):
                self._record_unresolved(job_id)
        except Exception as exc:
            if package_published:
                self._handle_published_exception(
                    job_id=job_id,
                    cause=exc,
                    output_root_fd=output_root_fd,
                    package_basename=package_basename,
                    expected_manifest=expected_manifest,
                )
            elif not self._mark_failed(
                job_id,
                "canonical_export_completion_failed",
            ):
                self._record_unresolved(job_id)
        finally:
            if output_root_fd is not None:
                try:
                    os.close(output_root_fd)
                except OSError:
                    pass

    def reconcile_interrupted_storage(self) -> None:
        # A worker may have observed an unknown return even though Core durably
        # committed a terminal outcome. Recovery must retire that in-memory
        # uncertainty even when the terminal job is absent from the storage
        # recovery query.
        self._refresh_unresolved_outcomes()
        jobs = self.repository.list_canonical_export_recovery_jobs()
        unresolved: list[str] = []
        for job in jobs:
            output_root_fd: int | None = None
            package_basename: str | None = None
            expected_manifest: dict[str, object] | None = None
            try:
                job_id = str(job["id"])
                attestation = self.repository.get_canonical_export_commit_attestation(
                    job_id
                )
                if attestation is None:
                    if not self._mark_failed(
                        job_id,
                        "canonical_export_commit_attestation_missing",
                    ):
                        unresolved.append(job_id)
                    continue
                artifact_proof = attestation.get("artifact_proof")
                manifest = (
                    artifact_proof.get("package_manifest")
                    if isinstance(artifact_proof, Mapping)
                    else None
                )
                if not isinstance(manifest, Mapping):
                    raise CanonicalExportIntegrityError(
                        "canonical_export_commit_attestation_invalid"
                    )
                expected_manifest = deepcopy(dict(manifest))
                output = job["output_binding"]
                assert isinstance(output, Mapping)
                root, _output_path, output_root_fd = (
                    self.repository.open_output_root_fd(
                    str(output["output_root_id"])
                    )
                )
                if (
                    root.get("kind") != "user_export"
                    or root.get("permission_fingerprint")
                    != output.get("permission_fingerprint")
                ):
                    raise CanonicalExportJobConflictError(
                        "canonical_export_output_root_stale"
                    )
                package_basename = str(output["package_basename"])
                inspected = inspect_existing_package(
                    output_root_fd=output_root_fd,
                    package_basename=package_basename,
                    expected_package_manifest=expected_manifest,
                )
                package_state = inspected.get("state")
                if package_state == "missing":
                    if not self._mark_failed(
                        job_id,
                        "canonical_export_package_missing",
                    ):
                        unresolved.append(job_id)
                    continue
                if package_state != "complete":
                    # A visible final directory that cannot be proven against
                    # the immutable attestation must never be left looking
                    # canonical while the runner reports healthy.
                    raise CanonicalExportIntegrityError(
                        f"canonical_export_package_{package_state or 'invalid'}"
                    )
                package_proof = inspected["proof"]
                if not isinstance(package_proof, Mapping):
                    raise CanonicalExportIntegrityError(
                        "canonical_export_package_proof_invalid"
                    )
                runtime_manifest = package_proof.get("runtime_manifest")
                if not isinstance(runtime_manifest, Mapping):
                    raise CanonicalExportIntegrityError(
                        "canonical_export_package_proof_invalid"
                    )
                manifest_timeline = manifest.get("timeline_binding")
                job_timeline = job.get("timeline_binding")
                if not (
                    manifest.get("job_id") == job.get("id")
                    and manifest.get("export_id") == job.get("operation_id")
                    and manifest.get("project_id") == job.get("project_id")
                    and manifest.get("package_basename") == package_basename
                    and isinstance(manifest_timeline, Mapping)
                    and isinstance(job_timeline, Mapping)
                    and all(
                        manifest_timeline.get(key) == job_timeline.get(key)
                        for key in (
                            "revision",
                            "revision_sha256",
                            "timeline_content_sha256",
                            "source_bindings_sha256",
                        )
                    )
                ):
                    raise CanonicalExportIntegrityError(
                        "canonical_export_package_manifest_mismatch"
                    )
                self.repository.reconcile_canonical_export_commit_pending(
                    job_id=job_id,
                    artifact_proof=package_proof["core_artifact_proof"],
                    occurrences=package_proof["usage_occurrences"],
                    runtime_manifest=package_proof["runtime_manifest"],
                )
                self._clear_unresolved(job_id)
            except Exception as exc:
                if (
                    output_root_fd is not None
                    and package_basename is not None
                    and expected_manifest is not None
                ):
                    if not self._handle_published_exception(
                        job_id=str(job["id"]),
                        cause=exc,
                        output_root_fd=output_root_fd,
                        package_basename=package_basename,
                        expected_manifest=expected_manifest,
                    ):
                        unresolved.append(str(job["id"]))
                else:
                    self._mark_recoverable(
                        str(job["id"]),
                        "canonical_export_recovery_pending",
                    )
                    unresolved.append(str(job["id"]))
                    self._record_unresolved(str(job["id"]))
            finally:
                if output_root_fd is not None:
                    try:
                        os.close(output_root_fd)
                    except OSError:
                        pass
        if unresolved:
            for job_id in unresolved:
                self._record_unresolved(job_id)
        remaining = self._refresh_unresolved_outcomes()
        if remaining:
            raise CanonicalExportIntegrityError(
                "canonical_export_recovery_incomplete:"
                + ",".join(remaining)
            )


__all__ = [
    "CanonicalExportJobRunner",
    "CanonicalExportService",
    "CanonicalExportServiceError",
    "canonical_export_workspace_summary_in_transaction",
]
