from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import gc
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PLUGIN_ROOT / "scripts"
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(SCRIPTS))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from core.blueprint_contract import (  # noqa: E402
    BLUEPRINT_MAX_SEMANTIC_BYTES,
    canonical_json,
)
from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import MediaRepository  # noqa: E402
import memolens_blueprint_authority as authority  # noqa: E402
from memolens_contracts import MemoLensError  # noqa: E402
from memolens_persisted_blueprint import PersistedBlueprintReader  # noqa: E402
from memolens_project_store import ProjectIndexReader  # noqa: E402
from memolens_sqlite import ReadOnlyDatabase  # noqa: E402


def _large_semantic() -> dict[str, Any]:
    # 43 legal 12k script blocks put the semantic value close to Core's
    # 512 KiB ceiling without needing evidence rows or artificial DB writes.
    return {
        "intent": {
            "goal": "turn local footage into a coherent story",
            "stance": "ordinary memories deserve careful editing",
            "audience": "individual creators",
            "platform": "short-video",
        },
        "script": {
            "blocks": [
                {"block_id": f"block_{index}", "text": "x" * 12_000}
                for index in range(43)
            ]
        },
        "direction": {
            "theme": "rediscovery",
            "narrative_arc": "forgotten to found",
            "emotion": "calm",
            "tone": "restrained",
            "pace": "measured",
        },
        "output": {"duration_target_ms": 30_000, "aspect_ratio": "9:16"},
        "constraints": {"must_include": [], "must_exclude": []},
        "material_hints": [],
        "reference_refs": [],
        "technique_refs": [],
        "bindings": {"creator_context": None, "wiki_generation": None},
        "assumptions": [],
        "missing_evidence": [],
        "open_decisions": [],
    }


def _small_semantic(script_text: str) -> dict[str, Any]:
    semantic = _large_semantic()
    semantic["script"] = {
        "blocks": [{"block_id": "opening", "text": script_text}]
    }
    return semantic


