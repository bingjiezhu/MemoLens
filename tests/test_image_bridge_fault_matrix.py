from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

from PIL import Image

from core.image_analysis_contract import (
    require_valid_projection_manifest,
    require_valid_projection_receipt,
)
from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.image_projection_renderer import require_valid_projected_image_index_row
from core.media_db import MediaRepository, content_sha256
from tests import test_image_analysis_persistence as persistence_fixture
from tests import test_image_projection_schema_guards as projection_fixture


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_STRESS_ROUNDS = 100


class _InjectedBoundaryFault(RuntimeError):
    pass


def _normalized_sql(statement: str) -> str:
    return " ".join(statement.split()).upper()


def _publish_boundary(statement: str) -> str | None:
    normalized = _normalized_sql(statement)
    if normalized.startswith("UPDATE ANALYSIS_RUNS SET STATUS='SUCCEEDED'"):
        return "run_status"
    if normalized.startswith("INSERT INTO IMAGE_ANALYSIS_RESULTS"):
        return "result"
    if normalized.startswith("INSERT INTO IMAGE_ANALYSIS_ARTIFACTS"):
        return "artifact"
    if normalized.startswith("INSERT INTO IMAGE_ANALYSIS_HEADS"):
        return "head_insert"
    if normalized.startswith("UPDATE IMAGE_ANALYSIS_HEADS SET ANALYSIS_RUN_ID="):
        return "head_update"
    if normalized.startswith("INSERT INTO IMAGE_PROJECTION_CHANGES"):
        return "change"
    if normalized.startswith("INSERT INTO IMAGE_PUBLISH_RECEIPTS"):
        return "receipt"
    if normalized.startswith(
        "UPDATE IMAGE_ANALYSIS_ATTEMPT_STATES SET HEARTBEAT_SEQUENCE="
    ):
        return "attempt_checkpoint"
    if normalized.startswith(
        "UPDATE MEDIA_JOBS SET STATUS='RUNNING',STAGE='SHADOW_PROJECTION'"
    ):
        return "job_checkpoint"
    return None


def _projection_boundary(statement: str) -> str | None:
    normalized = _normalized_sql(statement)
    if normalized.startswith("INSERT INTO IMAGE_PROJECTION_GENERATIONS"):
        # The V14 AFTER INSERT trigger snapshots image_projection_aliases inside
        # this same SQLite statement.  The wrapper therefore exercises the real
        # before/after boundary of both the generation and its alias snapshot.
        return "generation_alias_snapshot"
    if normalized.startswith("INSERT INTO IMAGE_PROJECTION_ROWS"):
        return "projection_row"
    if normalized.startswith("INSERT INTO IMAGE_INDEX"):
        return "physical_row"
    if normalized.startswith("INSERT INTO IMAGE_PROJECTION_RECEIPTS"):
        return "projection_receipt"
    if normalized.startswith("INSERT INTO IMAGE_PROJECTION_MANIFESTS"):
        return "manifest_insert"
    if normalized.startswith(
        "UPDATE IMAGE_PROJECTION_GENERATIONS SET STATUS='COMPLETE'"
    ):
        return "generation_complete"
    if normalized.startswith(
        "UPDATE IMAGE_PROJECTION_GENERATIONS SET IS_ACTIVE=0"
    ):
        return "active_pointer_clear"
    if normalized.startswith(
        "UPDATE IMAGE_PROJECTION_GENERATIONS SET IS_ACTIVE=1"
    ):
        return "active_pointer_set"
    return None


@dataclass
class _FaultPlan:
    target: str
    phase: str
    matcher: Callable[[str], str | None]
    hits: int = 0

    def before(self, statement: str) -> bool:
        if self.hits != 0 or self.matcher(statement) != self.target:
            return False
        if self.phase == "before":
            self.hits += 1
            raise _InjectedBoundaryFault(f"{self.target}:before")
        return True

    def after(self, matched: bool) -> None:
        if matched and self.hits == 0 and self.phase == "after":
            self.hits += 1
            raise _InjectedBoundaryFault(f"{self.target}:after")


