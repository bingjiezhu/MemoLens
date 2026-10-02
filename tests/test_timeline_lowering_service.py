from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import unittest
from unittest.mock import patch

from backend.src.media.timeline_lowering import (
    TimelineLoweringService,
    TimelineLoweringServiceError,
)
from core.coverage_contract import canonical_sha256
from core.media_db import CanonicalTimelineSourceUnavailableError
from core.timeline_lowering_contract import TimelineLoweringContractError


DATABASE_UUID = "12345678-1234-4123-8123-123456789abc"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


BLUEPRINT_BINDING = {
    "revision": 2,
    "content_sha256": SHA_A,
    "semantic_sha256": SHA_B,
    "operation_id": "blueprint_op_2",
}
COVERAGE_BINDING = {
    "revision": 3,
    "content_sha256": SHA_C,
    "evidence_manifest_sha256": SHA_D,
    "operation_id": "coverage_op_3",
}
SOURCE_INPUT = {
    "evidence_ref": "memolens://evidence/asset/asset_aaaaaaaaaaaaaaaaaaaaaaaa",
    "asset_id": "asset_aaaaaaaaaaaaaaaaaaaaaaaa",
    "asset_sha256": SHA_A,
    "asset_source_id": "src_aaaaaaaaaaaaaaaaaaaaaaaa",
    "analysis_run_id": f"arun_{'a' * 32}",
    "analysis_revision": 1,
    "analysis_content_sha256": SHA_B,
    "source_binding_sha256": SHA_C,
}
SOURCE_MANIFEST = {
    **SOURCE_INPUT,
    "clip_id": "clip_aaaaaaaaaaaaaaaaaaaaaaaa",
    "coverage_proof_sha256": SHA_E,
    "media_kind": "image",
}
COVERAGE_PROOF = {
    "kind": "asset",
    "asset_id": SOURCE_INPUT["asset_id"],
    "asset_sha256": SOURCE_INPUT["asset_sha256"],
    "analysis_run_id": SOURCE_INPUT["analysis_run_id"],
    "analysis_revision": SOURCE_INPUT["analysis_revision"],
    "analysis_content_sha256": SOURCE_INPUT["analysis_content_sha256"],
    "source_binding_sha256": SOURCE_INPUT["source_binding_sha256"],
}
COVERAGE_EVIDENCE = {
    "evidence_ref": SOURCE_INPUT["evidence_ref"],
    "status": "verified",
    "proof_sha256": canonical_sha256(COVERAGE_PROOF),
    "proof": COVERAGE_PROOF,
}
TIMELINE = {
    "timeline_id": "timeline_aaaaaaaaaaaaaaaaaaaaaaaa",
    "project_id": "project",
}


def command_payload(
    *,
    blueprint: dict[str, object] | None = None,
    coverage: dict[str, object] | None = None,
    timeline_head: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "expected_blueprint": blueprint or dict(BLUEPRINT_BINDING),
        "expected_coverage": coverage or dict(COVERAGE_BINDING),
        "expected_timeline_head": timeline_head,
    }


def blueprint_row() -> dict[str, object]:
    return {
        **BLUEPRINT_BINDING,
        "blueprint": {"project_id": "project", "semantic": {}},
    }


def coverage_row() -> dict[str, object]:
    return {
        **COVERAGE_BINDING,
        "blueprint_revision": BLUEPRINT_BINDING["revision"],
        "blueprint_content_sha256": BLUEPRINT_BINDING["content_sha256"],
        "blueprint_semantic_sha256": BLUEPRINT_BINDING["semantic_sha256"],
        "blueprint_operation_id": BLUEPRINT_BINDING["operation_id"],
        "plan": {
            "blueprint_binding": {
                field: BLUEPRINT_BINDING[field]
                for field in ("revision", "content_sha256", "semantic_sha256")
            },
            "evidence_manifest": [dict(COVERAGE_EVIDENCE)],
        },
    }


def canonical_revision(
    *,
    blueprint_binding: dict[str, object] | None = None,
    coverage_binding: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "project_id": "project",
        "revision": 1,
        "revision_sha256": SHA_F,
        "timeline_id": TIMELINE["timeline_id"],
        "timeline_content_sha256": SHA_E,
        "blueprint_binding": blueprint_binding or dict(BLUEPRINT_BINDING),
        "coverage_binding": coverage_binding or dict(COVERAGE_BINDING),
        "timeline": dict(TIMELINE),
        "source_bindings": [dict(SOURCE_MANIFEST)],
        "operation_id": "timeline_op_1",
    }


