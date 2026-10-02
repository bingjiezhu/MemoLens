from __future__ import annotations

from contextlib import closing
from copy import deepcopy
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
from core.media_db import MediaRepository
from core.timeline_lowering_contract import canonical_timeline_content_sha256
import test_timeline_lowering_integration as timeline_fixture


class TimelineRestoreServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-timeline-restore-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.addCleanup(self.repository.close)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            content,
        )

    def _build_project(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        return timeline_fixture.TimelineLoweringIntegrationTests._build_project(self)

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
    def _restore_ref(workspace: dict[str, object]) -> dict[str, object]:
        selected = workspace["selected_revision"]
        assert isinstance(selected, dict)
        return {
            key: value
            for key, value in selected.items()
            if key != "is_head"
        }

    @staticmethod
    def _restore_payload(
        materialize: dict[str, object],
        current: dict[str, object],
        historical: dict[str, object],
    ) -> dict[str, object]:
        head = current["head"]
        assert isinstance(head, dict)
        return {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": head,
            "restore_from": TimelineRestoreServiceTests._restore_ref(historical),
        }

    def _materialize_and_edit(
        self,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        project_id, _blueprint, _coverage, materialize = self._build_project()
        self._materialize(project_id, materialize, key="restore-base")
        revision_one = self.timelines.read(project_id, revision=1)
        head_one = revision_one["head"]
        timeline_one = revision_one["timeline"]
        assert isinstance(head_one, dict) and isinstance(timeline_one, dict)
        clip = timeline_one["tracks"][0]["clips"][0]
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": materialize["expected_blueprint"],
                "expected_coverage": materialize["expected_coverage"],
                "expected_timeline_head": head_one,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_250,
                },
            },
            idempotency_key="restore-parent-edit",
            expected_database_uuid=self.repository.database_uuid,
        )
        return project_id, materialize, revision_one, self.timelines.read(project_id)

    def test_restore_appends_exact_historical_payload_and_replays(self) -> None:
        project_id, materialize, historical, current = self._materialize_and_edit()
        frozen_historical = deepcopy(historical)
        frozen_current = deepcopy(current)
        payload = self._restore_payload(materialize, current, historical)

        first = self.timelines.restore_revision(
            project_id,
            payload,
            idempotency_key="restore-one-as-three",
            expected_database_uuid=self.repository.database_uuid,
        )
        replay = self.timelines.restore_revision(
            project_id,
            payload,
            idempotency_key="restore-one-as-three",
            expected_database_uuid=self.repository.database_uuid,
        )
        reopened = self.timelines.read(project_id)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(first.response["command_type"], "timeline.restore_revision")
        self.assertEqual(first.response["result"]["kind"], "revision_restored")
        self.assertEqual(first.response["result"]["restore_from"], payload["restore_from"])
        self.assertEqual(first.response["result"]["result_head"], reopened["head"])
        self.assertEqual(reopened["head"]["revision"], 3)

        expected_timeline = deepcopy(historical["timeline"])
        expected_timeline["revision"] = 3
        expected_timeline["parent"] = {
            "revision": 2,
            "content_sha256": canonical_timeline_content_sha256(
                current["timeline"]
            ),
        }
        self.assertEqual(reopened["timeline"], expected_timeline)
        self.assertEqual(reopened["source_bindings"], historical["source_bindings"])
        self.assertEqual(historical, frozen_historical)
        self.assertEqual(current, frozen_current)
        reopened_historical = self.timelines.read(project_id, revision=1)
        expected_selected_revision = deepcopy(historical["selected_revision"])
        expected_selected_revision["is_head"] = False
        self.assertEqual(
            reopened_historical["selected_revision"],
            expected_selected_revision,
        )
        self.assertEqual(reopened_historical["timeline"], historical["timeline"])
        self.assertEqual(
            reopened_historical["source_bindings"],
            historical["source_bindings"],
        )
        self.assertEqual(reopened_historical["head"], reopened["head"])

    def test_invalid_target_source_stale_and_fault_write_zero_rows(self) -> None:
        project_id, materialize, historical, current = self._materialize_and_edit()
        payload = self._restore_payload(materialize, current, historical)
        invalid = deepcopy(payload)
        invalid["restore_from"] = deepcopy(current["head"])
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.restore_revision(
                project_id,
                invalid,
                idempotency_key="restore-current-invalid",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "timeline_restore_target_invalid")

        with patch.object(
            self.repository,
            "append_canonical_timeline_operation",
            side_effect=RuntimeError("restore-after-revision"),
        ):
            with self.assertRaisesRegex(RuntimeError, "restore-after-revision"):
                self.timelines.restore_revision(
                    project_id,
                    payload,
                    idempotency_key="restore-fault",
                    expected_database_uuid=self.repository.database_uuid,
                )
        self.assertEqual(self.timelines.read(project_id)["head"], current["head"])
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

        source_id = historical["source_bindings"][0]["asset_source_id"]
        self.repository.mark_source_availability(str(source_id), "missing")
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.restore_revision(
                project_id,
                payload,
                idempotency_key="restore-stale-source",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "timeline_restore_source_stale")


if __name__ == "__main__":
    unittest.main()