class _FaultingConnection:
    def __init__(self, connection: sqlite3.Connection, plan: _FaultPlan) -> None:
        self._connection = connection
        self._plan = plan

    def execute(
        self,
        statement: str,
        parameters: object = (),
    ) -> sqlite3.Cursor:
        matched = self._plan.before(statement)
        cursor = self._connection.execute(statement, parameters)
        self._plan.after(matched)
        return cursor

    def executemany(
        self,
        statement: str,
        parameters: Sequence[object],
    ) -> sqlite3.Cursor:
        matched = self._plan.before(statement)
        cursor = self._connection.executemany(statement, parameters)
        self._plan.after(matched)
        return cursor

    def __getattr__(self, name: str) -> object:
        return getattr(self._connection, name)


@contextmanager
def _faulting_transactions(
    repository: MediaRepository,
    plan: _FaultPlan,
) -> Iterator[None]:
    real_transaction = repository.transaction

    @contextmanager
    def faulting_transaction(*, immediate: bool = False) -> Iterator[object]:
        with real_transaction(immediate=immediate) as connection:
            yield _FaultingConnection(connection, plan)

    with patch.object(repository, "transaction", faulting_transaction):
        yield


_PUBLISH_LEDGER_TABLES = (
    "analysis_runs",
    "media_jobs",
    "image_analysis_job_bindings",
    "image_analysis_attempt_authorities",
    "image_analysis_attempt_states",
    "image_analysis_results",
    "image_analysis_artifacts",
    "image_analysis_heads",
    "asset_analysis_heads",
    "image_projection_changes",
    "image_publish_receipts",
)

_PROJECTION_LEDGER_TABLES = (
    "media_jobs",
    "image_analysis_attempt_states",
    "legacy_image_aliases",
    "image_index",
    "image_projection_generations",
    "image_projection_aliases",
    "image_projection_rows",
    "image_projection_manifests",
    "image_projection_receipts",
)


def _table_snapshot(
    db_path: Path,
    tables: Sequence[str],
) -> dict[str, list[tuple[object, ...]]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"  # noqa: S608 - fixed names
            ).fetchall()
            for table in tables
        }


def _enqueue_successor(
    fixture: persistence_fixture.ImageAnalysisPersistenceTests,
    *,
    key: str,
    expected_head: Mapping[str, object],
) -> tuple[str, dict[str, object]]:
    job = fixture.repository.enqueue_image_analysis(
        runtime_generation=persistence_fixture.RUNTIME_GENERATION,
        database_file_identity=fixture.database_identity,
        asset_id=fixture.asset_id,
        source_id=fixture.source_id,
        analysis_profile=persistence_fixture.PROFILE,
        expected_head=expected_head,
        enqueue_scope="media-import/image-analysis",
        idempotency_key=key,
    )
    job_id = str(job["id"])
    return job_id, fixture._sealed_result(job_id)


def _publication_head(result: Mapping[str, object]) -> dict[str, object]:
    return {
        "analysis_run_id": result["analysis_run_id"],
        "revision": result["revision"],
        "content_sha256": result["content_sha256"],
    }


class ImagePublishFaultMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = persistence_fixture.ImageAnalysisPersistenceTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _assert_fault_rolls_back(
        self,
        *,
        job_id: str,
        result: Mapping[str, object],
        target: str,
        phase: str,
    ) -> None:
        before = _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES)
        plan = _FaultPlan(target=target, phase=phase, matcher=_publish_boundary)
        with _faulting_transactions(self.fixture.repository, plan):
            with self.assertRaisesRegex(
                _InjectedBoundaryFault,
                rf"{target}:{phase}",
            ):
                self.fixture.repository.publish_image_analysis(
                    job_id=job_id,
                    runtime_generation=persistence_fixture.RUNTIME_GENERATION,
                    expected_attempt=1,
                    result=result,
                    artifacts=self.fixture._artifacts(),
                )
        self.assertEqual(plan.hits, 1)
        self.assertEqual(
            _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES),
            before,
        )

    def test_every_initial_publish_write_boundary_is_all_or_none(self) -> None:
        job = self.fixture._enqueue(key="fault-matrix-initial")
        job_id = str(job["id"])
        result = self.fixture._sealed_result(job_id)
        targets = (
            "run_status",
            "result",
            "artifact",
            "head_insert",
            "change",
            "receipt",
            "attempt_checkpoint",
            "job_checkpoint",
        )
        for target in targets:
            for phase in ("before", "after"):
                with self.subTest(target=target, phase=phase):
                    self._assert_fault_rolls_back(
                        job_id=job_id,
                        result=result,
                        target=target,
                        phase=phase,
                    )

    def test_head_update_cas_boundary_is_all_or_none(self) -> None:
        first = self.fixture._enqueue(key="fault-matrix-head-base")
        first_id = str(first["id"])
        first_result = self.fixture._sealed_result(first_id)
        self.fixture.repository.publish_image_analysis(
            job_id=first_id,
            runtime_generation=persistence_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=first_result,
            artifacts=self.fixture._artifacts(),
        )
        second_id, second_result = _enqueue_successor(
            self.fixture,
            key="fault-matrix-head-successor",
            expected_head=_publication_head(first_result),
        )
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                self._assert_fault_rolls_back(
                    job_id=second_id,
                    result=second_result,
                    target="head_update",
                    phase=phase,
                )


class ImageProjectionFaultMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = projection_fixture.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        self.repository = self.fixture.repository
        first = self.repository.project_image_analysis_change(
            job_id=self.fixture.job_id,
            runtime_generation=projection_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        self.old_generation_id = str(first["generation_id"])
        self._insert_alias("fault-matrix-legacy-alias")
        self.second_job_id = self._publish_successor()
        self.baseline = _table_snapshot(
            self.fixture.db_path,
            _PROJECTION_LEDGER_TABLES,
        )

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _insert_alias(self, legacy_id: str) -> None:
        identity = {
            "database_uuid": self.repository.database_uuid,
            "legacy_id": legacy_id,
            "canonical_asset_id": self.fixture.asset_id,
            "library_root_id": self.fixture.root_id,
            "source_id": self.fixture.source_id,
            "alias_scope": "legacy-image-index/v1",
        }
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO legacy_image_aliases(
                       database_uuid,legacy_id,canonical_asset_id,library_root_id,
                       source_id,alias_scope,alias_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    *identity.values(),
                    content_sha256(identity),
                    projection_fixture.NOW,
                ),
            )

    def _publish_successor(self) -> str:
        database_stat = self.fixture.db_path.stat(follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=projection_fixture.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=projection_fixture.PROFILE,
            expected_head=self.fixture.analysis_binding,
            enqueue_scope="schema-guard/image-analysis",
            idempotency_key="fault-matrix-projection-successor",
        )
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        original_binding = self.fixture.binding
        try:
            self.fixture.binding = binding
            result = self.fixture._sealed_result()
        finally:
            self.fixture.binding = original_binding
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=projection_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )
        return job_id

    def _assert_old_generation_is_the_only_readable_generation(self) -> None:
        with self.repository.transaction() as connection:
            generation = connection.execute(
                """SELECT status,is_active FROM image_projection_generations
                    WHERE id=?""",
                (self.old_generation_id,),
            ).fetchone()
            building_count = connection.execute(
                """SELECT COUNT(*) FROM image_projection_generations
                    WHERE status='building'"""
            ).fetchone()[0]
            active_count = connection.execute(
                """SELECT COUNT(*) FROM image_projection_generations
                    WHERE status='complete' AND is_active=1"""
            ).fetchone()[0]
            row = connection.execute(
                """SELECT row_json FROM image_projection_rows
                    WHERE generation_id=? AND asset_id=?""",
                (self.old_generation_id, self.fixture.asset_id),
            ).fetchone()
            manifest = connection.execute(
                """SELECT manifest_json FROM image_projection_manifests
                    WHERE generation_id=?""",
                (self.old_generation_id,),
            ).fetchone()
            receipt = connection.execute(
                """SELECT receipt_json FROM image_projection_receipts
                    WHERE generation_id=?""",
                (self.old_generation_id,),
            ).fetchone()
        self.assertEqual(tuple(generation), ("complete", 1))
        self.assertEqual((building_count, active_count), (0, 1))
        assert row is not None and manifest is not None and receipt is not None
        row_document = json.loads(str(row["row_json"]))
        manifest_document = json.loads(str(manifest["manifest_json"]))
        receipt_document = json.loads(str(receipt["receipt_json"]))
        require_valid_projected_image_index_row(row_document)
        require_valid_projection_manifest(manifest_document)
        require_valid_projection_receipt(receipt_document)

        current = self.repository.get_current_image_analysis(self.fixture.asset_id)
        assert current is not None
        # The canonical head already points at revision two.  A failed rebuild
        # must not make the older active generation look current for that head.
        self.assertEqual(current["projection"]["status"], "pending")

    def _assert_sql_boundary_rolls_back(self, target: str, phase: str) -> None:
        plan = _FaultPlan(target=target, phase=phase, matcher=_projection_boundary)
        with _faulting_transactions(self.repository, plan):
            with self.assertRaisesRegex(
                _InjectedBoundaryFault,
                rf"{target}:{phase}",
            ):
                self.repository.project_image_analysis_change(
                    job_id=self.second_job_id,
                    runtime_generation=projection_fixture.RUNTIME_GENERATION,
                    expected_attempt=1,
                )
        self.assertEqual(plan.hits, 1)
        self.assertEqual(
            _table_snapshot(self.fixture.db_path, _PROJECTION_LEDGER_TABLES),
            self.baseline,
        )
        self._assert_old_generation_is_the_only_readable_generation()

    def test_each_projection_sql_boundary_preserves_previous_generation(self) -> None:
        targets = (
            "generation_alias_snapshot",
            "projection_row",
            "physical_row",
            "projection_receipt",
            "manifest_insert",
            "generation_complete",
            "active_pointer_clear",
            "active_pointer_set",
        )
        for target in targets:
            for phase in ("before", "after"):
                with self.subTest(target=target, phase=phase):
                    self._assert_sql_boundary_rolls_back(target, phase)

    def test_manifest_build_before_and_after_faults_preserve_previous_generation(
        self,
    ) -> None:
        from core import image_analysis_persistence as persistence

        real_build = persistence.build_projection_manifest
        for phase in ("before", "after"):
            calls = 0

            def faulting_build(*args: object, **kwargs: object) -> object:
                nonlocal calls
                calls += 1
                if phase == "before":
                    raise _InjectedBoundaryFault("manifest_build:before")
                real_build(*args, **kwargs)
                raise _InjectedBoundaryFault("manifest_build:after")

            with self.subTest(phase=phase):
                with patch.object(
                    persistence,
                    "build_projection_manifest",
                    side_effect=faulting_build,
                ):
                    with self.assertRaisesRegex(
                        _InjectedBoundaryFault,
                        rf"manifest_build:{phase}",
                    ):
                        self.repository.project_image_analysis_change(
                            job_id=self.second_job_id,
                            runtime_generation=projection_fixture.RUNTIME_GENERATION,
                            expected_attempt=1,
                        )
                self.assertEqual(calls, 1)
                self.assertEqual(
                    _table_snapshot(
                        self.fixture.db_path,
                        _PROJECTION_LEDGER_TABLES,
                    ),
                    self.baseline,
                )
                self._assert_old_generation_is_the_only_readable_generation()


class ImageBridgeReplayStressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = persistence_fixture.ImageAnalysisPersistenceTests(
            methodName="runTest"
        )
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _enqueue_at_head(
        self,
        *,
        key: str,
        expected_head: Mapping[str, object] | None,
    ) -> tuple[str, dict[str, object]]:
        job = self.fixture.repository.enqueue_image_analysis(
            runtime_generation=persistence_fixture.RUNTIME_GENERATION,
            database_file_identity=self.fixture.database_identity,
            asset_id=self.fixture.asset_id,
            source_id=self.fixture.source_id,
            analysis_profile=persistence_fixture.PROFILE,
            expected_head=expected_head,
            enqueue_scope="media-import/image-analysis",
            idempotency_key=key,
        )
        job_id = str(job["id"])
        return job_id, self.fixture._sealed_result(job_id)

    def _publish(self, job_id: str, result: Mapping[str, object]) -> None:
        self.fixture.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=persistence_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=self.fixture._artifacts(),
        )

    def test_four_replay_and_ordering_classes_each_run_one_hundred_rounds(self) -> None:
        rounds = {
            "same_request_replay": 0,
            "idempotency_rebind": 0,
            "stale_head_cas": 0,
            "out_of_order_change": 0,
        }

        stale_job = self.fixture._enqueue(key="stress-stale-candidate")
        stale_job_id = str(stale_job["id"])
        stale_result = self.fixture._sealed_result(stale_job_id)
        initial_authority = self.fixture.repository.get_image_analysis_job_binding(
            stale_job_id
        )["attempt_authority"]
        for _index in range(_STRESS_ROUNDS):
            replay = self.fixture._enqueue(key="stress-stale-candidate")
            self.assertTrue(replay["reused"])
            self.assertEqual(str(replay["id"]), stale_job_id)
            rounds["same_request_replay"] += 1
        for index in range(_STRESS_ROUNDS):
            rebound_profile = {
                **persistence_fixture.PROFILE,
                "profile_version": f"rebound-{index}",
            }
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "image_analysis_idempotency_conflict",
            ):
                self.fixture._enqueue(
                    key="stress-stale-candidate",
                    profile=rebound_profile,
                )
            rounds["idempotency_rebind"] += 1
        current_binding = self.fixture.repository.get_image_analysis_job_binding(
            stale_job_id
        )
        assert current_binding is not None
        self.assertEqual(current_binding["attempt_authority"], initial_authority)

        winner_id, winner_result = self._enqueue_at_head(
            key="stress-head-winner",
            expected_head=None,
        )
        self._publish(winner_id, winner_result)
        stale_baseline = _table_snapshot(
            self.fixture.db_path,
            _PUBLISH_LEDGER_TABLES,
        )
        for _index in range(_STRESS_ROUNDS):
            with self.assertRaisesRegex(
                ImageAnalysisPersistenceError,
                "image_analysis_head_conflict",
            ):
                self._publish(stale_job_id, stale_result)
            rounds["stale_head_cas"] += 1
        self.assertEqual(
            _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES),
            stale_baseline,
        )

        # Use the projection fixture for the ordering matrix because it carries
        # a real visual image-search vector.  A metadata-only result is
        # intentionally blocked by the production projector and cannot prove
        # the superseded/applied ordering path.
        ordering = projection_fixture.ImageProjectionSchemaGuardTests(
            methodName="runTest"
        )
        ordering.setUp()
        try:
            published_jobs: list[tuple[str, int]] = [
                (ordering.job_id, int(ordering.analysis_binding["revision"]))
            ]
            head: Mapping[str, object] = ordering.analysis_binding
            database_stat = ordering.db_path.stat(follow_symlinks=False)
            for index in range(_STRESS_ROUNDS):
                job = ordering.repository.enqueue_image_analysis(
                    runtime_generation=projection_fixture.RUNTIME_GENERATION,
                    database_file_identity=(
                        database_stat.st_dev,
                        database_stat.st_ino,
                    ),
                    asset_id=ordering.asset_id,
                    source_id=ordering.source_id,
                    analysis_profile=projection_fixture.PROFILE,
                    expected_head=head,
                    enqueue_scope="schema-guard/image-analysis",
                    idempotency_key=f"stress-ordered-successor-{index}",
                )
                job_id = str(job["id"])
                binding = ordering.repository.get_image_analysis_job_binding(job_id)
                assert binding is not None
                original_binding = ordering.binding
                try:
                    ordering.binding = binding
                    result = ordering._sealed_result()
                finally:
                    ordering.binding = original_binding
                ordering.repository.publish_image_analysis(
                    job_id=job_id,
                    runtime_generation=projection_fixture.RUNTIME_GENERATION,
                    expected_attempt=1,
                    result=result,
                    artifacts=ordering._artifacts(),
                )
                head = _publication_head(result)
                published_jobs.append((job_id, int(result["revision"])))

            newest_job_id = published_jobs[-1][0]
            projected = ordering.repository.project_image_analysis_change(
                job_id=newest_job_id,
                runtime_generation=projection_fixture.RUNTIME_GENERATION,
                expected_attempt=1,
            )
            generation_id = str(projected["generation_id"])
            older_jobs = published_jobs[:-1]
            self.assertEqual(len(older_jobs), _STRESS_ROUNDS)
            for job_id, revision in older_jobs:
                replay = ordering.repository.project_image_analysis_change(
                    job_id=job_id,
                    runtime_generation=projection_fixture.RUNTIME_GENERATION,
                    expected_attempt=1,
                )
                receipt = replay["projection_receipt"]
                self.assertTrue(replay["replayed"], (revision, replay))
                self.assertEqual(str(replay["generation_id"]), generation_id)
                self.assertEqual(receipt["outcome"], "superseded")
                self.assertEqual(
                    receipt["analysis_binding"]["revision"],
                    revision,
                )
                rounds["out_of_order_change"] += 1
        finally:
            ordering.tearDown()

        self.assertEqual(
            rounds,
            {
                "same_request_replay": _STRESS_ROUNDS,
                "idempotency_rebind": _STRESS_ROUNDS,
                "stale_head_cas": _STRESS_ROUNDS,
                "out_of_order_change": _STRESS_ROUNDS,
            },
        )


