from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline_lowering import (
    TimelineLoweringService,
    TimelineLoweringServiceError,
)
from core.db import ImageIndexRepository
from core.media_db import CanonicalTimelineIntegrityError, MediaRepository
from core.timeline_edit_contract import require_exact_timeline_edit_replay
import test_timeline_lowering_integration as timeline_fixture


class TimelineEditServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-edit-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(
        self,
        *,
        include_unselected_alternative: bool = False,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        return timeline_fixture.TimelineLoweringIntegrationTests._build_project(
            self,
            include_unselected_alternative=include_unselected_alternative,
        )

    def _materialize(
        self,
        project_id: str,
        payload: dict[str, object],
        *,
        key: str,
    ):
        return timeline_fixture.TimelineLoweringIntegrationTests._materialize(
            self,
            project_id,
            payload,
            key=key,
        )

    @staticmethod
    def _edit_payload(
        materialize_payload: dict[str, object],
        head: dict[str, object],
        edit: dict[str, object],
    ) -> dict[str, object]:
        return {
            "expected_blueprint": materialize_payload["expected_blueprint"],
            "expected_coverage": materialize_payload["expected_coverage"],
            "expected_timeline_head": head,
            "edit": edit,
        }

    def test_image_duration_edit_persists_exact_edit_and_replays_receipt(self) -> None:
        project_id, _blueprint, _coverage, materialize = self._build_project()
        self._materialize(project_id, materialize, key="timeline-edit-base")
        before = self.timelines.read(project_id)
        head = before["head"]
        timeline = before["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        first_clip = timeline["tracks"][0]["clips"][0]
        edit = {
            "op": "set_clip_duration",
            "clip_id": first_clip["clip_id"],
            "duration_ms": 1_250,
        }
        payload = self._edit_payload(materialize, head, edit)

        first = self.timelines.apply_edit(
            project_id,
            payload,
            idempotency_key="timeline-edit-duration",
            expected_database_uuid=self.repository.database_uuid,
        )
        replay = self.timelines.apply_edit(
            project_id,
            payload,
            idempotency_key="timeline-edit-duration",
            expected_database_uuid=self.repository.database_uuid,
        )
        reopened = self.timelines.read(project_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(first.response["command_type"], "timeline.apply_edit")
        self.assertEqual(
            first.response["result"],
            {
                "kind": "revision_created",
                "result_head": reopened["head"],
                "edit": edit,
            },
        )
        self.assertEqual(reopened["head"]["revision"], 2)
        self.assertEqual(
            reopened["timeline"]["tracks"][0]["clips"][0]["end_ms"],
            1_250,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            operation_result = connection.execute(
                """SELECT result_json FROM canonical_timeline_operations
                    WHERE project_id=? AND sequence=2""",
                (project_id,),
            ).fetchone()[0]
        self.assertIn('"edit":{"clip_id":', operation_result)
        self.assertNotIn(str(self.library), operation_result)

    def test_replace_resolves_source_from_same_beat_coverage_assignment(self) -> None:
        project_id, _blueprint, coverage, materialize = self._build_project(
            include_unselected_alternative=True
        )
        self._materialize(project_id, materialize, key="timeline-replace-base")
        before = self.timelines.read(project_id)
        head = before["head"]
        timeline = before["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        target = timeline["tracks"][0]["clips"][0]
        plan = coverage["plan"]
        beat = next(row for row in plan["beats"] if row["beat_id"] == target["beat_id"])
        alternative = beat["alternatives"][0]
        edit = {
            "op": "replace_clip",
            "clip_id": target["clip_id"],
            "assignment_id": alternative["assignment_id"],
        }

        result = self.timelines.apply_edit(
            project_id,
            self._edit_payload(materialize, head, edit),
            idempotency_key="timeline-replace",
            expected_database_uuid=self.repository.database_uuid,
        )
        reopened = self.timelines.read(project_id)

        replacement = reopened["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(replacement["assignment_id"], alternative["assignment_id"])
        self.assertEqual(replacement["evidence_ref"], alternative["evidence_ref"])
        self.assertEqual(result.response["result"]["edit"], edit)
        self.assertNotIn("asset_source_id", str(result.response))
        self.assertNotIn(str(self.library), str(result.response))

        replacement_source_id = next(
            row["asset_source_id"]
            for row in reopened["source_bindings"]
            if row["clip_id"] == replacement["clip_id"]
        )
        self.repository.mark_source_availability(
            str(replacement_source_id),
            "missing",
        )
        stale = self.timelines.read(project_id)
        self.assertEqual(stale["freshness"]["state"], "stale_source_binding")
        self.assertEqual(
            next(
                row["asset_source_id"]
                for row in stale["source_bindings"]
                if row["clip_id"] == replacement["clip_id"]
            ),
            replacement_source_id,
        )

    def test_stale_head_and_fault_after_revision_insert_leave_no_partial_edit(self) -> None:
        project_id, _blueprint, _coverage, materialize = self._build_project()
        self._materialize(project_id, materialize, key="timeline-edit-conflict-base")
        before = self.timelines.read(project_id)
        head = before["head"]
        timeline = before["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        first_clip = timeline["tracks"][0]["clips"][0]
        payload = self._edit_payload(
            materialize,
            head,
            {
                "op": "set_clip_duration",
                "clip_id": first_clip["clip_id"],
                "duration_ms": 1_100,
            },
        )
        original = self.repository.append_canonical_timeline_operation
        with patch.object(
            self.repository,
            "append_canonical_timeline_operation",
            side_effect=RuntimeError("injected-after-revision"),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected-after-revision"):
                self.timelines.apply_edit(
                    project_id,
                    payload,
                    idempotency_key="timeline-edit-fault",
                    expected_database_uuid=self.repository.database_uuid,
                )
        self.assertIsNotNone(original)
        self.assertEqual(self.timelines.read(project_id)["head"], head)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "canonical_timeline_operations",
                        "canonical_timeline_revisions",
                        "canonical_timeline_receipts",
                    )
                ),
                (1, 1, 1),
            )

        self.timelines.apply_edit(
            project_id,
            payload,
            idempotency_key="timeline-edit-winner",
            expected_database_uuid=self.repository.database_uuid,
        )
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.apply_edit(
                project_id,
                payload,
                idempotency_key="timeline-edit-stale",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "timeline_head_conflict")

    def test_concurrent_edits_have_one_head_cas_winner(self) -> None:
        project_id, _blueprint, _coverage, materialize = self._build_project()
        self._materialize(project_id, materialize, key="timeline-edit-race-base")
        before = self.timelines.read(project_id)
        head = before["head"]
        timeline = before["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        clip_id = timeline["tracks"][0]["clips"][0]["clip_id"]

        def apply(duration_ms: int):
            try:
                return self.timelines.apply_edit(
                    project_id,
                    self._edit_payload(
                        materialize,
                        head,
                        {
                            "op": "set_clip_duration",
                            "clip_id": clip_id,
                            "duration_ms": duration_ms,
                        },
                    ),
                    idempotency_key=f"timeline-edit-race-{duration_ms}",
                    expected_database_uuid=self.repository.database_uuid,
                )
            except TimelineLoweringServiceError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(apply, (1_300, 1_301)))

        successes = [row for row in outcomes if not isinstance(row, BaseException)]
        failures = [row for row in outcomes if isinstance(row, TimelineLoweringServiceError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual([row.code for row in failures], ["timeline_head_conflict"])
        self.assertEqual(self.timelines.read(project_id)["head"]["revision"], 2)
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertEqual(
                tuple(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0]
                    for table in (
                        "canonical_timeline_operations",
                        "canonical_timeline_revisions",
                        "canonical_timeline_receipts",
                    )
                ),
                (2, 2, 2),
            )

    def test_cold_audit_rejects_edit_result_tamper(self) -> None:
        project_id, _blueprint, _coverage, materialize = self._build_project()
        self._materialize(project_id, materialize, key="timeline-edit-tamper-base")
        before = self.timelines.read(project_id)
        head = before["head"]
        timeline = before["timeline"]
        assert isinstance(head, dict) and isinstance(timeline, dict)
        edit = {
            "op": "set_clip_duration",
            "clip_id": timeline["tracks"][0]["clips"][0]["clip_id"],
            "duration_ms": 1_250,
        }
        self.timelines.apply_edit(
            project_id,
            self._edit_payload(materialize, head, edit),
            idempotency_key="timeline-edit-before-tamper",
            expected_database_uuid=self.repository.database_uuid,
        )
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE "
                "name='trg_canonical_timeline_operations_no_update'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER trg_canonical_timeline_operations_no_update")
            connection.execute(
                """UPDATE canonical_timeline_operations
                   SET result_json=replace(result_json,'1250','1251')
                   WHERE project_id=? AND sequence=2""",
                (project_id,),
            )
            connection.execute(trigger)
        cold = MediaRepository(self.db_path)
        try:
            with patch(
                "core.timeline_edit_contract.require_exact_timeline_edit_replay",
                wraps=require_exact_timeline_edit_replay,
            ) as replay_verifier:
                with self.assertRaises(CanonicalTimelineIntegrityError):
                    cold.get_canonical_timeline_head(project_id)
            self.assertGreaterEqual(replay_verifier.call_count, 1)
            self.assertEqual(replay_verifier.call_args.kwargs["edit"]["duration_ms"], 1_251)
        finally:
            cold.close()


if __name__ == "__main__":
    unittest.main()