def timeline_head_binding(
    revision: dict[str, object],
) -> dict[str, object]:
    return {
        field: revision[field]
        for field in (
            "revision",
            "revision_sha256",
            "timeline_id",
            "timeline_content_sha256",
            "blueprint_binding",
            "coverage_binding",
            "operation_id",
        )
    }


def stale_canonical_revision() -> dict[str, object]:
    return canonical_revision(
        coverage_binding={
            "revision": 2,
            "content_sha256": SHA_B,
            "evidence_manifest_sha256": SHA_C,
            "operation_id": "coverage_op_2",
        }
    )


@dataclass(frozen=True)
class FakeCommandResult:
    response: dict[str, object]
    response_status: int
    replayed: bool
    operation_id: str
    resource_id: str


class FakeRepository:
    def __init__(self) -> None:
        self.database_uuid = DATABASE_UUID
        self.project: dict[str, object] | None = {"id": "project", "status": "active"}
        self.blueprint: dict[str, object] | None = blueprint_row()
        self.coverage: dict[str, object] | None = coverage_row()
        self.head: dict[str, object] | None = None
        self.revisions: dict[int, dict[str, object]] = {}
        self.resolved_sources = [dict(SOURCE_INPUT)]
        self.fixed_sources_current = True
        self.source_error: BaseException | None = None
        self.calls: list[tuple[str, object]] = []
        self.receipts: dict[tuple[object, ...], tuple[str, FakeCommandResult]] = {}
        self.connection = object()

    @contextmanager
    def transaction(self):
        self.calls.append(("transaction", None))
        yield self.connection

    def execute_canonical_timeline_command(self, **kwargs):
        self.calls.append(("execute", kwargs))
        identity = (
            kwargs["authenticated_principal"],
            kwargs["project_id"],
            kwargs["command_type"],
            kwargs["command_version"],
            kwargs["idempotency_key"],
        )
        replay = self.receipts.get(identity)
        if replay is not None:
            old_digest, old_result = replay
            if old_digest != kwargs["request_sha256"]:
                raise ValueError("timeline_idempotency_key_conflict")
            return replace(old_result, replayed=True)
        response, status, operation_id, resource_id = kwargs["mutation"](
            self.connection
        )
        result = FakeCommandResult(
            response=response,
            response_status=status,
            replayed=False,
            operation_id=operation_id,
            resource_id=resource_id,
        )
        self.receipts[identity] = (kwargs["request_sha256"], result)
        return result

    def _database_identity_in_transaction(self, connection):
        self.assert_connection(connection)
        return self.database_uuid

    def get_creative_project_in_transaction(self, connection, project_id):
        self.assert_connection(connection)
        return self.project if project_id == "project" else None

    def get_blueprint_head_in_transaction(self, connection, project_id):
        self.assert_connection(connection)
        return self.blueprint if project_id == "project" else None

    def get_coverage_head_in_transaction(self, connection, project_id):
        self.assert_connection(connection)
        return self.coverage if project_id == "project" else None

    def get_canonical_timeline_head_in_transaction(self, connection, project_id):
        self.assert_connection(connection)
        return self.head if project_id == "project" else None

    def get_trusted_canonical_timeline_head_in_transaction(
        self,
        connection,
        project_id,
    ):
        return self.get_canonical_timeline_head_in_transaction(
            connection,
            project_id,
        )

    def get_canonical_timeline_revision_in_transaction(
        self,
        connection,
        project_id,
        revision,
    ):
        self.assert_connection(connection)
        return self.revisions.get(revision) if project_id == "project" else None

    def get_trusted_canonical_timeline_revision_in_transaction(
        self,
        connection,
        project_id,
        revision,
    ):
        return self.get_canonical_timeline_revision_in_transaction(
            connection,
            project_id,
            revision,
        )

    def resolve_media_evidence_proofs_in_transaction(
        self,
        connection,
        evidence_refs,
        *,
        image_assets_only,
        residual_proofs=None,
        executable_evidence_refs=(),
    ):
        self.assert_connection(connection)
        if not image_assets_only:
            raise AssertionError("Coverage freshness must request image proofs only.")
        if residual_proofs not in (None, {}):
            raise AssertionError("This fixture has no residual proofs.")
        if tuple(executable_evidence_refs) != tuple(evidence_refs):
            raise AssertionError("Coverage evidence must be executable.")
        return [
            dict(COVERAGE_EVIDENCE)
            for evidence_ref in evidence_refs
            if evidence_ref == COVERAGE_EVIDENCE["evidence_ref"]
        ]

    def resolve_canonical_timeline_source_bindings_in_transaction(
        self,
        connection,
        coverage_plan,
    ):
        self.assert_connection(connection)
        self.calls.append(("resolve_sources", coverage_plan))
        if self.source_error is not None:
            raise self.source_error
        return self.resolved_sources

    def append_canonical_timeline_revision(self, connection, **kwargs):
        self.assert_connection(connection)
        self.calls.append(("append_revision", kwargs))
        revision = {
            "project_id": kwargs["project_id"],
            "revision": kwargs["revision"],
            "revision_sha256": SHA_F,
            "timeline_id": kwargs["timeline"]["timeline_id"],
            "timeline_content_sha256": SHA_E,
            "blueprint_binding": kwargs["blueprint_binding"],
            "coverage_binding": kwargs["coverage_binding"],
            "timeline": kwargs["timeline"],
            "source_bindings": kwargs["source_bindings"],
            "operation_id": kwargs["operation_id"],
        }
        self.revisions[int(kwargs["revision"])] = revision
        return revision

    def append_canonical_timeline_operation(self, connection, **kwargs):
        self.assert_connection(connection)
        self.calls.append(("append_operation", kwargs))

    def cas_canonical_timeline_head(self, connection, **kwargs):
        self.assert_connection(connection)
        self.calls.append(("cas_head", kwargs))
        self.head = self.revisions[int(kwargs["revision"])]
        return self.head

    def canonical_timeline_source_bindings_current_in_transaction(
        self,
        connection,
        source_bindings,
    ):
        self.assert_connection(connection)
        self.calls.append(("source_freshness", source_bindings))
        return self.fixed_sources_current

    def assert_connection(self, connection) -> None:
        if connection is not self.connection:
            raise AssertionError("service escaped the repository transaction")


class TimelineLoweringServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]

    def _materialize(self, *, key: str = "timeline-key", payload=None):
        with (
            patch(
                "backend.src.media.timeline_lowering.compile_canonical_timeline",
                return_value={
                    "timeline": dict(TIMELINE),
                    "source_bindings": [dict(SOURCE_MANIFEST)],
                },
            ) as compile_mock,
            patch(
                "backend.src.media.timeline_lowering.canonical_timeline_content_sha256",
                return_value=SHA_E,
            ),
        ):
            result = self.service.materialize_first_cut(
                "project",
                command_payload() if payload is None else payload,
                idempotency_key=key,
                expected_database_uuid=DATABASE_UUID,
            )
        return result, compile_mock

    def _reconcile(self, *, key: str = "timeline-reconcile-key", payload=None):
        if self.repository.head is None:
            self.repository.head = stale_canonical_revision()
            self.repository.revisions[1] = self.repository.head
        expected_head = timeline_head_binding(self.repository.head)
        with (
            patch(
                "backend.src.media.timeline_lowering.compile_canonical_timeline",
                return_value={
                    "timeline": dict(TIMELINE),
                    "source_bindings": [dict(SOURCE_MANIFEST)],
                },
            ) as compile_mock,
            patch(
                "backend.src.media.timeline_lowering.canonical_timeline_content_sha256",
                return_value=SHA_E,
            ),
        ):
            result = self.service.reconcile_from_coverage(
                "project",
                (
                    command_payload(timeline_head=expected_head)
                    if payload is None
                    else payload
                ),
                idempotency_key=key,
                expected_database_uuid=DATABASE_UUID,
            )
        return result, compile_mock

    def test_command_is_closed_and_requires_database_identity_and_null_head(self) -> None:
        invalid = command_payload()
        invalid["extra"] = True
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.service.materialize_first_cut(
                "project",
                invalid,
                idempotency_key="timeline-key",
                expected_database_uuid=DATABASE_UUID,
            )
        self.assertEqual(raised.exception.code, "invalid_timeline_command")
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.service.materialize_first_cut(
                "project",
                command_payload(),
                idempotency_key="timeline-key",
            )
        self.assertEqual(raised.exception.code, "invalid_database_identity")
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.service.materialize_first_cut(
                "project",
                command_payload(
                    timeline_head={"revision": 1, "revision_sha256": SHA_F}
                ),
                idempotency_key="timeline-key",
                expected_database_uuid=DATABASE_UUID,
            )
        self.assertEqual(raised.exception.code, "invalid_timeline_command")
        self.assertFalse(self.repository.calls)

    def test_materialize_validates_and_appends_one_closed_revision(self) -> None:
        result, compile_mock = self._materialize()
        self.assertFalse(result.replayed)
        self.assertEqual(result.response_status, 201)
        self.assertEqual(result.resource_id, "project:1")
        self.assertEqual(
            result.response["result"],
            {
                "kind": "revision_created",
                "result_head": {
                    "revision": 1,
                    "revision_sha256": SHA_F,
                    "timeline_id": TIMELINE["timeline_id"],
                    "timeline_content_sha256": SHA_E,
                    "blueprint_binding": BLUEPRINT_BINDING,
                    "coverage_binding": COVERAGE_BINDING,
                    "operation_id": result.operation_id,
                },
            },
        )
        compile_mock.assert_called_once_with(
            project_id="project",
            revision=1,
            parent=None,
            blueprint=self.repository.blueprint["blueprint"],  # type: ignore[index]
            blueprint_binding=BLUEPRINT_BINDING,
            coverage_plan=self.repository.coverage["plan"],  # type: ignore[index]
            coverage_binding=COVERAGE_BINDING,
            source_bindings=[SOURCE_INPUT],
        )
        names = [name for name, _ in self.repository.calls]
        self.assertEqual(
            names,
            [
                "execute",
                "resolve_sources",
                "append_revision",
                "append_operation",
                "cas_head",
            ],
        )
        execute = self.repository.calls[0][1]
        self.assertEqual(execute["authenticated_principal"], "desktop_app")
        self.assertEqual(execute["command_type"], "timeline.materialize_first_cut")
        operation = self.repository.calls[3][1]
        self.assertIsNone(operation["expected_timeline_head"])
        self.assertEqual(operation["result_revision_sha256"], SHA_F)

    def test_exact_blueprint_and_coverage_preconditions_fail_before_resolution(self) -> None:
        self.repository.blueprint = {**blueprint_row(), "operation_id": "changed_bp_op"}
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "blueprint_head_conflict")
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("resolve_sources", [name for name, _ in self.repository.calls])

        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        self.repository.coverage = {**coverage_row(), "operation_id": "changed_cov_op"}
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "coverage_head_conflict")
        self.assertNotIn("resolve_sources", [name for name, _ in self.repository.calls])

    def test_coverage_must_still_bind_the_exact_blueprint(self) -> None:
        assert self.repository.coverage is not None
        self.repository.coverage["plan"] = {
            "blueprint_binding": {
                **self.repository.coverage["plan"]["blueprint_binding"],  # type: ignore[index]
                "content_sha256": SHA_F,
            }
        }
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "coverage_blueprint_conflict")
        self.assertNotIn("resolve_sources", [name for name, _ in self.repository.calls])

    def test_existing_canonical_head_always_wins(self) -> None:
        self.repository.head = canonical_revision()
        self.repository.revisions[1] = self.repository.head
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "timeline_manual_current_conflict")
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("resolve_sources", [name for name, _ in self.repository.calls])

    def test_source_unavailable_and_contract_gap_are_zero_write_failures(self) -> None:
        self.repository.source_error = CanonicalTimelineSourceUnavailableError()
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "timeline_source_unavailable")
        self.assertEqual(raised.exception.status, 422)
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        self.repository.source_error = CanonicalTimelineSourceUnavailableError(
            "coverage_not_lowerable"
        )
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "coverage_not_lowerable")
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        error = TimelineLoweringContractError(
            [
                {
                    "code": "coverage_gap",
                    "path": "/coverage_plan/beats/0",
                    "message": "Gap.",
                }
            ]
        )
        with patch(
            "backend.src.media.timeline_lowering.compile_canonical_timeline",
            side_effect=error,
        ):
            with self.assertRaises(TimelineLoweringServiceError) as raised:
                self.service.materialize_first_cut(
                    "project",
                    command_payload(),
                    idempotency_key="timeline-key",
                    expected_database_uuid=DATABASE_UUID,
                )
        self.assertEqual(raised.exception.code, "coverage_not_lowerable")
        self.assertEqual(raised.exception.details, {"errors": error.errors})
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

    def test_receipt_replays_permanently_and_rejects_key_rebinding(self) -> None:
        first, first_compile = self._materialize()
        replay, replay_compile = self._materialize()
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(first_compile.call_count, 1)
        self.assertEqual(replay_compile.call_count, 0)
        self.assertEqual(
            [name for name, _ in self.repository.calls].count("append_revision"),
            1,
        )

        changed = command_payload(
            coverage={**COVERAGE_BINDING, "content_sha256": SHA_F}
        )
        with self.assertRaisesRegex(ValueError, "timeline_idempotency_key_conflict"):
            self._materialize(payload=changed)

    def test_database_switch_and_archive_fail_inside_command_transaction(self) -> None:
        self.repository.database_uuid = "22345678-1234-4123-8123-123456789abc"
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "database_identity_changed")
        self.assertEqual(raised.exception.status, 409)

        self.repository = FakeRepository()
        self.repository.project = {"id": "project", "status": "archived"}
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize()
        self.assertEqual(raised.exception.code, "project_archived")

    def test_reconcile_requires_one_closed_full_stale_head(self) -> None:
        head = timeline_head_binding(stale_canonical_revision())
        for invalid_head in (
            {key: value for key, value in head.items() if key != "operation_id"},
            {**head, "extra": True},
            None,
        ):
            with self.subTest(invalid_head=invalid_head):
                self.repository.calls.clear()
                with self.assertRaises(TimelineLoweringServiceError) as raised:
                    self.service.reconcile_from_coverage(
                        "project",
                        command_payload(timeline_head=invalid_head),
                        idempotency_key="timeline-reconcile-key",
                        expected_database_uuid=DATABASE_UUID,
                    )
                self.assertEqual(raised.exception.code, "invalid_timeline_command")
                self.assertFalse(self.repository.calls)

    def test_reconcile_appends_exact_n_plus_one_revision_and_operation(self) -> None:
        result, compile_mock = self._reconcile()
        expected_head = timeline_head_binding(stale_canonical_revision())
        self.assertFalse(result.replayed)
        self.assertEqual(result.response_status, 201)
        self.assertEqual(result.resource_id, "project:2")
        self.assertEqual(result.response["command_type"], self.service.reconcile_command_type)
        self.assertEqual(result.response["result"]["result_head"]["revision"], 2)
        compile_mock.assert_called_once_with(
            project_id="project",
            revision=2,
            parent={"revision": 1, "content_sha256": SHA_E},
            blueprint=self.repository.blueprint["blueprint"],  # type: ignore[index]
            blueprint_binding=BLUEPRINT_BINDING,
            coverage_plan=self.repository.coverage["plan"],  # type: ignore[index]
            coverage_binding=COVERAGE_BINDING,
            source_bindings=[SOURCE_INPUT],
        )
        execute = next(value for name, value in self.repository.calls if name == "execute")
        self.assertEqual(execute["command_type"], "timeline.reconcile_from_coverage")
        operation = next(
            value for name, value in self.repository.calls if name == "append_operation"
        )
        self.assertEqual(operation["command_type"], "timeline.reconcile_from_coverage")
        self.assertEqual(operation["command_version"], "1")
        self.assertEqual(operation["expected_timeline_head"], expected_head)
        appended = next(
            value for name, value in self.repository.calls if name == "append_revision"
        )
        self.assertEqual(appended["revision"], 2)
        self.assertEqual(appended["parent_revision"], 1)
        self.assertEqual(appended["parent_revision_sha256"], SHA_F)
        cas = next(value for name, value in self.repository.calls if name == "cas_head")
        self.assertEqual(cas["expected_head"], expected_head)
        self.assertEqual(cas["revision"], 2)

    def test_reconcile_rejects_head_change_current_noop_and_stale_coverage(self) -> None:
        stale = stale_canonical_revision()
        self.repository.head = stale
        self.repository.revisions[1] = stale
        mismatched = timeline_head_binding(stale)
        mismatched["operation_id"] = "timeline_other_op"
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._reconcile(payload=command_payload(timeline_head=mismatched))
        self.assertEqual(raised.exception.code, "timeline_head_conflict")
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        self.repository.head = canonical_revision()
        self.repository.revisions[1] = self.repository.head
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._reconcile()
        self.assertEqual(raised.exception.code, "timeline_already_current")
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

        self.repository = FakeRepository()
        self.service = TimelineLoweringService(self.repository)  # type: ignore[arg-type]
        with patch(
            "backend.src.media.timeline_lowering.coverage_freshness_in_transaction",
            return_value={"state": "stale_evidence", "gap_count": 0},
        ):
            with self.assertRaises(TimelineLoweringServiceError) as raised:
                self._reconcile()
        self.assertEqual(raised.exception.code, "coverage_plan_stale_evidence")
        self.assertNotIn("resolve_sources", [name for name, _ in self.repository.calls])

    def test_reconcile_rejects_gapped_coverage_before_any_write(self) -> None:
        self.repository.source_error = CanonicalTimelineSourceUnavailableError(
            "coverage_not_lowerable"
        )
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._reconcile()
        self.assertEqual(raised.exception.code, "coverage_not_lowerable")
        self.assertEqual(raised.exception.status, 422)
        self.assertNotIn("append_revision", [name for name, _ in self.repository.calls])

    def test_read_returns_exact_draft_without_approval_or_export_authority(self) -> None:
        selected = canonical_revision()
        self.repository.head = selected
        self.repository.revisions[1] = selected
        response = self.service.read("project")
        self.assertEqual(response["freshness"], {"state": "current", "reasons": []})
        self.assertEqual(response["head"]["revision_sha256"], SHA_F)
        self.assertTrue(response["selected_revision"]["is_head"])
        self.assertEqual(response["timeline"], TIMELINE)
        self.assertEqual(response["source_bindings"], [SOURCE_MANIFEST])
        self.assertEqual(
            response["lifecycle"],
            {
                "state": "draft",
                "approval": "not_established",
                "exportable": False,
            },
        )

    def test_read_freshness_has_stable_precedence_and_no_source_failover(self) -> None:
        selected = canonical_revision()
        self.repository.head = selected
        self.repository.revisions[1] = selected

        self.repository.blueprint = {**blueprint_row(), "operation_id": "new_bp_op"}
        response = self.service.read("project")
        self.assertEqual(response["freshness"]["state"], "stale_blueprint")
        self.assertNotIn("source_freshness", [name for name, _ in self.repository.calls])

        self.repository.calls.clear()
        self.repository.blueprint = blueprint_row()
        self.repository.coverage = {**coverage_row(), "content_sha256": SHA_F}
        response = self.service.read("project")
        self.assertEqual(response["freshness"]["state"], "stale_coverage")
        self.assertNotIn("source_freshness", [name for name, _ in self.repository.calls])

        self.repository.calls.clear()
        self.repository.coverage = coverage_row()
        self.repository.fixed_sources_current = False
        response = self.service.read("project")
        self.assertEqual(response["freshness"]["state"], "stale_source_binding")
        self.assertEqual(
            [name for name, _ in self.repository.calls].count("source_freshness"),
            1,
        )

    def test_missing_and_historical_read_are_explicit(self) -> None:
        response = self.service.read("project")
        self.assertEqual(response["freshness"], {"state": "missing", "reasons": []})
        self.assertIsNone(response["head"])
        self.assertIsNone(response["selected_revision"])
        self.assertIsNone(response["timeline"])
        self.assertEqual(response["source_bindings"], [])
        self.assertEqual(response["lifecycle"]["state"], "not_materialized")

        selected = canonical_revision()
        self.repository.head = selected
        self.repository.revisions[1] = selected
        response = self.service.read("project", revision=1)
        self.assertTrue(response["selected_revision"]["is_head"])
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.service.read("project", revision=2)
        self.assertEqual(raised.exception.code, "timeline_not_found")
        self.assertEqual(raised.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