_RESTART_SCRIPT = r"""
import json
from pathlib import Path
import sys

from core.image_analysis_persistence import ImageAnalysisPersistenceError
from core.media_db import MediaRepository

repository = MediaRepository(Path(sys.argv[1]))
try:
    repository.validate_image_analysis_job_admission(
        sys.argv[2],
        runtime_generation=sys.argv[3],
        expected_attempt=1,
    )
except ImageAnalysisPersistenceError as exc:
    print(json.dumps({"status": "error", "code": exc.code}, sort_keys=True))
else:
    print(json.dumps({"status": "unexpected_success"}, sort_keys=True))
finally:
    repository.close()
"""


class ImageBridgeRestartReplacementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = persistence_fixture.ImageAnalysisPersistenceTests(
            methodName="runTest"
        )
        self.fixture.setUp()
        job = self.fixture._enqueue(key="restart-replacement")
        self.job_id = str(job["id"])

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _restart_probe(self) -> dict[str, str]:
        completed = subprocess.run(
            (
                sys.executable,
                "-c",
                _RESTART_SCRIPT,
                str(self.fixture.db_path),
                self.job_id,
                persistence_fixture.RUNTIME_GENERATION,
            ),
            cwd=_REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(completed.stdout)

    def _assert_two_cold_restarts_fail_without_ledger_writes(
        self,
        *,
        expected_code: str,
        baseline: Mapping[str, object],
    ) -> None:
        first = self._restart_probe()
        second = self._restart_probe()
        self.assertEqual(
            (first, second),
            (
                {"status": "error", "code": expected_code},
                {"status": "error", "code": expected_code},
            ),
        )
        self.assertEqual(
            _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES),
            baseline,
        )

    def test_same_path_source_replacement_is_stable_across_cold_restarts(self) -> None:
        baseline = _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES)
        self.fixture.repository.close()
        replacement = self.fixture.library / "replacement.png"
        Image.new("RGB", (24, 12), "orange").save(replacement)
        os.replace(replacement, self.fixture.image_path)

        self._assert_two_cold_restarts_fail_without_ledger_writes(
            expected_code="image_source_changed",
            baseline=baseline,
        )

    def test_same_path_database_replacement_is_stable_across_cold_restarts(
        self,
    ) -> None:
        baseline = _table_snapshot(self.fixture.db_path, _PUBLISH_LEDGER_TABLES)
        self.fixture.repository.close()
        replacement = self.fixture.db_path.with_suffix(".replacement")
        with (
            closing(sqlite3.connect(self.fixture.db_path)) as source,
            closing(sqlite3.connect(replacement)) as target,
        ):
            source.backup(target)
        os.replace(replacement, self.fixture.db_path)

        self._assert_two_cold_restarts_fail_without_ledger_writes(
            expected_code="image_database_scope_changed",
            baseline=baseline,
        )


if __name__ == "__main__":
    unittest.main()
