from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.src.media.blueprint import BlueprintService
from core.blueprint_contract import blueprint_decision_unit_digests
from core.db import ImageIndexRepository
from core.media_db import (
    V5_SCHEMA_STATEMENTS,
    BlueprintAuthorityError,
    BlueprintIntegrityError,
    MediaRepository,
    _AuthorityJsonBudget,
    _B1_JSON_MAX_BYTES,
    _BLUEPRINT_AUTHORITY_EVENT_JSON_AGGREGATE_MAX_BYTES,
    _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS,
    _BLUEPRINT_AUTHORITY_RECEIPT_JSON_AGGREGATE_MAX_BYTES,
    canonical_json,
    content_sha256,
)
from tests.test_b1_core_ledger import semantic


class B1AuthorityResourceTests(unittest.TestCase):
    public_projection_commit_counts: dict[int, tuple[int, int]] = {}
    public_projection_restore_counts: dict[int, tuple[int, int]] = {}

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-b1-authority-resources-"
        )
        self.root = Path(self.temporary.name).resolve()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_blueprint_project(
        self,
    ) -> tuple[MediaRepository, BlueprintService, str, dict[str, object]]:
        library = self.root / "library"
        library.mkdir()
        db_path = self.root / "state" / "media.db"
        db_path.parent.mkdir()
        ImageIndexRepository(db_path).ensure_schema()
        repository = MediaRepository(db_path)
        repository.ensure_schema(library)
        project = repository.create_project(
            "Authority resources",
            {"goal": "legacy"},
            {"source": "test"},
        )
        project_id = str(project["id"])
        brief = repository.get_brief(project_id, 1)
        assert brief is not None
        service = BlueprintService(repository)
        initial_semantic = semantic("authority-resource")
        service.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": initial_semantic,
            },
            idempotency_key="authority-resource-initial",
        )
        return repository, service, project_id, initial_semantic

    @staticmethod
    def _authority_table_snapshot(
        repository: MediaRepository,
        project_id: str,
    ) -> tuple[int, int, tuple[object, ...] | None]:
        with repository._connect() as connection:
            event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_decision_authority_events "
                    "WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            )
            receipt_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM blueprint_decision_authority_receipts "
                    "WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
            )
            head = connection.execute(
                "SELECT sequence,event_id,event_sha256,updated_at "
                "FROM blueprint_decision_authority_heads WHERE project_id=?",
                (project_id,),
            ).fetchone()
        return event_count, receipt_count, tuple(head) if head is not None else None

    @staticmethod
    def _confirm_or_revoke(
        repository: MediaRepository,
        project_id: str,
        *,
        operation: str,
        nonce: str,
        idempotency_key: str,
        decision_unit: str = "script",
    ) -> tuple[dict[str, object], object]:
        envelope = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation=operation,
            decision_units=[decision_unit],
            runtime_authority_epoch="authority_resource_epoch",
        )
        method = (
            repository.confirm_blueprint_decision_units
            if operation == "confirm"
            else repository.revoke_blueprint_decision_units
        )
        result = method(
            project_id,
            runtime_authority_epoch="authority_resource_epoch",
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce=nonce,
            idempotency_key=idempotency_key,
        )
        return envelope, result

    def test_preflight_accepts_exact_value_and_row_caps_then_rejects_plus_one(
        self,
    ) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.execute(
            """CREATE TABLE blueprint_decision_authority_events(
                 project_id TEXT, decision_units_json TEXT, unit_digests_json TEXT,
                 active_confirmation_ids_json TEXT, presentation_json TEXT)"""
        )
        connection.execute(
            """CREATE TABLE blueprint_decision_authority_receipts(
                 project_id TEXT, response_json TEXT)"""
        )
        exact_value = '"' + ("x" * (_B1_JSON_MAX_BYTES - 2)) + '"'
        connection.execute(
            "INSERT INTO blueprint_decision_authority_events VALUES(?,?,?,?,?)",
            ("project", "[]", "{}", "{}", exact_value),
        )
        connection.execute(
            "INSERT INTO blueprint_decision_authority_receipts VALUES(?,?)",
            ("project", "{}"),
        )
        remaining = _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS - 1
        connection.executemany(
            "INSERT INTO blueprint_decision_authority_events VALUES(?,?,?,?,?)",
            (("project", "[]", "{}", "{}", "{}") for _ in range(remaining)),
        )
        connection.executemany(
            "INSERT INTO blueprint_decision_authority_receipts VALUES(?,?)",
            (("project", "{}") for _ in range(remaining)),
        )

        budget = MediaRepository._preflight_authority_json(connection, "project")
        self.assertEqual(budget.event_count, _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS)
        self.assertEqual(budget.receipt_count, _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS)

        connection.execute(
            "UPDATE blueprint_decision_authority_events SET presentation_json=? "
            "WHERE rowid=1",
            (exact_value[:-1] + 'x"',),
        )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_event_json_too_large",
        ):
            MediaRepository._preflight_authority_json(connection, "project")

        connection.execute(
            "UPDATE blueprint_decision_authority_events SET presentation_json=? "
            "WHERE rowid=1",
            (exact_value,),
        )
        connection.execute(
            "UPDATE blueprint_decision_authority_receipts SET response_json=? "
            "WHERE rowid=1",
            (exact_value,),
        )
        budget = MediaRepository._preflight_authority_json(connection, "project")
        self.assertEqual(budget.receipt_count, _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS)
        connection.execute(
            "UPDATE blueprint_decision_authority_receipts SET response_json=? "
            "WHERE rowid=1",
            (exact_value[:-1] + 'x"',),
        )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_receipt_response_too_large",
        ):
            MediaRepository._preflight_authority_json(connection, "project")
        connection.execute(
            "UPDATE blueprint_decision_authority_receipts SET response_json='{}' "
            "WHERE rowid=1"
        )
        connection.execute(
            "INSERT INTO blueprint_decision_authority_events VALUES(?,?,?,?,?)",
            ("project", "[]", "{}", "{}", "{}"),
        )
        connection.execute(
            "INSERT INTO blueprint_decision_authority_receipts VALUES(?,?)",
            ("project", "{}"),
        )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_capacity_exceeded",
        ):
            MediaRepository._preflight_authority_json(connection, "project")

    def test_two_subthreshold_rows_cross_aggregate_before_any_json_decode(
        self,
    ) -> None:
        repository, _service, project_id, _initial = self._create_blueprint_project()
        self._confirm_or_revoke(
            repository,
            project_id,
            operation="confirm",
            nonce="aggregate_confirm",
            idempotency_key="aggregate-confirm",
        )
        self._confirm_or_revoke(
            repository,
            project_id,
            operation="revoke",
            nonce="aggregate_revoke",
            idempotency_key="aggregate-revoke",
        )
        with repository.transaction() as connection:
            budget = repository._preflight_authority_json(connection, project_id)
        self.assertEqual(budget.event_count, 2)
        self.assertLess(budget.event_json_bytes, 2 * 4 * _B1_JSON_MAX_BYTES)

        with (
            patch(
                "core.media_db._BLUEPRINT_AUTHORITY_EVENT_JSON_AGGREGATE_MAX_BYTES",
                budget.event_json_bytes - 1,
            ),
            patch.object(
                MediaRepository,
                "_decode_canonical_json",
                side_effect=AssertionError("authority JSON decoded before preflight"),
            ) as decode_spy,
            repository.transaction() as connection,
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_authority_capacity_exceeded",
            ):
                repository._validate_authority_ledger(connection, project_id)
        decode_spy.assert_not_called()

        with (
            patch(
                "core.media_db._BLUEPRINT_AUTHORITY_RECEIPT_JSON_AGGREGATE_MAX_BYTES",
                budget.receipt_json_bytes - 1,
            ),
            patch.object(
                MediaRepository,
                "_decode_canonical_json",
                side_effect=AssertionError("receipt JSON decoded before preflight"),
            ) as decode_spy,
            repository.transaction() as connection,
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_authority_capacity_exceeded",
            ):
                repository._validate_authority_ledger(connection, project_id)
        decode_spy.assert_not_called()

    def test_revision_merge_stream_uses_one_query_at_small_and_large_scale(self) -> None:
        def statement_count(revisions: int) -> int:
            connection = sqlite3.connect(":memory:")
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            connection.execute(
                """CREATE TABLE creative_blueprint_revisions(
                     project_id TEXT, revision INTEGER, operation_id TEXT)"""
            )
            connection.execute(
                """CREATE TABLE creative_blueprint_operations(
                     project_id TEXT, id TEXT, typed_diff_json TEXT)"""
            )
            connection.executemany(
                "INSERT INTO creative_blueprint_revisions VALUES('project',?,?)",
                ((revision, f"operation_{revision}") for revision in range(1, revisions + 1)),
            )
            connection.executemany(
                "INSERT INTO creative_blueprint_operations VALUES('project',?,'[]')",
                ((f"operation_{revision}",) for revision in range(1, revisions + 1)),
            )
            statements: list[str] = []
            connection.set_trace_callback(statements.append)
            rows = list(
                MediaRepository._authority_revision_rows(
                    connection,
                    "project",
                    revisions,
                )
            )
            connection.set_trace_callback(None)
            self.assertEqual(len(rows), revisions)
            return sum(
                statement.lstrip().upper().startswith("SELECT")
                for statement in statements
            )

        self.assertEqual(statement_count(8), 1)
        self.assertEqual(statement_count(10_000), 1)
        self.assertFalse(
            hasattr(MediaRepository, "_decision_digests_through_revision")
        )

    def test_public_projection_has_constant_fresh_connection_statement_slope(
        self,
    ) -> None:
        def build_snapshots(
            name: str,
            *,
            restore_heavy: bool,
        ) -> tuple[str, dict[int, Path]]:
            root = self.root / name
            library = root / "library"
            library.mkdir(parents=True)
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            repository.ensure_schema(library)
            project = repository.create_project(
                f"Authority {name}",
                {"goal": "legacy"},
                {"source": "test"},
            )
            project_id = str(project["id"])
            brief = repository.get_brief(project_id, 1)
            assert brief is not None
            service = BlueprintService(repository)
            created = service.commit_proposal(
                project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": semantic(f"{name}-1"),
                },
                idempotency_key=f"{name}-1",
            )
            result = created.response["result"]
            assert isinstance(result, dict)
            head = result["result_head"]
            assert isinstance(head, dict)
            restore_target = {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            }
            snapshots: dict[int, Path] = {}

            def freeze(revision: int) -> None:
                destination = root / f"revision-{revision}.db"
                with sqlite3.connect(db_path) as source, sqlite3.connect(
                    destination
                ) as target:
                    source.backup(target)
                snapshots[revision] = destination

            for revision in range(2, 101):
                expected_head = {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                }
                if restore_heavy and revision % 2 == 1:
                    changed = service.restore_revision(
                        project_id,
                        {
                            "expected_head": expected_head,
                            "restore_from": restore_target,
                        },
                        idempotency_key=f"{name}-{revision}",
                    )
                else:
                    changed = service.commit_proposal(
                        project_id,
                        {
                            "expected_head": expected_head,
                            "initial_legacy_brief": None,
                            "source_candidate_sha256": None,
                            "semantic": semantic(f"{name}-{revision}"),
                        },
                        idempotency_key=f"{name}-{revision}",
                    )
                result = changed.response["result"]
                assert isinstance(result, dict)
                head = result["result_head"]
                assert isinstance(head, dict)
                if revision in {10, 50, 100}:
                    freeze(revision)
            return project_id, snapshots

        def trace(
            project_id: str,
            snapshots: dict[int, Path],
        ) -> dict[int, tuple[int, int]]:
            counts: dict[int, tuple[int, int]] = {}
            for revision, snapshot in snapshots.items():
                repository = MediaRepository(snapshot)
                statements: list[str] = []
                real_connect = repository._connect

                def traced_connect() -> sqlite3.Connection:
                    connection = real_connect()
                    connection.set_trace_callback(statements.append)
                    return connection

                with patch.object(repository, "_connect", new=traced_connect):
                    projection = repository.get_blueprint_authority_projection(
                        project_id
                    )
                self.assertEqual(projection["as_of_revision"], revision)
                selects = sum(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in statements
                )
                counts[revision] = (selects, len(statements))
            return counts

        commit_project, commit_snapshots = build_snapshots(
            "public-commit-slope",
            restore_heavy=False,
        )
        restore_project, restore_snapshots = build_snapshots(
            "public-restore-slope",
            restore_heavy=True,
        )
        commit_counts = trace(commit_project, commit_snapshots)
        restore_counts = trace(restore_project, restore_snapshots)
        type(self).public_projection_commit_counts = commit_counts
        type(self).public_projection_restore_counts = restore_counts
        self.assertEqual(commit_counts[10], commit_counts[50])
        self.assertEqual(commit_counts[50], commit_counts[100])
        self.assertEqual(restore_counts[10], restore_counts[50])
        self.assertEqual(restore_counts[50], restore_counts[100])

    def test_no_change_storage_class_fails_before_operation_decode(self) -> None:
        repository, service, project_id, initial = self._create_blueprint_project()
        head = repository.get_blueprint_head(project_id)
        assert head is not None
        no_change = service.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": initial,
            },
            idempotency_key="no-change-storage-class",
        )
        with repository._connect() as connection:
            connection.execute(
                "DROP TRIGGER trg_creative_blueprint_operations_no_update"
            )
            connection.execute(
                "UPDATE creative_blueprint_operations SET actor_json=CAST(X'31' AS BLOB) "
                "WHERE id=?",
                (no_change.operation_id,),
            )

        decoded_no_change = False
        real_decoder = MediaRepository._blueprint_operation_from_row

        def guarded_decoder(
            cls: type[MediaRepository],
            row: sqlite3.Row,
        ) -> dict[str, object]:
            nonlocal decoded_no_change
            if row["id"] == no_change.operation_id:
                decoded_no_change = True
                raise AssertionError("no-change JSON decoded before storage preflight")
            return real_decoder(row)

        with patch.object(
            MediaRepository,
            "_blueprint_operation_from_row",
            new=classmethod(guarded_decoder),
        ):
            with self.assertRaisesRegex(
                BlueprintIntegrityError,
                "blueprint_operation_json_too_large",
            ):
                repository.get_blueprint_authority_projection(project_id)
        self.assertFalse(decoded_no_change)

    def test_section_mapping_invalidates_only_affected_decision_units(self) -> None:
        before_semantic = semantic("mapping-before")
        after_semantic = deepcopy(before_semantic)
        after_semantic["intent"]["audience"] = "a different audience"  # type: ignore[index]
        before_digests = blueprint_decision_unit_digests(before_semantic)
        after_digests = blueprint_decision_unit_digests(after_semantic)
        self.assertNotEqual(
            before_digests["intent_goal"],
            after_digests["intent_goal"],
        )
        self.assertEqual(
            before_digests["intent_stance"],
            after_digests["intent_stance"],
        )
        active = {
            unit: {
                "event_id": f"event_{unit}",
                "confirmed_at": "2026-01-01T00:00:00+00:00",
                "observed_revision": 1,
                "presentation_sha256": "a" * 64,
                "_unit_semantic_sha256": digest,
            }
            for unit, digest in before_digests.items()
        }
        MediaRepository._invalidate_authority_for_revision(
            active,
            (
                "intent",
                "material_hints",
                "bindings",
                "assumptions",
                "open_decisions",
            ),
            after_digests,
        )
        invalidated = {unit for unit, value in active.items() if value is None}
        self.assertEqual(
            invalidated,
            {"intent_goal", "material_constraints", "references"},
        )
        self.assertIsNotNone(active["intent_stance"])
        self.assertIsNotNone(active["script"])
        self.assertIsNotNone(active["creative_direction"])
        self.assertIsNotNone(active["output"])
        self.assertIsNotNone(active["techniques"])

    def test_same_revision_confirm_revoke_confirm_order_is_preserved(self) -> None:
        active: dict[str, dict[str, object] | None] = {
            unit: None
            for unit in (
                "intent_goal",
                "intent_stance",
                "script",
                "creative_direction",
                "output",
                "material_constraints",
                "references",
                "techniques",
            )
        }

        def event(
            event_id: str,
            operation: str,
            active_refs: dict[str, str],
        ) -> dict[str, object]:
            return {
                "id": event_id,
                "created_at": "2026-01-01T00:00:00+00:00",
                "presentation_sha256": "a" * 64,
                "authority_operation": operation,
                "decision_units": ["script"],
                "unit_digests": {"script": "b" * 64},
                "active_confirmation_ids": active_refs,
                "observed_head": {"revision": 9},
            }

        MediaRepository._apply_validated_authority_event(
            active,
            event("confirm_one", "confirm", {}),
            validate_refs=True,
        )
        MediaRepository._apply_validated_authority_event(
            active,
            event("revoke_two", "revoke", {"script": "confirm_one"}),
            validate_refs=True,
        )
        MediaRepository._apply_validated_authority_event(
            active,
            event("confirm_three", "confirm", {}),
            validate_refs=True,
        )
        assert active["script"] is not None
        self.assertEqual(active["script"]["event_id"], "confirm_three")
        self.assertEqual(active["script"]["observed_revision"], 9)

    def test_historical_projection_validates_later_ledger_and_receipts_are_joined(
        self,
    ) -> None:
        repository, service, project_id, initial = self._create_blueprint_project()
        self._confirm_or_revoke(
            repository,
            project_id,
            operation="confirm",
            nonce="historical_confirm_script",
            idempotency_key="historical-confirm-script",
        )
        revision_one = repository.get_blueprint_head(project_id)
        assert revision_one is not None
        changed = deepcopy(initial)
        changed["script"]["blocks"][0]["text"] = "changed after confirmation"  # type: ignore[index]
        service.commit_proposal(
            project_id,
            {
                "expected_head": {
                    "revision": 1,
                    "content_sha256": revision_one["content_sha256"],
                },
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": changed,
            },
            idempotency_key="historical-change-script",
        )
        self._confirm_or_revoke(
            repository,
            project_id,
            operation="confirm",
            nonce="historical_confirm_output",
            idempotency_key="historical-confirm-output",
            decision_unit="output",
        )

        statements: list[str] = []
        with repository.transaction() as connection:
            connection.set_trace_callback(statements.append)
            historical = repository._authority_projection_in_transaction(
                connection,
                project_id,
                revision=1,
            )
            connection.set_trace_callback(None)
        self.assertTrue(historical["decision_units"]["script"]["confirmed"])
        current = repository.get_blueprint_authority_projection(project_id)
        self.assertFalse(current["decision_units"]["script"]["confirmed"])
        self.assertTrue(current["decision_units"]["output"]["confirmed"])
        authority_receipt_reads = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "blueprint_decision_authority_receipts" in statement
        ]
        self.assertEqual(len(authority_receipt_reads), 2)
        self.assertFalse(
            any(
                "event_id=" in statement.lower().split("where", 1)[-1]
                for statement in authority_receipt_reads
            )
        )
        self.assertTrue(
            any(
                "JOIN main.blueprint_decision_authority_receipts" in statement
                for statement in statements
            )
        )

        with repository._connect() as connection:
            connection.execute("DROP TRIGGER trg_blueprint_authority_receipts_no_update")
            connection.execute(
                """UPDATE blueprint_decision_authority_receipts SET response_json='{}'
                    WHERE event_id=(
                      SELECT id FROM blueprint_decision_authority_events
                       WHERE project_id=? ORDER BY sequence DESC LIMIT 1)""",
                (project_id,),
            )
        with self.assertRaisesRegex(
            BlueprintIntegrityError,
            "blueprint_authority_receipt",
        ):
            repository.get_blueprint_authority_projection(project_id, revision=1)

    def test_write_aggregate_capacity_is_preinsert_and_transaction_unchanged(
        self,
    ) -> None:
        repository, _service, project_id, _initial = self._create_blueprint_project()
        self._confirm_or_revoke(
            repository,
            project_id,
            operation="confirm",
            nonce="write_aggregate_confirm",
            idempotency_key="write-aggregate-confirm",
        )
        revoke = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="revoke",
            decision_units=["script"],
            runtime_authority_epoch="authority_resource_epoch",
        )
        with repository.transaction() as connection:
            budget = repository._preflight_authority_json(connection, project_id)
        before = self._authority_table_snapshot(repository, project_id)
        with patch(
            "core.media_db._BLUEPRINT_AUTHORITY_EVENT_JSON_AGGREGATE_MAX_BYTES",
            budget.event_json_bytes,
        ):
            with self.assertRaisesRegex(
                BlueprintAuthorityError,
                "blueprint_authority_capacity_exceeded",
            ):
                repository.revoke_blueprint_decision_units(
                    project_id,
                    runtime_authority_epoch="authority_resource_epoch",
                    presentation=revoke["presentation"],
                    presentation_sha256=str(revoke["presentation_sha256"]),
                    native_gesture_nonce="write_aggregate_event_failure",
                    idempotency_key="write-aggregate-event-failure",
                )
        self.assertEqual(self._authority_table_snapshot(repository, project_id), before)

        with patch(
            "core.media_db._BLUEPRINT_AUTHORITY_RECEIPT_JSON_AGGREGATE_MAX_BYTES",
            budget.receipt_json_bytes,
        ):
            with self.assertRaisesRegex(
                BlueprintAuthorityError,
                "blueprint_authority_capacity_exceeded",
            ):
                repository.revoke_blueprint_decision_units(
                    project_id,
                    runtime_authority_epoch="authority_resource_epoch",
                    presentation=revoke["presentation"],
                    presentation_sha256=str(revoke["presentation_sha256"]),
                    native_gesture_nonce="write_aggregate_receipt_failure",
                    idempotency_key="write-aggregate-receipt-failure",
                )
        self.assertEqual(self._authority_table_snapshot(repository, project_id), before)

    def test_capacity_math_accepts_exact_aggregate_and_rejects_plus_one(self) -> None:
        event_json = ("{}", "[]")
        response_json = "{}"
        event_delta = sum(len(value.encode()) for value in event_json)
        receipt_delta = len(response_json.encode())
        exact = _AuthorityJsonBudget(
            event_count=0,
            receipt_count=0,
            event_json_bytes=(
                _BLUEPRINT_AUTHORITY_EVENT_JSON_AGGREGATE_MAX_BYTES - event_delta
            ),
            receipt_json_bytes=(
                _BLUEPRINT_AUTHORITY_RECEIPT_JSON_AGGREGATE_MAX_BYTES
                - receipt_delta
            ),
        )
        MediaRepository._assert_authority_write_capacity(
            exact,
            event_json_values=event_json,
            receipt_response_json=response_json,
        )
        for over in (
            _AuthorityJsonBudget(
                event_count=0,
                receipt_count=0,
                event_json_bytes=exact.event_json_bytes + 1,
                receipt_json_bytes=exact.receipt_json_bytes,
            ),
            _AuthorityJsonBudget(
                event_count=0,
                receipt_count=0,
                event_json_bytes=exact.event_json_bytes,
                receipt_json_bytes=exact.receipt_json_bytes + 1,
            ),
        ):
            with self.assertRaisesRegex(
                BlueprintAuthorityError,
                "blueprint_authority_capacity_exceeded",
            ):
                MediaRepository._assert_authority_write_capacity(
                    over,
                    event_json_values=event_json,
                    receipt_response_json=response_json,
                )

    @staticmethod
    def _seed_valid_authority_events(
        repository: MediaRepository,
        project_id: str,
        count: int,
    ) -> None:
        with repository.transaction(immediate=True) as connection:
            ledger = repository._validate_authority_ledger(connection, project_id)
            database_uuid = repository._database_identity_in_transaction(connection)
            current_ref = repository._head_projection(ledger.current_head)
            active = {
                unit: None for unit in ledger.current_digests
            }
            event_rows: list[tuple[object, ...]] = []
            receipt_rows: list[tuple[object, ...]] = []
            prior_id: str | None = None
            prior_sha256: str | None = None
            created_at = "2026-01-01T00:00:00+00:00"
            for sequence in range(1, count + 1):
                operation = "confirm" if sequence % 2 else "revoke"
                event_id = f"authevt_bulk_{sequence}"
                active_id = (
                    active["script"]["event_id"]
                    if active["script"] is not None
                    else None
                )
                presentation = {
                    "object": "memolens.blueprint_decision_authority.presentation",
                    "schema_version": "1",
                    "database_uuid": database_uuid,
                    "runtime_authority_epoch": "bulk_authority_epoch",
                    "project_id": project_id,
                    "authority_operation": operation,
                    "observed_head": current_ref,
                    "decision_units": [
                        {
                            "name": "script",
                            "semantic_sha256": ledger.current_digests["script"],
                            "content": ledger.current_payloads["script"],
                        }
                    ],
                    "active_confirmation_ids": {"script": active_id},
                }
                presentation_sha256 = content_sha256(presentation)
                active_refs = {"script": str(active_id)} if operation == "revoke" else {}
                request_sha256 = content_sha256({"bulk_sequence": sequence})
                material = repository._authority_event_material(
                    event_id=event_id,
                    database_uuid=database_uuid,
                    project_id=project_id,
                    sequence=sequence,
                    parent_event_id=prior_id,
                    parent_event_sha256=prior_sha256,
                    authority_operation=operation,
                    observed_head=current_ref,
                    decision_units=["script"],
                    unit_digests={"script": ledger.current_digests["script"]},
                    active_confirmation_ids=active_refs,
                    presentation_sha256=presentation_sha256,
                    runtime_authority_epoch="bulk_authority_epoch",
                    native_gesture_nonce=f"bulk_gesture_{sequence}",
                    request_sha256=request_sha256,
                    created_at=created_at,
                )
                event_sha256 = content_sha256(material)
                event = {**material, "event_sha256": event_sha256}
                repository._apply_validated_authority_event(
                    active,
                    event,
                    validate_refs=True,
                )
                projection = repository._projection_from_active(
                    revision=int(current_ref["revision"]),
                    content_sha256_value=str(current_ref["content_sha256"]),
                    decision_digests=ledger.current_digests,
                    active=active,
                )
                response = repository._authority_command_response(
                    project_id=project_id,
                    authority_operation=operation,
                    event_id=event_id,
                    projection=projection,
                )
                serialized_response = canonical_json(response)
                response_sha256 = content_sha256(response)
                resource_id = f"{project_id}:{event_id}"
                command_type = f"blueprint.authority.{operation}"
                receipt_material = repository._authority_receipt_material(
                    database_uuid=database_uuid,
                    authenticated_principal="main_native_user_gesture",
                    project_id=project_id,
                    command_type=command_type,
                    command_version="1",
                    idempotency_key=f"bulk-idempotency-{sequence}",
                    request_sha256=request_sha256,
                    event_id=event_id,
                    response_status=201,
                    response_sha256=response_sha256,
                    resource_type="blueprint_decision_authority",
                    resource_id=resource_id,
                    created_at=created_at,
                )
                event_rows.append(
                    (
                        event_id,
                        database_uuid,
                        project_id,
                        sequence,
                        prior_id,
                        prior_sha256,
                        operation,
                        current_ref["revision"],
                        current_ref["content_sha256"],
                        current_ref["semantic_sha256"],
                        current_ref["operation_id"],
                        canonical_json(["script"]),
                        canonical_json({"script": ledger.current_digests["script"]}),
                        canonical_json(active_refs),
                        canonical_json(presentation),
                        presentation_sha256,
                        "bulk_authority_epoch",
                        f"bulk_gesture_{sequence}",
                        request_sha256,
                        event_sha256,
                        created_at,
                    )
                )
                receipt_rows.append(
                    (
                        database_uuid,
                        "main_native_user_gesture",
                        project_id,
                        command_type,
                        "1",
                        f"bulk-idempotency-{sequence}",
                        request_sha256,
                        event_id,
                        201,
                        serialized_response,
                        response_sha256,
                        content_sha256(receipt_material),
                        "blueprint_decision_authority",
                        resource_id,
                        created_at,
                    )
                )
                prior_id = event_id
                prior_sha256 = event_sha256

            connection.executemany(
                """INSERT INTO blueprint_decision_authority_events(
                     id,database_uuid,project_id,sequence,parent_event_id,parent_event_sha256,
                     authority_operation,observed_revision,observed_content_sha256,
                     observed_semantic_sha256,observed_operation_id,decision_units_json,
                     unit_digests_json,active_confirmation_ids_json,presentation_json,
                     presentation_sha256,runtime_authority_epoch,native_gesture_nonce,
                     request_sha256,event_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                event_rows,
            )
            connection.executemany(
                """INSERT INTO blueprint_decision_authority_receipts(
                     database_uuid,authenticated_principal,project_id,command_type,
                     command_version,idempotency_key,request_sha256,event_id,response_status,
                     response_json,response_sha256,receipt_sha256,resource_type,resource_id,
                     created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                receipt_rows,
            )
            connection.execute(
                "DROP TRIGGER trg_blueprint_authority_heads_initial_sequence"
            )
            connection.execute(
                """INSERT INTO blueprint_decision_authority_heads(
                     project_id,sequence,event_id,event_sha256,updated_at)
                   VALUES(?,?,?,?,?)""",
                (project_id, count, prior_id, prior_sha256, created_at),
            )
            initial_trigger = next(
                statement
                for statement in V5_SCHEMA_STATEMENTS
                if "trg_blueprint_authority_heads_initial_sequence" in statement
            )
            connection.execute(initial_trigger)

    def test_real_4096_boundary_allows_last_write_and_replay_but_rejects_4097(
        self,
    ) -> None:
        repository, _service, project_id, _initial = self._create_blueprint_project()
        self._seed_valid_authority_events(
            repository,
            project_id,
            _BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS - 1,
        )
        boundary_envelope, boundary = self._confirm_or_revoke(
            repository,
            project_id,
            operation="revoke",
            nonce="boundary_gesture_4096",
            idempotency_key="boundary-idempotency-4096",
        )
        self.assertFalse(boundary.replayed)
        self.assertEqual(
            self._authority_table_snapshot(repository, project_id)[:2],
            (_BLUEPRINT_AUTHORITY_EVENT_MAX_ROWS,) * 2,
        )

        replay = repository.revoke_blueprint_decision_units(
            project_id,
            runtime_authority_epoch="authority_resource_epoch",
            presentation=boundary_envelope["presentation"],
            presentation_sha256=str(boundary_envelope["presentation_sha256"]),
            native_gesture_nonce="boundary_gesture_4096",
            idempotency_key="boundary-idempotency-4096",
        )
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.event_id, boundary.event_id)

        over = repository.build_blueprint_authority_presentation(
            project_id,
            authority_operation="confirm",
            decision_units=["script"],
            runtime_authority_epoch="authority_resource_epoch",
        )
        before = self._authority_table_snapshot(repository, project_id)
        with self.assertRaisesRegex(
            BlueprintAuthorityError,
            "blueprint_authority_capacity_exceeded",
        ):
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="authority_resource_epoch",
                presentation=over["presentation"],
                presentation_sha256=str(over["presentation_sha256"]),
                native_gesture_nonce="boundary_gesture_4097",
                idempotency_key="boundary-idempotency-4097",
            )
        self.assertEqual(self._authority_table_snapshot(repository, project_id), before)


if __name__ == "__main__":
    unittest.main()
