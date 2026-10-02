from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from core.image_analysis_job_contract import (
    IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_OBJECT,
    IMAGE_ANALYSIS_ATTEMPT_REASONS,
    IMAGE_ANALYSIS_ERROR_CODES,
    IMAGE_ANALYSIS_JOB_OBJECT,
    IMAGE_ANALYSIS_JOB_SCHEMA_VERSION,
    IMAGE_ANALYSIS_JOB_STATUSES,
    IMAGE_ANALYSIS_REQUEST_OBJECT,
    IMAGE_ANALYSIS_STAGES,
    IMAGE_ANALYSIS_TERMINAL_STATUSES,
    IMAGE_ANALYSIS_WORKER_KIND,
    IMAGE_ANALYSIS_WORK_STAGES,
    ImageAnalysisJobContractError,
    canonical_image_analysis_attempt_authority_json,
    canonical_json,
    canonical_sha256,
    image_analysis_attempt_authority_sha256,
    image_analysis_request_sha256,
    next_image_analysis_stage,
    require_valid_image_analysis_attempt_authority,
    require_valid_image_analysis_checkpoint,
    require_valid_image_analysis_error,
    require_valid_image_analysis_idempotency_binding,
    require_valid_image_analysis_job,
    require_valid_image_analysis_request,
    require_valid_image_analysis_worker_kind,
    seal_image_analysis_attempt_authority,
    validate_image_analysis_attempt_authority,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


ASSET_SHA256 = _sha("image-bytes")
ASSET_ID = f"asset_{ASSET_SHA256[:24]}"
SOURCE_ID = f"src_{_sha('source')[:24]}"
ROOT_ID = f"root_{_sha('root')[:24]}"
JOB_ID = f"job_{_sha('job')[:32]}"
ANALYSIS_RUN_ID = f"arun_{_sha('analysis')[:32]}"


def _request() -> dict[str, object]:
    return {
        "object": IMAGE_ANALYSIS_REQUEST_OBJECT,
        "schema_version": IMAGE_ANALYSIS_JOB_SCHEMA_VERSION,
        "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
        "database_binding": {
            "database_uuid": "01234567-89ab-4def-8abc-0123456789ab",
            "database_device": 1,
            "database_inode": 2,
        },
        "asset_binding": {
            "asset_id": ASSET_ID,
            "input_asset_sha256": ASSET_SHA256,
        },
        "source_binding": {
            "source_id": SOURCE_ID,
            "library_root_id": ROOT_ID,
            "relative_path": "photos/2026/展览 图片.jpg",
            "observed_size": 4_096,
            "observed_mtime_ns": 1_787_000_000_000_000_000,
            "observed_ctime_ns": 1_787_000_000_000_000_001,
            "source_device": 3,
            "source_inode": 4,
            "file_identity_sha256": _sha("device-inode-size-times"),
        },
        "analysis_profile": {
            "profile_id": "image-default",
            "profile_version": "1.0.0",
            "profile_sha256": _sha("profile"),
        },
        "expected_head": None,
        "intended_publication": {
            "analysis_run_id": ANALYSIS_RUN_ID,
            "revision": 1,
        },
        "idempotency": {
            "scope": "desktop:POST:/v1/media/image-analysis",
            "key": "image-analysis-one",
        },
    }


def _stage_outcome(stage: str, outcome: str = "succeeded") -> dict[str, object]:
    return {
        "stage": stage,
        "outcome": outcome,
        "outcome_sha256": _sha(f"{stage}:{outcome}"),
        "reason_code": None,
    }


def _checkpoint(
    completed_count: int = 0,
    *,
    current_stage: str | None = None,
) -> dict[str, object]:
    completed = list(IMAGE_ANALYSIS_WORK_STAGES[:completed_count])
    if current_stage is None:
        if completed_count == len(IMAGE_ANALYSIS_WORK_STAGES):
            current_stage = "completed"
        elif completed_count == 0:
            current_stage = "queued"
        else:
            current_stage = IMAGE_ANALYSIS_WORK_STAGES[completed_count]
    publish_binding = None
    if "publish" in completed:
        publish_binding = {
            "analysis_run_id": ANALYSIS_RUN_ID,
            "revision": 1,
            "content_sha256": _sha("result"),
            "change_position": 1,
            "publish_receipt_sha256": _sha("publish-receipt"),
            "projection_receipt_sha256": (_sha("projection-receipt") if "shadow_projection" in completed else None),
        }
    return {
        "object": "memolens.image_analysis_checkpoint",
        "schema_version": "1",
        "worker_kind": "image_analysis",
        "job_id": JOB_ID,
        "attempt": 1,
        "current_stage": current_stage,
        "completed_stages": completed,
        "stage_outcomes": [_stage_outcome(stage) for stage in completed],
        "artifact_digests": [],
        "publish_binding": publish_binding,
    }


def _idempotency(request: dict[str, object], job_id: str = JOB_ID) -> dict[str, object]:
    return {
        "object": "memolens.image_analysis_idempotency_binding",
        "schema_version": "1",
        "worker_kind": "image_analysis",
        "scope": request["idempotency"]["scope"],  # type: ignore[index]
        "key": request["idempotency"]["key"],  # type: ignore[index]
        "request_sha256": image_analysis_request_sha256(request),
        "job_id": job_id,
    }


def _attempt_authority(
    *,
    attempt: int = 1,
    generation_label: str = "runtime-a",
    reason: str | None = None,
    predecessor: dict[str, object] | None = None,
) -> dict[str, object]:
    request = _request()
    start_checkpoint = _checkpoint()
    start_checkpoint["attempt"] = attempt
    if attempt > 1 and predecessor is None:
        if attempt != 2:
            raise AssertionError("Fixture auto-predecessor only supports attempt two.")
        prior_authority = seal_image_analysis_attempt_authority(_attempt_authority())
        prior_checkpoint = _checkpoint(1)
        predecessor = {
            "attempt": 1,
            "status": "interrupted",
            "stage": "metadata",
            "checkpoint_sha256": canonical_sha256(prior_checkpoint),
            "authority_sha256": prior_authority["authority_sha256"],
        }
    return {
        "object": IMAGE_ANALYSIS_ATTEMPT_AUTHORITY_OBJECT,
        "schema_version": IMAGE_ANALYSIS_JOB_SCHEMA_VERSION,
        "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
        "job_id": JOB_ID,
        "attempt": attempt,
        "runtime_generation": f"runtime_generation_{_sha(generation_label)}",
        "database_binding": deepcopy(request["database_binding"]),
        "request_sha256": image_analysis_request_sha256(request),
        "reason": reason or ("initial" if attempt == 1 else "explicit_resume"),
        "predecessor": predecessor,
        "start_checkpoint_sha256": canonical_sha256(start_checkpoint),
    }


def _error(
    code: str = "source_changed",
    *,
    stage: str = "source_admission",
    retryable: bool = False,
) -> dict[str, object]:
    return {
        "object": "memolens.image_analysis_error",
        "schema_version": "1",
        "worker_kind": "image_analysis",
        "code": code,
        "stage": stage,
        "retryable": retryable,
        "detail_sha256": _sha("diagnostic-classification"),
    }


def _job(
    *,
    status: str = "queued",
    checkpoint: dict[str, object] | None = None,
    error: dict[str, object] | None = None,
    cancel_requested: bool = False,
) -> dict[str, object]:
    request = _request()
    checkpoint = checkpoint or _checkpoint()
    return {
        "object": IMAGE_ANALYSIS_JOB_OBJECT,
        "schema_version": IMAGE_ANALYSIS_JOB_SCHEMA_VERSION,
        "worker_kind": IMAGE_ANALYSIS_WORKER_KIND,
        "job_id": JOB_ID,
        "request": request,
        "request_sha256": image_analysis_request_sha256(request),
        "idempotency_binding": _idempotency(request),
        "status": status,
        "attempt": checkpoint["attempt"],
        "current_stage": checkpoint["current_stage"],
        "checkpoint": checkpoint,
        "heartbeat_sequence": 0,
        "cancel_requested": cancel_requested,
        "error": error,
    }


class ImageAnalysisJobContractTests(unittest.TestCase):
    def assert_contract_error(self, value, *, code: str, path: str, validator) -> None:
        with self.assertRaises(ImageAnalysisJobContractError) as raised:
            validator(value)
        self.assertTrue(
            any(item["code"] == code and item["path"] == path for item in raised.exception.errors),
            raised.exception.errors,
        )

    def test_worker_stage_status_and_error_registries_are_closed(self) -> None:
        self.assertEqual(IMAGE_ANALYSIS_WORKER_KIND, "image_analysis")
        self.assertEqual(
            IMAGE_ANALYSIS_STAGES,
            (
                "queued",
                "source_admission",
                "metadata",
                "geocode",
                "vision",
                "embedding",
                "quality",
                "publish",
                "shadow_projection",
                "completed",
            ),
        )
        self.assertEqual(
            IMAGE_ANALYSIS_JOB_STATUSES,
            {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "interrupted"},
        )
        self.assertEqual(IMAGE_ANALYSIS_TERMINAL_STATUSES, {"succeeded", "failed", "cancelled"})
        self.assertEqual(
            IMAGE_ANALYSIS_ERROR_CODES,
            {
                "cancelled",
                "checkpoint_corrupt",
                "database_scope_changed",
                "head_conflict",
                "idempotency_conflict",
                "internal_error",
                "invalid_request",
                "profile_changed",
                "projection_failed",
                "provider_denied",
                "provider_unavailable",
                "publication_conflict",
                "root_scope_changed",
                "source_changed",
                "source_permission_denied",
                "source_unavailable",
                "stage_failed",
                "worker_interrupted",
            },
        )
        self.assertEqual(require_valid_image_analysis_worker_kind("image_analysis"), "image_analysis")
        for substituted in ("video_index", "image", "IMAGE_ANALYSIS", 1, None):
            with self.subTest(substituted=substituted):
                self.assert_contract_error(
                    substituted,
                    code="unsupported_worker_kind",
                    path="/worker_kind",
                    validator=require_valid_image_analysis_worker_kind,
                )

    def test_request_is_closed_deterministic_profile_versioned_and_content_addressed(self) -> None:
        request = _request()
        validated = require_valid_image_analysis_request(request)
        self.assertEqual(validated, request)
        self.assertIsNot(validated, request)
        self.assertNotIn("runtime_generation", request["database_binding"])
        self.assertEqual(
            image_analysis_request_sha256(request),
            hashlib.sha256(canonical_json(request).encode("utf-8")).hexdigest(),
        )
        reordered = json.loads(json.dumps(request, ensure_ascii=False))
        reordered = dict(reversed(list(reordered.items())))
        self.assertEqual(image_analysis_request_sha256(request), image_analysis_request_sha256(reordered))

        missing_profile_version = deepcopy(request)
        del missing_profile_version["analysis_profile"]["profile_version"]  # type: ignore[index]
        self.assert_contract_error(
            missing_profile_version,
            code="required_field_missing",
            path="/analysis_profile/profile_version",
            validator=require_valid_image_analysis_request,
        )
        rebound_asset = deepcopy(request)
        rebound_asset["asset_binding"]["asset_id"] = f"asset_{_sha('other')[:24]}"  # type: ignore[index]
        self.assert_contract_error(
            rebound_asset,
            code="asset_digest_mismatch",
            path="/asset_binding/asset_id",
            validator=require_valid_image_analysis_request,
        )
        nonlinear = deepcopy(request)
        nonlinear["expected_head"] = {
            "analysis_run_id": f"arun_{_sha('prior')[:32]}",
            "revision": 8,
            "content_sha256": _sha("prior-result"),
        }
        nonlinear["intended_publication"]["revision"] = 3  # type: ignore[index]
        self.assert_contract_error(
            nonlinear,
            code="nonlinear_intended_revision",
            path="/intended_publication/revision",
            validator=require_valid_image_analysis_request,
        )

    def test_request_rejects_unknown_authority_absolute_paths_credentials_payloads_and_bad_types(self) -> None:
        mutations = []
        unknown_root = deepcopy(_request())
        unknown_root["database_path"] = "/tmp/memolens.sqlite"
        mutations.append((unknown_root, "unknown_field", "/*"))
        unknown_nested = deepcopy(_request())
        unknown_nested["analysis_profile"]["provider_payload"] = {"prompt": "private"}  # type: ignore[index]
        mutations.append((unknown_nested, "unknown_field", "/analysis_profile/*"))
        credential = deepcopy(_request())
        credential["source_binding"]["api_key"] = "secret"  # type: ignore[index]
        mutations.append((credential, "unknown_field", "/source_binding/*"))
        absolute = deepcopy(_request())
        absolute["source_binding"]["relative_path"] = "/Users/person/private.jpg"  # type: ignore[index]
        mutations.append((absolute, "absolute_path_forbidden", "/source_binding/relative_path"))
        windows = deepcopy(_request())
        windows["source_binding"]["relative_path"] = "C:\\private.jpg"  # type: ignore[index]
        mutations.append((windows, "absolute_path_forbidden", "/source_binding/relative_path"))
        traversal = deepcopy(_request())
        traversal["source_binding"]["relative_path"] = "photos/../private.jpg"  # type: ignore[index]
        mutations.append((traversal, "invalid_relative_path", "/source_binding/relative_path"))
        boolean_inode = deepcopy(_request())
        boolean_inode["database_binding"]["database_inode"] = True  # type: ignore[index]
        mutations.append((boolean_inode, "invalid_integer", "/database_binding/database_inode"))
        runtime_execution_fact = deepcopy(_request())
        runtime_execution_fact["database_binding"]["runtime_generation"] = (  # type: ignore[index]
            f"runtime_generation_{_sha('runtime')}"
        )
        mutations.append(
            (
                runtime_execution_fact,
                "unknown_field",
                "/database_binding/*",
            )
        )
        non_finite = deepcopy(_request())
        non_finite["source_binding"]["observed_mtime_ns"] = float("nan")  # type: ignore[index]
        mutations.append((non_finite, "invalid_number", "/source_binding/observed_mtime_ns"))
        for value, code, path in mutations:
            with self.subTest(code=code, path=path):
                self.assert_contract_error(
                    value,
                    code=code,
                    path=path,
                    validator=require_valid_image_analysis_request,
                )

    def test_request_digest_is_generation_stable_but_attempt_authority_is_not(self) -> None:
        request = _request()
        request_digest = image_analysis_request_sha256(request)
        first = seal_image_analysis_attempt_authority(_attempt_authority(generation_label="runtime-a"))
        second = seal_image_analysis_attempt_authority(_attempt_authority(generation_label="runtime-b"))

        self.assertEqual(first["request_sha256"], request_digest)
        self.assertEqual(second["request_sha256"], request_digest)
        self.assertNotEqual(first["runtime_generation"], second["runtime_generation"])
        self.assertNotEqual(first["authority_sha256"], second["authority_sha256"])

    def test_attempt_authority_is_closed_self_digesting_and_consecutive(self) -> None:
        self.assertEqual(
            IMAGE_ANALYSIS_ATTEMPT_REASONS,
            {
                "initial",
                "explicit_resume",
                "process_recovery",
                "runtime_handoff",
            },
        )
        initial = seal_image_analysis_attempt_authority(_attempt_authority())
        successor = seal_image_analysis_attempt_authority(_attempt_authority(attempt=2))

        self.assertEqual(validate_image_analysis_attempt_authority(initial), [])
        detached = require_valid_image_analysis_attempt_authority(initial)
        self.assertEqual(detached, initial)
        self.assertIsNot(detached, initial)
        self.assertEqual(
            initial["authority_sha256"],
            image_analysis_attempt_authority_sha256(initial),
        )
        self.assertEqual(successor["predecessor"]["attempt"], 1)  # type: ignore[index]
        self.assertEqual(
            successor["predecessor"]["authority_sha256"],  # type: ignore[index]
            initial["authority_sha256"],
        )
        self.assertEqual(
            canonical_image_analysis_attempt_authority_json(initial),
            canonical_json(initial),
        )

    def test_attempt_authority_rejects_bad_generation_predecessor_and_digest(self) -> None:
        invalid_generation = _attempt_authority()
        invalid_generation["runtime_generation"] = "runtime_generation_deadbeef"
        self.assert_contract_error(
            invalid_generation,
            code="invalid_runtime_generation",
            path="/runtime_generation",
            validator=seal_image_analysis_attempt_authority,
        )

        missing_predecessor = _attempt_authority(attempt=2)
        missing_predecessor["predecessor"] = None
        self.assert_contract_error(
            missing_predecessor,
            code="attempt_predecessor_required",
            path="/predecessor",
            validator=seal_image_analysis_attempt_authority,
        )

        skipped_predecessor = _attempt_authority(attempt=2)
        skipped_predecessor["predecessor"]["attempt"] = 9  # type: ignore[index]
        self.assert_contract_error(
            skipped_predecessor,
            code="attempt_predecessor_mismatch",
            path="/predecessor/attempt",
            validator=seal_image_analysis_attempt_authority,
        )

        first_with_predecessor = _attempt_authority()
        first_with_predecessor["predecessor"] = _attempt_authority(attempt=2)["predecessor"]
        self.assert_contract_error(
            first_with_predecessor,
            code="attempt_predecessor_forbidden",
            path="/predecessor",
            validator=seal_image_analysis_attempt_authority,
        )

        wrong_initial_reason = _attempt_authority(attempt=2, reason="initial")
        self.assert_contract_error(
            wrong_initial_reason,
            code="attempt_reason_mismatch",
            path="/reason",
            validator=seal_image_analysis_attempt_authority,
        )

        tampered = seal_image_analysis_attempt_authority(_attempt_authority())
        tampered["start_checkpoint_sha256"] = _sha("different-checkpoint")
        self.assert_contract_error(
            tampered,
            code="digest_mismatch",
            path="/authority_sha256",
            validator=require_valid_image_analysis_attempt_authority,
        )

    def test_checkpoint_accepts_only_monotonic_completed_outcomes_digests_and_publish_binding(self) -> None:
        initial = require_valid_image_analysis_checkpoint(_checkpoint())
        self.assertEqual(initial["current_stage"], "queued")

        metadata = _checkpoint(2)
        metadata["artifact_digests"] = [
            {"stage": "metadata", "name": "normalized_metadata", "sha256": _sha("metadata")}
        ]
        self.assertEqual(require_valid_image_analysis_checkpoint(metadata), metadata)

        all_done = _checkpoint(len(IMAGE_ANALYSIS_WORK_STAGES))
        self.assertEqual(require_valid_image_analysis_checkpoint(all_done)["current_stage"], "completed")

        skipped = _checkpoint(2)
        skipped["completed_stages"] = ["source_admission", "geocode"]
        skipped["stage_outcomes"][1]["stage"] = "geocode"  # type: ignore[index]
        self.assert_contract_error(
            skipped,
            code="checkpoint_stage_order_invalid",
            path="/completed_stages",
            validator=require_valid_image_analysis_checkpoint,
        )
        early_artifact = _checkpoint(1)
        early_artifact["artifact_digests"] = [{"stage": "vision", "name": "provider_payload", "sha256": _sha("opaque")}]
        self.assert_contract_error(
            early_artifact,
            code="artifact_stage_not_completed",
            path="/artifact_digests/0/stage",
            validator=require_valid_image_analysis_checkpoint,
        )
        premature_publish = _checkpoint(1)
        premature_publish["publish_binding"] = _checkpoint(7)["publish_binding"]
        self.assert_contract_error(
            premature_publish,
            code="premature_publish_binding",
            path="/publish_binding",
            validator=require_valid_image_analysis_checkpoint,
        )
        missing_publish = _checkpoint(7)
        missing_publish["publish_binding"] = None
        self.assert_contract_error(
            missing_publish,
            code="publish_binding_required",
            path="/publish_binding",
            validator=require_valid_image_analysis_checkpoint,
        )

    def test_checkpoint_requires_reason_for_non_complete_stage_outcomes_and_sorted_unique_artifacts(self) -> None:
        disabled = _checkpoint(4)
        disabled["stage_outcomes"][3]["outcome"] = "disabled"  # type: ignore[index]
        disabled["stage_outcomes"][3]["outcome_sha256"] = _sha("vision:disabled")  # type: ignore[index]
        self.assert_contract_error(
            disabled,
            code="reason_code_required",
            path="/stage_outcomes/3/reason_code",
            validator=require_valid_image_analysis_checkpoint,
        )
        disabled["stage_outcomes"][3]["reason_code"] = "provider_not_granted"  # type: ignore[index]
        self.assertEqual(require_valid_image_analysis_checkpoint(disabled), disabled)

        unsorted = _checkpoint(2)
        unsorted["artifact_digests"] = [
            {"stage": "metadata", "name": "z", "sha256": _sha("z")},
            {"stage": "metadata", "name": "a", "sha256": _sha("a")},
        ]
        self.assert_contract_error(
            unsorted,
            code="artifact_order_invalid",
            path="/artifact_digests",
            validator=require_valid_image_analysis_checkpoint,
        )
        raw_payload = _checkpoint(1)
        raw_payload["provider_payload"] = {"response": "private"}
        self.assert_contract_error(
            raw_payload,
            code="unknown_field",
            path="/*",
            validator=require_valid_image_analysis_checkpoint,
        )

        too_many_artifacts = _checkpoint(1)
        too_many_artifacts["artifact_digests"] = [
            {
                "stage": "source_admission",
                "name": f"artifact-{index:03d}",
                "sha256": _sha(f"artifact-{index:03d}"),
            }
            for index in range(129)
        ]
        self.assert_contract_error(
            too_many_artifacts,
            code="artifact_limit_exceeded",
            path="/artifact_digests",
            validator=require_valid_image_analysis_checkpoint,
        )

        oversized_prefix = _checkpoint(1)
        oversized_prefix["completed_stages"] = list(IMAGE_ANALYSIS_WORK_STAGES) + ["metadata"]
        oversized_prefix["stage_outcomes"] = [_stage_outcome(stage) for stage in oversized_prefix["completed_stages"]]
        oversized_prefix["current_stage"] = "completed"
        self.assert_contract_error(
            oversized_prefix,
            code="checkpoint_stage_order_invalid",
            path="/completed_stages",
            validator=require_valid_image_analysis_checkpoint,
        )

    def test_error_contract_is_stable_payload_free_and_has_closed_retry_policy(self) -> None:
        self.assertEqual(require_valid_image_analysis_error(_error()), _error())
        unknown = _error("made_up")
        self.assert_contract_error(
            unknown,
            code="invalid_error_code",
            path="/code",
            validator=require_valid_image_analysis_error,
        )
        leaked = _error()
        leaked["message"] = "/Users/person/private.jpg: provider key abc"
        self.assert_contract_error(
            leaked,
            code="unknown_field",
            path="/*",
            validator=require_valid_image_analysis_error,
        )
        retry_conflict = _error("idempotency_conflict", retryable=True)
        self.assert_contract_error(
            retry_conflict,
            code="invalid_retry_policy",
            path="/retryable",
            validator=require_valid_image_analysis_error,
        )

    def test_idempotency_binding_is_exact_and_job_rejects_key_or_request_rebind(self) -> None:
        request = _request()
        binding = _idempotency(request)
        self.assertEqual(require_valid_image_analysis_idempotency_binding(binding), binding)

        job = _job()
        self.assertEqual(require_valid_image_analysis_job(job), job)
        retry_checkpoint = _checkpoint()
        retry_checkpoint["attempt"] = 2
        retry_job = _job(checkpoint=retry_checkpoint)
        self.assertEqual(require_valid_image_analysis_job(retry_job), retry_job)
        rebound_key = deepcopy(job)
        rebound_key["idempotency_binding"]["key"] = "different-key"
        self.assert_contract_error(
            rebound_key,
            code="idempotency_binding_mismatch",
            path="/idempotency_binding",
            validator=require_valid_image_analysis_job,
        )
        rebound_request = deepcopy(job)
        rebound_request["request"]["analysis_profile"]["profile_version"] = "2.0.0"
        self.assert_contract_error(
            rebound_request,
            code="request_digest_mismatch",
            path="/request_sha256",
            validator=require_valid_image_analysis_job,
        )

    def test_job_state_machine_cross_checks_checkpoint_error_cancel_and_terminal_state(self) -> None:
        running_checkpoint = _checkpoint(1)
        running = _job(status="running", checkpoint=running_checkpoint)
        self.assertEqual(require_valid_image_analysis_job(running), running)

        completed = _checkpoint(len(IMAGE_ANALYSIS_WORK_STAGES))
        succeeded = _job(status="succeeded", checkpoint=completed)
        self.assertEqual(require_valid_image_analysis_job(succeeded), succeeded)

        failed_error = _error("stage_failed", stage="metadata", retryable=True)
        failed_checkpoint = _checkpoint(1)
        failed = _job(status="failed", checkpoint=failed_checkpoint, error=failed_error)
        self.assertEqual(require_valid_image_analysis_job(failed), failed)

        cancelled_error = _error("cancelled", stage="metadata", retryable=False)
        cancelled = _job(
            status="cancelled",
            checkpoint=failed_checkpoint,
            error=cancelled_error,
            cancel_requested=True,
        )
        self.assertEqual(require_valid_image_analysis_job(cancelled), cancelled)

        no_error = _job(status="failed", checkpoint=failed_checkpoint)
        self.assert_contract_error(
            no_error,
            code="job_error_required",
            path="/error",
            validator=require_valid_image_analysis_job,
        )
        false_success = _job(status="succeeded", checkpoint=running_checkpoint)
        self.assert_contract_error(
            false_success,
            code="success_state_mismatch",
            path="/current_stage",
            validator=require_valid_image_analysis_job,
        )
        cancelled_without_request = _job(
            status="cancelled",
            checkpoint=failed_checkpoint,
            error=cancelled_error,
        )
        self.assert_contract_error(
            cancelled_without_request,
            code="cancel_state_mismatch",
            path="/cancel_requested",
            validator=require_valid_image_analysis_job,
        )

    def test_stage_successor_and_canonical_json_are_deterministic_and_non_finite_safe(self) -> None:
        self.assertEqual(
            [next_image_analysis_stage(stage) for stage in IMAGE_ANALYSIS_STAGES[:-1]],
            list(IMAGE_ANALYSIS_STAGES[1:]),
        )
        self.assert_contract_error(
            "completed",
            code="terminal_stage",
            path="/current_stage",
            validator=next_image_analysis_stage,
        )
        self.assertEqual(canonical_json({"b": 1, "a": "图片"}), '{"a":"图片","b":1}')
        self.assertEqual(canonical_sha256({"b": 1, "a": 2}), canonical_sha256({"a": 2, "b": 1}))
        with self.assertRaises(ValueError):
            canonical_json({"value": float("nan")})


if __name__ == "__main__":
    unittest.main()