class _LargeAuthorityFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-large-authority-"
        )
        root = Path(self.temporary.name).resolve()
        library = root / "library"
        library.mkdir()
        self.db_path = root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(library)
        project = self.repository.create_project(
            "Large authority presentation",
            {"goal": "legacy"},
            {"source": "test"},
        )
        self.project_id = str(project["id"])
        brief = self.repository.get_brief(self.project_id, 1)
        assert brief is not None
        BlueprintService(self.repository).commit_proposal(
            self.project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": _large_semantic(),
            },
            idempotency_key="large-authority-initial",
        )
        envelope = self.repository.build_blueprint_authority_presentation(
            self.project_id,
            authority_operation="confirm",
            decision_units=list(authority.DECISION_UNITS),
            runtime_authority_epoch="epoch_large_authority",
        )
        self.presentation_bytes = len(
            canonical_json(envelope["presentation"]).encode("utf-8")
        )
        self.repository.confirm_blueprint_decision_units(
            self.project_id,
            runtime_authority_epoch="epoch_large_authority",
            presentation=envelope["presentation"],
            presentation_sha256=str(envelope["presentation_sha256"]),
            native_gesture_nonce="gesture_large_authority",
            idempotency_key="confirm-large-authority",
        )

    def close(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def connection(self, path: Path | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(path or self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def snapshot(self) -> tuple[dict[str, Any], dict[int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]]:
        head = self.repository.get_blueprint_head(self.project_id)
        assert head is not None
        with closing(self.connection()) as connection:
            rows = connection.execute(
                "SELECT * FROM creative_blueprint_revisions WHERE project_id=? ORDER BY revision",
                (self.project_id,),
            ).fetchall()
        revision_chain = {
            int(row["revision"]): (
                dict(row),
                json.loads(str(row["blueprint_json"])),
                {},
            )
            for row in rows
        }
        return dict(head), revision_chain

    def copy_database(self, destination: Path) -> None:
        with closing(sqlite3.connect(self.db_path)) as source, closing(
            sqlite3.connect(destination)
        ) as target:
            source.backup(target)


class BlueprintAuthorityResourceTests(unittest.TestCase):
    revision_statement_counts: dict[int, tuple[int, int]] = {}
    restore_revision_statement_counts: dict[int, tuple[int, int]] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = _LargeAuthorityFixture()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def _validate(
        self,
        *,
        path: Path | None = None,
        current_head: dict[str, Any] | None = None,
        revision_chain: dict[
            int, tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
        ]
        | None = None,
    ) -> authority.ValidatedAuthorityLedger:
        if current_head is None or revision_chain is None:
            current_head, revision_chain = self.fixture.snapshot()
        with closing(self.fixture.connection(path)) as connection:
            return authority.validate_authority_ledger(
                connection,
                project_id=self.fixture.project_id,
                current_head=current_head,
                revision_chain=revision_chain,
                authority_available=True,
            )

    def test_reader_budget_matches_frozen_core_writer_contract(self) -> None:
        self.assertEqual(authority._AUTHORITY_JSON_MAX_BYTES, 1_048_576)
        self.assertEqual(authority._MAX_RECEIPT_RESPONSE_BYTES, 1_048_576)
        self.assertEqual(authority._MAX_AUTHORITY_EVENTS, 4_096)
        self.assertEqual(
            authority._MAX_AUTHORITY_EVENT_JSON_BYTES,
            64 * 1_048_576,
        )
        self.assertEqual(
            authority._MAX_AUTHORITY_RECEIPT_JSON_BYTES,
            64 * 1_048_576,
        )

    def test_core_legal_near_limit_presentation_is_agent_recoverable(self) -> None:
        semantic_bytes = len(canonical_json(_large_semantic()).encode("utf-8"))
        self.assertGreater(semantic_bytes, 500_000)
        self.assertLessEqual(semantic_bytes, BLUEPRINT_MAX_SEMANTIC_BYTES)
        self.assertGreater(self.fixture.presentation_bytes, 262_144)
        self.assertGreater(self.fixture.presentation_bytes, 500_000)
        self.assertLessEqual(
            self.fixture.presentation_bytes,
            authority._AUTHORITY_JSON_MAX_BYTES,
        )

        ledger = self._validate()
        head, revision_chain = self.fixture.snapshot()
        projection = authority.project_validated_authority(
            revision_chain=revision_chain,
            validated_events=ledger,
            selected_revision=int(head["revision"]),
        )
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(
            projection["confirmed_decision_unit_count"],
            len(authority.DECISION_UNITS),
        )

    def test_over_core_per_value_limit_fails_before_json_materialization(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-authority-oversize-"
        ) as temporary:
            copied = Path(temporary) / "oversize.db"
            self.fixture.copy_database(copied)
            with closing(sqlite3.connect(copied)) as connection, connection:
                connection.execute(
                    "DROP TRIGGER trg_blueprint_authority_events_no_update"
                )
                connection.execute(
                    "UPDATE blueprint_decision_authority_events SET presentation_json=?",
                    ("x" * (authority._AUTHORITY_JSON_MAX_BYTES + 1),),
                )
            with mock.patch.object(
                authority,
                "_strict_json",
                side_effect=AssertionError("oversized JSON was materialized"),
            ):
                with self.assertRaises(MemoLensError) as raised:
                    self._validate(path=copied)
            self.assertEqual(
                raised.exception.code,
                "blueprint_authority_integrity_error",
            )

    def test_aggregate_limit_fails_before_json_materialization(self) -> None:
        with mock.patch.object(
            authority,
            "_MAX_AUTHORITY_EVENT_JSON_BYTES",
            self.fixture.presentation_bytes - 1,
        ), mock.patch.object(
            authority,
            "_strict_json",
            side_effect=AssertionError("aggregate-over-budget JSON was materialized"),
        ):
            with self.assertRaises(MemoLensError) as raised:
                self._validate()
        self.assertEqual(
            raised.exception.code,
            "blueprint_authority_integrity_error",
        )

    def test_streamed_state_is_event_bounded_and_does_not_resurrect(self) -> None:
        real_head, real_chain = self.fixture.snapshot()
        base_bundle = real_chain[1]
        base_row, base_payloads, base_digests = authority._revision_material(
            real_chain, 1
        )
        revision_count = 10_000
        synthetic_chain = {
            revision: base_bundle for revision in range(1, revision_count + 1)
        }
        synthetic_head = {**real_head, "revision": revision_count}
        calls = 0

        def synthetic_revision_material(
            _chain: object,
            revision: int,
        ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
            nonlocal calls
            calls += 1
            row = {**base_row, "revision": revision}
            digests = dict(base_digests)
            if 5_000 <= revision < 7_000:
                digests["script"] = "f" * 64
            return row, base_payloads, digests

        statements: list[str] = []
        with closing(self.fixture.connection()) as connection, mock.patch.object(
            authority,
            "_revision_material",
            side_effect=synthetic_revision_material,
        ):
            connection.set_trace_callback(statements.append)
            ledger = authority.validate_authority_ledger(
                connection,
                project_id=self.fixture.project_id,
                current_head=synthetic_head,
                revision_chain=synthetic_chain,
                authority_available=True,
            )
            validation_calls = calls
            projection = authority.project_validated_authority(
                revision_chain=synthetic_chain,
                validated_events=ledger,
                selected_revision=revision_count,
            )

        self.assertEqual(validation_calls, revision_count)
        self.assertLess(len(statements), 12)
        self.assertFalse(hasattr(ledger, "semantic_change_revisions"))
        self.assertEqual(len(ledger.events), 1)
        self.assertEqual(
            sum(len(values) for values in ledger.unit_event_values.values()),
            len(authority.DECISION_UNITS),
        )
        self.assertEqual(
            ledger.unit_event_values["script"][0]["invalidated_at_revision"],
            5_000,
        )
        self.assertFalse(projection["decision_units"]["script"]["confirmed"])
        self.assertEqual(
            projection["decision_units"]["script"]["semantic_sha256"],
            base_digests["script"],
        )
        self.assertLessEqual(
            len(ledger.projection_cache),
            authority._MAX_AUTHORITY_PROJECTION_CACHE,
        )

    def test_event_receipt_join_stream_never_fetches_all_or_retains_payloads(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-authority-join-stream-"
        ) as temporary:
            root = Path(temporary)
            library = root / "library"
            library.mkdir()
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            self.addCleanup(repository.close)
            repository.ensure_schema(library)
            project = repository.create_project(
                "Authority join stream",
                {"goal": "legacy"},
                {"source": "test"},
            )
            project_id = str(project["id"])
            brief = repository.get_brief(project_id, 1)
            assert brief is not None
            BlueprintService(repository).commit_proposal(
                project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": _small_semantic("join stream"),
                },
                idempotency_key="join-stream-initial",
            )
            for sequence in range(1, 17):
                operation = "confirm" if sequence % 2 else "revoke"
                envelope = repository.build_blueprint_authority_presentation(
                    project_id,
                    authority_operation=operation,
                    decision_units=["script"],
                    runtime_authority_epoch="join_stream_epoch",
                )
                command = (
                    repository.confirm_blueprint_decision_units
                    if operation == "confirm"
                    else repository.revoke_blueprint_decision_units
                )
                command(
                    project_id,
                    runtime_authority_epoch="join_stream_epoch",
                    presentation=envelope["presentation"],
                    presentation_sha256=str(envelope["presentation_sha256"]),
                    native_gesture_nonce=f"join_stream_gesture_{sequence}",
                    idempotency_key=f"join-stream-{sequence}",
                )

            head = repository.get_blueprint_head(project_id)
            assert head is not None
            with closing(sqlite3.connect(db_path)) as snapshot_connection:
                snapshot_connection.row_factory = sqlite3.Row
                revision_rows = snapshot_connection.execute(
                    "SELECT * FROM creative_blueprint_revisions "
                    "WHERE project_id=? ORDER BY revision",
                    (project_id,),
                ).fetchall()
            revision_chain = {
                int(row["revision"]): (
                    dict(row),
                    json.loads(str(row["blueprint_json"])),
                    {},
                )
                for row in revision_rows
            }

            stats = {
                "fetchall_attempts": 0,
                "join_cursors": 0,
                "live_events": 0,
                "peak_live_events": 0,
            }

            class GuardedCursor:
                def __init__(self, cursor: sqlite3.Cursor) -> None:
                    self._cursor = cursor

                def __iter__(self) -> GuardedCursor:
                    return self

                def __next__(self) -> sqlite3.Row:
                    return next(self._cursor)

                def fetchone(self) -> sqlite3.Row | None:
                    return self._cursor.fetchone()

                def fetchall(self) -> list[sqlite3.Row]:
                    stats["fetchall_attempts"] += 1
                    raise AssertionError("authority event/receipt cursor used fetchall")

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._cursor, name)

            class GuardedConnection:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self._connection = connection

                def execute(
                    self,
                    sql: str,
                    parameters: tuple[object, ...] = (),
                ) -> sqlite3.Cursor | GuardedCursor:
                    cursor = self._connection.execute(sql, parameters)
                    normalized = " ".join(sql.lower().split())
                    if (
                        "blueprint_decision_authority_events" in normalized
                        or "blueprint_decision_authority_receipts" in normalized
                    ):
                        if (
                            " join blueprint_decision_authority_receipts "
                            in normalized
                        ):
                            stats["join_cursors"] += 1
                        return GuardedCursor(cursor)
                    return cursor

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._connection, name)

            class TrackedEvent(dict[str, Any]):
                def __init__(self, value: dict[str, Any]) -> None:
                    super().__init__(value)
                    stats["live_events"] += 1
                    stats["peak_live_events"] = max(
                        stats["peak_live_events"],
                        stats["live_events"],
                    )

                def __del__(self) -> None:
                    stats["live_events"] -= 1

            real_decode_event = authority._decode_event

            def tracked_decode_event(row: sqlite3.Row) -> TrackedEvent:
                return TrackedEvent(real_decode_event(row))

            connection = sqlite3.connect(db_path)
            connection.row_factory = sqlite3.Row
            try:
                with mock.patch.object(
                    authority,
                    "_decode_event",
                    side_effect=tracked_decode_event,
                ):
                    ledger = authority.validate_authority_ledger(
                        GuardedConnection(connection),  # type: ignore[arg-type]
                        project_id=project_id,
                        current_head=head,
                        revision_chain=revision_chain,
                        authority_available=True,
                    )
            finally:
                connection.close()
            gc.collect()

        self.assertEqual(stats["fetchall_attempts"], 0)
        self.assertEqual(stats["join_cursors"], 1)
        self.assertLessEqual(stats["peak_live_events"], 2)
        self.assertEqual(stats["live_events"], 0)
        self.assertEqual(len(ledger.events), 16)
        self.assertTrue(
            all(
                "presentation" not in event
                and "_projection_after" not in event
                and "unit_digests" not in event
                for event in ledger.events
            )
        )

    def test_real_sqlite_revision_stream_has_constant_statement_slope(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-authority-revision-slope-"
        ) as temporary:
            root = Path(temporary)
            library = root / "library"
            library.mkdir()
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            self.addCleanup(repository.close)
            repository.ensure_schema(library)
            project = repository.create_project(
                "Authority revision slope",
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
                    "semantic": _small_semantic("revision 1"),
                },
                idempotency_key="revision-slope-1",
            )
            result = created.response["result"]
            assert isinstance(result, dict)
            head = result["result_head"]
            assert isinstance(head, dict)
            envelope = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=["script"],
                runtime_authority_epoch="revision_slope_epoch",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="revision_slope_epoch",
                presentation=envelope["presentation"],
                presentation_sha256=str(envelope["presentation_sha256"]),
                native_gesture_nonce="revision_slope_confirmation",
                idempotency_key="revision-slope-confirmation",
            )

            snapshots: dict[int, Path] = {}

            def freeze(revision: int) -> None:
                destination = root / f"revision-{revision}.db"
                with closing(sqlite3.connect(db_path)) as source, closing(
                    sqlite3.connect(destination)
                ) as target:
                    source.backup(target)
                snapshots[revision] = destination

            for revision in range(2, 101):
                committed = service.commit_proposal(
                    project_id,
                    {
                        "expected_head": {
                            "revision": head["revision"],
                            "content_sha256": head["content_sha256"],
                        },
                        "initial_legacy_brief": None,
                        "source_candidate_sha256": None,
                        "semantic": _small_semantic(f"revision {revision}"),
                    },
                    idempotency_key=f"revision-slope-{revision}",
                )
                result = committed.response["result"]
                assert isinstance(result, dict)
                head = result["result_head"]
                assert isinstance(head, dict)
                if revision in {10, 50, 100}:
                    freeze(revision)

            counts: dict[int, tuple[int, int]] = {}
            for revision, snapshot in snapshots.items():
                database = ReadOnlyDatabase(snapshot)
                reader = PersistedBlueprintReader(
                    database,
                    ProjectIndexReader(database),
                )
                statements: list[str] = []
                with closing(database.connection()) as connection:
                    connection.set_trace_callback(statements.append)
                    read = reader.get_in_connection(connection, project_id)
                    connection.set_trace_callback(None)
                self.assertEqual(read["blueprint"]["revision"], revision)
                selects = sum(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in statements
                )
                stream_selects = sum(
                    "r.revision<=" in " ".join(statement.lower().split())
                    for statement in statements
                )
                self.assertEqual(stream_selects, 1)
                counts[revision] = (selects, len(statements))

        type(self).revision_statement_counts = counts
        self.assertEqual(counts[10], counts[50])
        self.assertEqual(counts[50], counts[100])

    def test_restore_heavy_revision_stream_has_constant_statement_slope(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-authority-restore-slope-"
        ) as temporary:
            root = Path(temporary)
            library = root / "library"
            library.mkdir()
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            self.addCleanup(repository.close)
            repository.ensure_schema(library)
            project = repository.create_project(
                "Authority restore slope",
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
                    "semantic": _small_semantic("restore base"),
                },
                idempotency_key="restore-slope-1",
            )
            result = created.response["result"]
            assert isinstance(result, dict)
            head = result["result_head"]
            assert isinstance(head, dict)
            restore_target = {
                "revision": head["revision"],
                "content_sha256": head["content_sha256"],
            }
            envelope = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=["script"],
                runtime_authority_epoch="restore_slope_epoch",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="restore_slope_epoch",
                presentation=envelope["presentation"],
                presentation_sha256=str(envelope["presentation_sha256"]),
                native_gesture_nonce="restore_slope_confirmation",
                idempotency_key="restore-slope-confirmation",
            )

            snapshots: dict[int, Path] = {}

            def freeze(revision: int) -> None:
                destination = root / f"restore-revision-{revision}.db"
                with closing(sqlite3.connect(db_path)) as source, closing(
                    sqlite3.connect(destination)
                ) as target:
                    source.backup(target)
                snapshots[revision] = destination

            for revision in range(2, 101):
                expected_head = {
                    "revision": head["revision"],
                    "content_sha256": head["content_sha256"],
                }
                if revision % 2 == 1:
                    changed = service.restore_revision(
                        project_id,
                        {
                            "expected_head": expected_head,
                            "restore_from": restore_target,
                        },
                        idempotency_key=f"restore-slope-{revision}",
                    )
                else:
                    changed = service.commit_proposal(
                        project_id,
                        {
                            "expected_head": expected_head,
                            "initial_legacy_brief": None,
                            "source_candidate_sha256": None,
                            "semantic": _small_semantic(
                                f"restore alternate {revision}"
                            ),
                        },
                        idempotency_key=f"restore-slope-{revision}",
                    )
                result = changed.response["result"]
                assert isinstance(result, dict)
                head = result["result_head"]
                assert isinstance(head, dict)
                if revision in {10, 50, 100}:
                    freeze(revision)

            counts: dict[int, tuple[int, int]] = {}
            for revision, snapshot in snapshots.items():
                database = ReadOnlyDatabase(snapshot)
                reader = PersistedBlueprintReader(
                    database,
                    ProjectIndexReader(database),
                )
                statements: list[str] = []
                with closing(database.connection()) as connection:
                    connection.set_trace_callback(statements.append)
                    read = reader.get_in_connection(connection, project_id)
                    connection.set_trace_callback(None)
                self.assertEqual(read["blueprint"]["revision"], revision)
                selects = sum(
                    statement.lstrip().upper().startswith("SELECT")
                    for statement in statements
                )
                stream_selects = sum(
                    "r.revision<=" in " ".join(statement.lower().split())
                    for statement in statements
                )
                self.assertEqual(stream_selects, 1)
                counts[revision] = (selects, len(statements))

        type(self).restore_revision_statement_counts = counts
        self.assertEqual(counts[10], counts[50])
        self.assertEqual(counts[50], counts[100])

    def test_projection_cache_has_a_revision_independent_ceiling(self) -> None:
        ledger = authority._build_validated_ledger(events=[])
        base_row, base_document, base_operation = self.fixture.snapshot()[1][1]
        revision_chain = {
            revision: (
                {**deepcopy(base_row), "revision": revision},
                base_document,
                base_operation,
            )
            for revision in range(
                1,
                authority._MAX_AUTHORITY_PROJECTION_CACHE + 3,
            )
        }
        for revision in revision_chain:
            authority.project_validated_authority(
                revision_chain=revision_chain,
                validated_events=ledger,
                selected_revision=revision,
            )
        self.assertEqual(
            len(ledger.projection_cache),
            authority._MAX_AUTHORITY_PROJECTION_CACHE,
        )

    def test_core_plugin_projection_parity_for_same_revision_events_and_restore(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-authority-parity-"
        ) as temporary:
            root = Path(temporary)
            library = root / "library"
            library.mkdir()
            db_path = root / "media.db"
            ImageIndexRepository(db_path).ensure_schema()
            repository = MediaRepository(db_path)
            self.addCleanup(repository.close)
            repository.ensure_schema(library)
            project = repository.create_project(
                "Authority projection parity",
                {"goal": "legacy"},
                {"source": "test"},
            )
            project_id = str(project["id"])
            brief = repository.get_brief(project_id, 1)
            assert brief is not None
            service = BlueprintService(repository)
            service.commit_proposal(
                project_id,
                {
                    "expected_head": None,
                    "initial_legacy_brief": {
                        "revision": 1,
                        "content_sha256": brief["content_sha256"],
                    },
                    "source_candidate_sha256": None,
                    "semantic": _small_semantic("revision one"),
                },
                idempotency_key="parity-revision-one",
            )
            revision_one = repository.get_blueprint_head(project_id)
            assert revision_one is not None
            first_confirmation = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=["script", "output"],
                runtime_authority_epoch="epoch_projection_parity",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="epoch_projection_parity",
                presentation=first_confirmation["presentation"],
                presentation_sha256=str(
                    first_confirmation["presentation_sha256"]
                ),
                native_gesture_nonce="gesture_parity_one",
                idempotency_key="parity-confirm-one",
            )

            service.commit_proposal(
                project_id,
                {
                    "expected_head": {
                        "revision": revision_one["revision"],
                        "content_sha256": revision_one["content_sha256"],
                    },
                    "initial_legacy_brief": None,
                    "source_candidate_sha256": None,
                    "semantic": _small_semantic("revision two changes script"),
                },
                idempotency_key="parity-revision-two",
            )
            revision_two = repository.get_blueprint_head(project_id)
            assert revision_two is not None
            second_confirmation = repository.build_blueprint_authority_presentation(
                project_id,
                authority_operation="confirm",
                decision_units=["script"],
                runtime_authority_epoch="epoch_projection_parity",
            )
            repository.confirm_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="epoch_projection_parity",
                presentation=second_confirmation["presentation"],
                presentation_sha256=str(
                    second_confirmation["presentation_sha256"]
                ),
                native_gesture_nonce="gesture_parity_two",
                idempotency_key="parity-confirm-two",
            )
            same_revision_revoke = (
                repository.build_blueprint_authority_presentation(
                    project_id,
                    authority_operation="revoke",
                    decision_units=["output"],
                    runtime_authority_epoch="epoch_projection_parity",
                )
            )
            repository.revoke_blueprint_decision_units(
                project_id,
                runtime_authority_epoch="epoch_projection_parity",
                presentation=same_revision_revoke["presentation"],
                presentation_sha256=str(
                    same_revision_revoke["presentation_sha256"]
                ),
                native_gesture_nonce="gesture_parity_revoke",
                idempotency_key="parity-revoke-output",
            )

            service.restore_revision(
                project_id,
                {
                    "expected_head": {
                        "revision": revision_two["revision"],
                        "content_sha256": revision_two["content_sha256"],
                    },
                    "restore_from": {
                        "revision": revision_one["revision"],
                        "content_sha256": revision_one["content_sha256"],
                    },
                },
                idempotency_key="parity-restore-one",
            )
            revision_three = repository.get_blueprint_head(project_id)
            assert revision_three is not None

            with closing(sqlite3.connect(db_path)) as connection:
                connection.row_factory = sqlite3.Row
                rows = connection.execute(
                    "SELECT * FROM creative_blueprint_revisions WHERE project_id=? ORDER BY revision",
                    (project_id,),
                ).fetchall()
                revision_chain = {
                    int(row["revision"]): (
                        dict(row),
                        json.loads(str(row["blueprint_json"])),
                        {},
                    )
                    for row in rows
                }
                ledger = authority.validate_authority_ledger(
                    connection,
                    project_id=project_id,
                    current_head=revision_three,
                    revision_chain=revision_chain,
                    authority_available=True,
                )

            expected_confirmed = {
                1: {"script", "output"},
                2: {"script"},
                3: set(),
            }
            for revision, expected in expected_confirmed.items():
                plugin_projection = authority.project_validated_authority(
                    revision_chain=revision_chain,
                    validated_events=ledger,
                    selected_revision=revision,
                )
                core_projection = repository.get_blueprint_authority_projection(
                    project_id,
                    revision=revision,
                )

                def confirmed_units(projection: dict[str, Any]) -> set[str]:
                    return {
                        unit
                        for unit, value in projection["decision_units"].items()
                        if value["confirmed"] is True
                    }

                self.assertEqual(confirmed_units(plugin_projection), expected)
                self.assertEqual(plugin_projection, core_projection)


if __name__ == "__main__":
    unittest.main()
