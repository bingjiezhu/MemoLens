from __future__ import annotations

from contextlib import closing, contextmanager
import copy
import hashlib
import json
import os
import sqlite3
import unittest
from unittest.mock import patch

from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import IMAGE_INDEX_SQL_COLUMNS
from tests import test_image_projection_schema_guards as schema_guards
from tests import test_image_projection_verified_read as verified_read


class _InjectedRemovalFault(RuntimeError):
    pass


class _FaultingConnection:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        target: str,
        phase: str,
    ) -> None:
        self._connection = connection
        self._target = target
        self._phase = phase
        self.hits = 0

    @staticmethod
    def _normalized(statement: str) -> str:
        return " ".join(statement.split()).upper()

    def _matches(self, statement: str) -> bool:
        normalized = self._normalized(statement)
        if self._target == "source":
            return normalized.startswith("UPDATE ASSET_SOURCES")
        if self._target == "change":
            return normalized.startswith("INSERT INTO IMAGE_PROJECTION_CHANGES")
        if self._target == "generation":
            return normalized.startswith("INSERT INTO IMAGE_PROJECTION_GENERATIONS")
        if self._target == "receipt":
            return normalized.startswith("INSERT INTO IMAGE_PROJECTION_RECEIPTS")
        if self._target == "manifest":
            return normalized.startswith("INSERT INTO IMAGE_PROJECTION_MANIFESTS")
        if self._target == "read_manifest":
            return normalized.startswith(
                "INSERT INTO IMAGE_PROJECTION_READ_MANIFESTS"
            )
        if self._target == "physical_clear":
            return normalized == "DELETE FROM IMAGE_INDEX WHERE ID=?"
        if self._target == "materialize":
            return normalized.startswith("INSERT INTO IMAGE_INDEX")
        if self._target == "deactivate":
            return (
                normalized.startswith("UPDATE IMAGE_PROJECTION_GENERATIONS")
                and "SET IS_ACTIVE=0" in normalized
            )
        if self._target == "activate":
            return (
                normalized.startswith("UPDATE IMAGE_PROJECTION_GENERATIONS")
                and "SET IS_ACTIVE=1" in normalized
            )
        return False

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        matched = self._matches(statement)
        if matched and self._phase == "before":
            self.hits += 1
            raise _InjectedRemovalFault(f"{self._target}:before")
        cursor = self._connection.execute(statement, parameters)
        if matched and self._phase == "after":
            self.hits += 1
            raise _InjectedRemovalFault(f"{self._target}:after")
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


@contextmanager
def _faulting_transactions(
    repository: object,
    *,
    target: str,
    phase: str,
) -> object:
    real_transaction = repository.transaction
    wrapper: _FaultingConnection | None = None

    @contextmanager
    def faulting_transaction(*, immediate: bool = False) -> object:
        nonlocal wrapper
        with real_transaction(immediate=immediate) as connection:
            wrapper = _FaultingConnection(
                connection,
                target=target,
                phase=phase,
            )
            yield wrapper

    with patch.object(repository, "transaction", faulting_transaction):
        yield lambda: 0 if wrapper is None else wrapper.hits


class ImageProjectionRemovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = schema_guards.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.first = self.fixture.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self.second_asset_id, second_job_id = (
            verified_read.ImageProjectionVerifiedReadTests._publish_second_asset(
                self
            )
        )
        self.second = self.fixture.repository.project_image_analysis_change(
            job_id=second_job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        with self.fixture.repository.transaction() as connection:
            source = connection.execute(
                """SELECT updated_at FROM asset_sources WHERE id=?""",
                (self.fixture.source_id,),
            ).fetchone()
        assert source is not None
        self.expected_source_updated_at = str(source["updated_at"])

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _mark(
        self,
        *,
        availability: str = "missing",
        asset_id: str | None = None,
        analysis_binding: dict[str, object] | None = None,
        source_updated_at: str | None = None,
    ) -> dict[str, object]:
        return self.fixture.repository.mark_image_source_unavailable(
            source_id=self.fixture.source_id,
            expected_asset_id=asset_id or self.fixture.asset_id,
            expected_analysis_binding=(
                analysis_binding or dict(self.fixture.analysis_binding)
            ),
            expected_source_updated_at=(
                source_updated_at or self.expected_source_updated_at
            ),
            availability=availability,
        )

    def _project(self, change_position: int) -> dict[str, object]:
        return self.fixture.repository.project_image_source_removal(
            change_position=change_position,
        )

    def _snapshot(self) -> str:
        tables = (
            "asset_sources",
            "image_analysis_results",
            "image_analysis_heads",
            "image_projection_changes",
            "image_projection_generations",
            "image_projection_rows",
            "image_projection_receipts",
            "image_projection_manifests",
            "image_projection_read_manifests",
            "legacy_image_aliases",
            "image_projection_aliases",
            "image_index",
        )
        with self.fixture.repository.transaction() as connection:
            snapshot: dict[str, list[list[object]]] = {}
            for table in tables:
                rows = connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                snapshot[table] = [
                    [
                        {"bytes": value.hex()}
                        if isinstance(value, bytes)
                        else value
                        for value in tuple(row)
                    ]
                    for row in rows
                ]
        return json.dumps(
            snapshot,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def _physical_ids(self) -> list[str]:
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM image_index ORDER BY id"
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def _source_updated_at(self) -> str:
        with self.fixture.repository.transaction() as connection:
            row = connection.execute(
                "SELECT updated_at FROM asset_sources WHERE id=?",
                (self.fixture.source_id,),
            ).fetchone()
        assert row is not None
        return str(row["updated_at"])

    def _active_generation_id(self) -> str:
        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT id FROM image_projection_generations
                    WHERE is_active=1 ORDER BY id"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        return str(rows[0]["id"])

    def _rebuild(self, *, nonce: str) -> dict[str, object]:
        with self.fixture.repository.transaction() as connection:
            high_water = connection.execute(
                "SELECT MAX(position) FROM image_projection_changes"
            ).fetchone()
            alias_set = connection.execute(
                """SELECT manifest.alias_set_sha256
                     FROM image_projection_manifests manifest
                     JOIN image_projection_generations generation
                       ON generation.id=manifest.generation_id
                    WHERE generation.is_active=1"""
            ).fetchone()
        assert high_water is not None and alias_set is not None
        return self.fixture.repository.rebuild_image_projection_generation(
            fixed_high_water=int(high_water[0]),
            expected_alias_set_sha256=str(alias_set["alias_set_sha256"]),
            rebuild_nonce=nonce,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
        )

    def _publish_next_revision(self) -> tuple[str, dict[str, object]]:
        return verified_read.ImageProjectionVerifiedReadTests._publish_second_revision(
            self
        )

    def _insert_alias(self, legacy_id: str) -> None:
        identity = {
            "database_uuid": self.fixture.repository.database_uuid,
            "legacy_id": legacy_id,
            "canonical_asset_id": self.fixture.asset_id,
            "library_root_id": self.fixture.root_id,
            "source_id": self.fixture.source_id,
            "alias_scope": "legacy-image-index/v1",
        }
        with self.fixture.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO legacy_image_aliases(
                       database_uuid,legacy_id,canonical_asset_id,library_root_id,
                       source_id,alias_scope,alias_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    *identity.values(),
                    hashlib.sha256(
                        json.dumps(
                            identity,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                    schema_guards.NOW,
                ),
            )

    def _insert_physical_copy(self, *, source_id: str, new_id: str) -> None:
        columns = ",".join(IMAGE_INDEX_SQL_COLUMNS)
        placeholders = ",".join("?" for _ in IMAGE_INDEX_SQL_COLUMNS)
        with self.fixture.repository.transaction(immediate=True) as connection:
            source = connection.execute(
                f"SELECT {columns} FROM image_index WHERE id=?",
                (source_id,),
            ).fetchone()
            assert source is not None
            row = dict(source)
            row.update(
                {
                    "id": new_id,
                    "sha256": hashlib.sha256(new_id.encode("utf-8")).hexdigest(),
                    "filename": f"{new_id}.jpg",
                    "relative_path": f"legacy/{new_id}.jpg",
                }
            )
            connection.execute(
                f"INSERT INTO image_index({columns}) VALUES({placeholders})",
                tuple(row[column] for column in IMAGE_INDEX_SQL_COLUMNS),
            )

    def test_remove_is_two_transactions_and_preserves_canonical_history(self) -> None:
        with self.fixture.repository.transaction() as connection:
            canonical_before = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_analysis_results),
                     (SELECT COUNT(*) FROM image_analysis_heads),
                     (SELECT COUNT(*) FROM image_publish_receipts)"""
            ).fetchone()
        assert canonical_before is not None

        marked = self._mark()

        self.assertIs(marked["replayed"], False)
        self.assertEqual(marked["availability"], "missing")
        change = marked["change"]
        assert isinstance(change, dict)
        self.assertEqual(change["operation"], "remove")
        self.assertIsNone(change["source_binding_sha256"])
        self.assertEqual(self._physical_ids(), sorted(
            [self.fixture.asset_id, self.second_asset_id]
        ))
        pending = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert pending is not None
        self.assertEqual(pending["source"]["availability"], "missing")
        self.assertEqual(pending["projection"]["status"], "unavailable")
        self.assertEqual(
            pending["projection"]["reason_code"],
            "image_projection_removal_pending",
        )
        self.assertEqual(
            pending["projection"]["change_position"],
            marked["change_position"],
        )

        projected = self._project(int(marked["change_position"]))

        receipt = projected["projection_receipt"]
        assert isinstance(receipt, dict)
        self.assertEqual(receipt["outcome"], "removed")
        self.assertIsNone(receipt["source_binding_sha256"])
        self.assertIsNone(receipt["projected_row_sha256"])
        successor_id = str(projected["generation_id"])
        self.assertEqual(self._physical_ids(), [self.second_asset_id])
        with self.fixture.repository.transaction() as connection:
            canonical_after = connection.execute(
                """SELECT
                     (SELECT COUNT(*) FROM image_analysis_results),
                     (SELECT COUNT(*) FROM image_analysis_heads),
                     (SELECT COUNT(*) FROM image_publish_receipts)"""
            ).fetchone()
            rows = connection.execute(
                """SELECT asset_id FROM image_projection_rows
                    WHERE generation_id=? ORDER BY asset_id""",
                (successor_id,),
            ).fetchall()
        assert canonical_after is not None
        self.assertEqual(tuple(canonical_after), tuple(canonical_before))
        self.assertEqual([str(row["asset_id"]) for row in rows], [
            self.second_asset_id
        ])
        removed = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert removed is not None
        self.assertEqual(removed["analysis_binding"], self.fixture.analysis_binding)
        self.assertEqual(removed["source"]["availability"], "missing")
        self.assertEqual(removed["projection"]["status"], "unavailable")
        self.assertEqual(removed["projection"]["outcome"], "removed")
        self.assertEqual(
            removed["projection"]["generation_id"], successor_id
        )
        self.assertEqual(
            removed["projection"]["processing_generation_id"], successor_id
        )
        self.assertEqual(
            removed["projection"]["change_position"],
            marked["change_position"],
        )
        other = self.fixture.repository.get_current_image_analysis(
            self.second_asset_id
        )
        assert other is not None
        self.assertEqual(other["projection"]["status"], "current")
        self.assertEqual(other["projection"]["generation_id"], successor_id)

    def test_mark_and_project_replay_one_removal_for_100_rounds(self) -> None:
        marked = self._mark()
        projected = self._project(int(marked["change_position"]))
        snapshot = self._snapshot()

        for _round in range(100):
            replay_mark = self._mark()
            replay_project = self._project(int(marked["change_position"]))
            self.assertIs(replay_mark["replayed"], True)
            self.assertEqual(replay_mark["change"], marked["change"])
            self.assertIs(replay_project["replayed"], True)
            self.assertEqual(
                replay_project["generation_id"], projected["generation_id"]
            )
            self.assertEqual(
                replay_project["projection_receipt"],
                projected["projection_receipt"],
            )
            self.assertEqual(self._snapshot(), snapshot)

    def test_stale_head_and_source_are_zero_write(self) -> None:
        baseline = self._snapshot()
        stale_binding = copy.deepcopy(self.fixture.analysis_binding)
        stale_binding["revision"] = int(stale_binding["revision"]) + 1
        cases = (
            (
                {"asset_id": self.second_asset_id},
                "image_source_removal_asset_changed",
            ),
            (
                {"analysis_binding": stale_binding},
                "image_source_removal_head_changed",
            ),
            (
                {"source_updated_at": "stale-source-updated-at"},
                "image_source_removal_source_changed",
            ),
        )
        for kwargs, reason in cases:
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(
                    ImageAnalysisPersistenceError,
                    reason,
                ):
                    self._mark(**kwargs)
                self.assertEqual(self._snapshot(), baseline)

    def test_tx_a_faults_roll_back_source_and_remove_change_together(self) -> None:
        baseline = self._snapshot()
        for target in ("source", "change"):
            for phase in ("before", "after"):
                with self.subTest(target=target, phase=phase):
                    with _faulting_transactions(
                        self.fixture.repository,
                        target=target,
                        phase=phase,
                    ) as hits:
                        with self.assertRaisesRegex(
                            _InjectedRemovalFault,
                            rf"{target}:{phase}",
                        ):
                            self._mark()
                    self.assertEqual(hits(), 1)
                    self.assertEqual(self._snapshot(), baseline)

    def test_tx_b_faults_keep_tx_a_and_previous_projection(self) -> None:
        marked = self._mark()
        baseline = self._snapshot()
        previous_generation_id = self._active_generation_id()
        for target in (
            "generation",
            "receipt",
            "manifest",
            "physical_clear",
            "materialize",
            "read_manifest",
            "deactivate",
            "activate",
        ):
            for phase in ("before", "after"):
                with self.subTest(target=target, phase=phase):
                    with _faulting_transactions(
                        self.fixture.repository,
                        target=target,
                        phase=phase,
                    ) as hits:
                        with self.assertRaisesRegex(
                            _InjectedRemovalFault,
                            rf"{target}:{phase}",
                        ):
                            self._project(int(marked["change_position"]))
                    self.assertEqual(hits(), 1)
                    self.assertEqual(self._snapshot(), baseline)
                    self.assertEqual(
                        self._active_generation_id(),
                        previous_generation_id,
                    )
                    current = self.fixture.repository.get_current_image_analysis(
                        self.fixture.asset_id
                    )
                    assert current is not None
                    self.assertEqual(
                        current["projection"]["reason_code"],
                        "image_projection_removal_pending",
                    )
                    self.assertEqual(
                        current["source"]["availability"],
                        "missing",
                    )

    def test_available_toggle_cannot_replay_or_resurrect_removed_asset(self) -> None:
        marked = self._mark()
        projected = self._project(int(marked["change_position"]))
        removal_generation_id = str(projected["generation_id"])

        self.fixture.repository.mark_source_availability(
            self.fixture.source_id,
            "available",
        )
        changed_updated_at = self._source_updated_at()
        baseline = self._snapshot()
        for expected_updated_at in (
            self.expected_source_updated_at,
            changed_updated_at,
        ):
            with self.subTest(expected_updated_at=expected_updated_at):
                with self.assertRaisesRegex(
                    ImageAnalysisPersistenceError,
                    "image_source_removal_source_changed",
                ):
                    self.fixture.repository.mark_image_source_unavailable(
                        source_id=self.fixture.source_id,
                        expected_asset_id=self.fixture.asset_id,
                        expected_analysis_binding=self.fixture.analysis_binding,
                        expected_source_updated_at=expected_updated_at,
                        availability="missing",
                    )
                self.assertEqual(self._snapshot(), baseline)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_analysis_binding_corrupt",
        ):
            self.fixture.repository.project_image_analysis_change(
                job_id=self.fixture.job_id,
                runtime_generation=schema_guards.RUNTIME_GENERATION,
                expected_attempt=1,
            )
        self.assertEqual(self._snapshot(), baseline)

        rebuilt = self._rebuild(nonce="removed-available-does-not-readmit")
        self.assertEqual(self._physical_ids(), [self.second_asset_id])
        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert current is not None
        self.assertEqual(current["source"]["availability"], "available")
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertEqual(current["projection"]["outcome"], "removed")
        self.assertEqual(
            current["projection"]["processing_generation_id"],
            removal_generation_id,
        )
        self.assertEqual(
            current["projection"]["generation_id"],
            rebuilt["generation_id"],
        )

        replayed_removal = self._project(int(marked["change_position"]))
        self.assertIs(replayed_removal["replayed"], True)
        self.assertEqual(
            replayed_removal["generation_id"],
            removal_generation_id,
        )
        self.assertEqual(
            replayed_removal["previous_generation_id"],
            projected["previous_generation_id"],
        )
        self.assertIs(replayed_removal["is_active"], False)
        self.assertEqual(self._active_generation_id(), rebuilt["generation_id"])

    def test_new_revision_explicit_upsert_is_the_only_readmission(self) -> None:
        marked = self._mark()
        self._project(int(marked["change_position"]))
        self.fixture.repository.mark_source_availability(
            self.fixture.source_id,
            "available",
        )

        job_id, new_binding = self._publish_next_revision()
        projected = self.fixture.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )

        self.assertEqual(
            self._physical_ids(),
            sorted([self.fixture.asset_id, self.second_asset_id]),
        )
        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert current is not None
        self.assertEqual(current["analysis_binding"], new_binding)
        self.assertEqual(current["projection"]["status"], "current")
        self.assertEqual(
            current["projection"]["generation_id"],
            projected["generation_id"],
        )
        with self.fixture.repository.transaction() as connection:
            latest = connection.execute(
                """SELECT operation,position FROM image_projection_changes
                    WHERE asset_id=? ORDER BY position DESC LIMIT 1""",
                (self.fixture.asset_id,),
            ).fetchone()
        assert latest is not None
        self.assertEqual(latest["operation"], "upsert")

    def test_registered_alias_retirement_waits_for_unmanaged_row_cleanup(
        self,
    ) -> None:
        legacy_id = "removal-registered-alias"
        unmanaged_id = "removal-unmanaged-legacy"
        self._insert_alias(legacy_id)
        job_id, current_binding = self._publish_next_revision()
        self.fixture.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=schema_guards.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self._insert_physical_copy(
            source_id=self.fixture.asset_id,
            new_id=legacy_id,
        )
        self._insert_physical_copy(
            source_id=self.fixture.asset_id,
            new_id=unmanaged_id,
        )
        marked = self.fixture.repository.mark_image_source_unavailable(
            source_id=self.fixture.source_id,
            expected_asset_id=self.fixture.asset_id,
            expected_analysis_binding=current_binding,
            expected_source_updated_at=self._source_updated_at(),
            availability="revoked",
        )

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_source_removal_read_cutover_blocked",
        ):
            self._project(int(marked["change_position"]))
        self.assertIn(unmanaged_id, self._physical_ids())
        with self.fixture.repository.transaction(immediate=True) as connection:
            removed = connection.execute(
                "DELETE FROM image_index WHERE id=?",
                (unmanaged_id,),
            )
        self.assertEqual(removed.rowcount, 1)

        projected = self._project(int(marked["change_position"]))

        self.assertEqual(
            self._physical_ids(),
            [self.second_asset_id],
        )
        with self.fixture.repository.transaction() as connection:
            ledger = connection.execute(
                """SELECT canonical_asset_id FROM legacy_image_aliases
                    WHERE legacy_id=?""",
                (legacy_id,),
            ).fetchone()
            snapshot = connection.execute(
                """SELECT canonical_asset_id FROM image_projection_aliases
                    WHERE generation_id=? AND legacy_id=?""",
                (projected["generation_id"], legacy_id),
            ).fetchone()
        assert ledger is not None and snapshot is not None
        self.assertEqual(ledger["canonical_asset_id"], self.fixture.asset_id)
        self.assertEqual(snapshot["canonical_asset_id"], self.fixture.asset_id)

    def test_generic_image_terminal_transition_uses_remove_command(self) -> None:
        with self.fixture.repository.transaction() as connection:
            before = connection.execute(
                """SELECT COUNT(*) FROM image_projection_changes
                    WHERE asset_id=?""",
                (self.fixture.asset_id,),
            ).fetchone()[0]

        self.fixture.repository.mark_source_availability(
            self.fixture.source_id,
            "changed",
        )
        self.fixture.repository.mark_source_availability(
            self.fixture.source_id,
            "changed",
        )

        with self.fixture.repository.transaction() as connection:
            rows = connection.execute(
                """SELECT operation,source_binding_sha256
                     FROM image_projection_changes WHERE asset_id=?
                     ORDER BY position""",
                (self.fixture.asset_id,),
            ).fetchall()
        self.assertEqual(len(rows), before + 1)
        self.assertEqual(rows[-1]["operation"], "remove")
        self.assertIsNone(rows[-1]["source_binding_sha256"])
        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )
        assert current is not None
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_projection_removal_pending",
        )

    def test_same_path_database_replacement_is_zero_write(self) -> None:
        baseline = self._snapshot()
        self.fixture.repository.close()
        replacement = self.fixture.db_path.with_suffix(".removal-replacement")
        with (
            closing(sqlite3.connect(self.fixture.db_path)) as source,
            closing(sqlite3.connect(replacement)) as target,
        ):
            source.backup(target)
        os.replace(replacement, self.fixture.db_path)

        with self.assertRaisesRegex(
            ImageAnalysisPersistenceError,
            "image_database_scope_changed",
        ):
            self._mark()
        self.assertEqual(self._snapshot(), baseline)

    def test_removed_reader_rejects_reintroduced_physical_row(self) -> None:
        marked = self._mark()
        self._project(int(marked["change_position"]))
        self._insert_physical_copy(
            source_id=self.second_asset_id,
            new_id=self.fixture.asset_id,
        )

        current = self.fixture.repository.get_current_image_analysis(
            self.fixture.asset_id
        )

        assert current is not None
        self.assertEqual(current["projection"]["status"], "unavailable")
        self.assertIsNone(current["projection"]["outcome"])
        self.assertEqual(
            current["projection"]["reason_code"],
            "image_analysis_projection_corrupt",
        )


if __name__ == "__main__":
    unittest.main()
