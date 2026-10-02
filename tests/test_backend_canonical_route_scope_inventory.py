from __future__ import annotations

import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from flask import Flask

from backend.src import DESKTOP_TOKEN_HEADER
from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.db import ImageIndexRepository
from core.media_db import MediaRepository
from tests.test_coverage_persistence import semantic
import tests.test_timeline_lowering_api as timeline_fixture


class BackendCanonicalRouteScopeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-canonical-route-scope-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        self.db_path = self.root / "state" / "canonical.db"
        self.db_path.parent.mkdir(parents=True)
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.legacy_timelines = TimelineService(self.repository)
        self.token = "canonical-route-scope-token"
        self._fixture_build_serial = 0
        self._request_serial = 0

        app = Flask("canonical-route-scope-inventory")
        app.config.update(TESTING=True, DESKTOP_SESSION_TOKEN=self.token)
        app.extensions.update(
            {
                "media_repository": self.repository,
                "blueprint_service": self.blueprints,
                "coverage_service": self.coverage,
                "timeline_service": self.legacy_timelines,
                "timeline_lowering_service": self.timelines,
            }
        )
        app.register_blueprint(api_blueprint)
        self.client = app.test_client()

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        scoped_name = f"scope-{self._fixture_build_serial}-{name}"
        return timeline_fixture.TimelineLoweringApiTests._register_image(
            self,
            scoped_name,
            content,
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        self._fixture_build_serial += 1
        return timeline_fixture.TimelineLoweringApiTests._build_project(self)

    @property
    def query(self) -> dict[str, str]:
        return {
            "db_path": str(self.db_path),
            "expected_database_uuid": self.repository.database_uuid,
        }

    @staticmethod
    def _blueprint_ref(head: dict[str, object]) -> dict[str, object]:
        return {
            "revision": head["revision"],
            "content_sha256": head["content_sha256"],
        }

    @staticmethod
    def _coverage_blueprint_ref(
        materialize: dict[str, object],
    ) -> dict[str, object]:
        expected = materialize["expected_blueprint"]
        assert isinstance(expected, dict)
        return {
            key: expected[key]
            for key in ("revision", "content_sha256", "semantic_sha256")
        }

    @staticmethod
    def _head_projection(
        head: dict[str, object] | None,
        fields: tuple[str, ...],
    ) -> tuple[object, ...] | None:
        if head is None:
            return None
        return tuple(head.get(field) for field in fields)

    def _state_snapshot(self, project_ids: tuple[str, ...]) -> dict[str, object]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            dump = tuple(connection.iterdump())
            revision_counts = {
                project_id: tuple(
                    int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                            (project_id,),
                        ).fetchone()[0]
                    )
                    for table in (
                        "creative_blueprint_revisions",
                        "coverage_plan_revisions",
                        "canonical_timeline_revisions",
                    )
                )
                for project_id in project_ids
            }
        heads = {
            project_id: (
                self._head_projection(
                    self.repository.get_blueprint_head(project_id),
                    (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                        "operation_id",
                    ),
                ),
                self._head_projection(
                    self.repository.get_coverage_head(project_id),
                    (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    ),
                ),
                self._head_projection(
                    self.repository.get_canonical_timeline_head(project_id),
                    (
                        "revision",
                        "revision_sha256",
                        "timeline_content_sha256",
                        "operation_id",
                    ),
                ),
            )
            for project_id in project_ids
        }
        return {"dump": dump, "heads": heads, "revision_counts": revision_counts}

    def _post_denied_without_change(
        self,
        *,
        project_ids: tuple[str, ...],
        path: str,
        payload: dict[str, object],
        status: int,
        code: str,
    ) -> None:
        self._request_serial += 1
        before = self._state_snapshot(project_ids)
        response = self.client.post(
            path,
            query_string=self.query,
            json=payload,
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": f"canonical-route-denial-{self._request_serial}",
            },
        )
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["code"], code)
        self.assertEqual(self._state_snapshot(project_ids), before)

    def test_blueprint_routes_reject_cross_project_heads_and_wrong_restore_revision_without_write(
        self,
    ) -> None:
        project_a, _materialize_a, _sources_a = self._build_project()
        project_b, _materialize_b, _sources_b = self._build_project()
        head_a_one = self.repository.get_blueprint_head(project_a)
        head_b = self.repository.get_blueprint_head(project_b)
        assert head_a_one is not None and head_b is not None

        project_ids = (project_a, project_b)
        self._post_denied_without_change(
            project_ids=project_ids,
            path=f"/v1/creative/projects/{project_a}/blueprint/commit",
            payload={
                "expected_head": self._blueprint_ref(head_b),
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic("cross-project Blueprint commit"),
            },
            status=409,
            code="blueprint_head_conflict",
        )

        self.blueprints.commit_proposal(
            project_a,
            {
                "expected_head": self._blueprint_ref(head_a_one),
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic("Blueprint restore scope successor"),
            },
            idempotency_key="canonical-route-blueprint-successor",
        )
        head_a_two = self.repository.get_blueprint_head(project_a)
        assert head_a_two is not None
        restore_base = {
            "expected_head": self._blueprint_ref(head_a_two),
        }
        self._post_denied_without_change(
            project_ids=project_ids,
            path=f"/v1/creative/projects/{project_a}/blueprint/restore",
            payload={
                **restore_base,
                "restore_from": self._blueprint_ref(head_b),
            },
            status=409,
            code="blueprint_integrity_error",
        )
        self._post_denied_without_change(
            project_ids=project_ids,
            path=f"/v1/creative/projects/{project_a}/blueprint/restore",
            payload={
                **restore_base,
                "restore_from": {
                    **self._blueprint_ref(head_a_one),
                    "revision": 999,
                },
            },
            status=404,
            code="blueprint_restore_target_not_found",
        )

    def test_coverage_route_rejects_cross_project_or_forged_blueprint_head_without_write(
        self,
    ) -> None:
        project_a, materialize_a, _sources_a = self._build_project()
        project_b, materialize_b, _sources_b = self._build_project()
        coverage_a = self.repository.get_coverage_head(project_a)
        assert coverage_a is not None
        expected_plan_head = {
            "revision": coverage_a["revision"],
            "content_sha256": coverage_a["content_sha256"],
        }
        project_ids = (project_a, project_b)

        forged_blueprint = self._coverage_blueprint_ref(materialize_a)
        forged_blueprint["content_sha256"] = "0" * 64
        for label, expected_blueprint in (
            ("forged", forged_blueprint),
            ("cross-project", self._coverage_blueprint_ref(materialize_b)),
        ):
            with self.subTest(scope=label):
                self._post_denied_without_change(
                    project_ids=project_ids,
                    path=f"/v1/creative/projects/{project_a}/coverage/materialize",
                    payload={
                        "expected_blueprint": expected_blueprint,
                        "expected_plan_head": expected_plan_head,
                    },
                    status=409,
                    code="blueprint_head_conflict",
                )

    def test_timeline_routes_reject_cross_project_upstream_heads_and_forged_restore_revision_without_write(
        self,
    ) -> None:
        project_a, materialize_a, _sources_a = self._build_project()
        project_b, materialize_b, _sources_b = self._build_project()
        project_ids = (project_a, project_b)

        forged_blueprint = copy.deepcopy(materialize_a["expected_blueprint"])
        forged_coverage = copy.deepcopy(materialize_a["expected_coverage"])
        assert isinstance(forged_blueprint, dict)
        assert isinstance(forged_coverage, dict)
        forged_blueprint["content_sha256"] = "0" * 64
        forged_coverage["content_sha256"] = "0" * 64
        materialize_cases = (
            (
                "forged-blueprint",
                {**materialize_a, "expected_blueprint": forged_blueprint},
                "blueprint_head_conflict",
            ),
            (
                "forged-coverage",
                {**materialize_a, "expected_coverage": forged_coverage},
                "coverage_head_conflict",
            ),
            (
                "cross-project",
                {
                    **materialize_a,
                    "expected_blueprint": materialize_b["expected_blueprint"],
                    "expected_coverage": materialize_b["expected_coverage"],
                },
                "blueprint_head_conflict",
            ),
        )
        for label, payload, code in materialize_cases:
            with self.subTest(action="materialize", scope=label):
                self._post_denied_without_change(
                    project_ids=project_ids,
                    path=f"/v1/creative/projects/{project_a}/timeline/materialize",
                    payload=payload,
                    status=409,
                    code=code,
                )

        self.timelines.materialize_first_cut(
            project_a,
            materialize_a,
            idempotency_key="canonical-route-timeline-parent",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_a)
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        edit = {
            "op": "set_clip_duration",
            "clip_id": clip["clip_id"],
            "duration_ms": 1_450,
        }
        common = {
            "expected_blueprint": materialize_a["expected_blueprint"],
            "expected_coverage": materialize_a["expected_coverage"],
            "expected_timeline_head": current["head"],
        }
        actions = {
            "edit": {**common, "edit": edit},
            "reconcile": common,
            "restore": {**common, "restore_from": current["selected_revision"]},
        }
        for action, base in actions.items():
            selected = base.get("restore_from")
            if isinstance(selected, dict):
                base = {
                    **base,
                    "restore_from": {
                        key: value for key, value in selected.items() if key != "is_head"
                    },
                }
            action_cases = (
                (
                    "forged-blueprint",
                    {**base, "expected_blueprint": forged_blueprint},
                    "blueprint_head_conflict",
                ),
                (
                    "forged-coverage",
                    {**base, "expected_coverage": forged_coverage},
                    "coverage_head_conflict",
                ),
                (
                    "cross-project",
                    {
                        **base,
                        "expected_blueprint": materialize_b["expected_blueprint"],
                        "expected_coverage": materialize_b["expected_coverage"],
                    },
                    "blueprint_head_conflict",
                ),
            )
            for label, payload, code in action_cases:
                with self.subTest(action=action, scope=label):
                    self._post_denied_without_change(
                        project_ids=project_ids,
                        path=f"/v1/creative/projects/{project_a}/timeline/{action}",
                        payload=payload,
                        status=409,
                        code=code,
                    )

        self.timelines.apply_edit(
            project_a,
            {**common, "edit": edit},
            idempotency_key="canonical-route-timeline-successor",
            expected_database_uuid=self.repository.database_uuid,
        )
        historical = self.timelines.read(project_a, revision=1)
        current = self.timelines.read(project_a)
        restore_from = historical["selected_revision"]
        assert isinstance(restore_from, dict)
        forged_restore_from = {
            key: value for key, value in restore_from.items() if key != "is_head"
        }
        forged_restore_from["revision_sha256"] = "0" * 64
        self._post_denied_without_change(
            project_ids=project_ids,
            path=f"/v1/creative/projects/{project_a}/timeline/restore",
            payload={
                "expected_blueprint": materialize_a["expected_blueprint"],
                "expected_coverage": materialize_a["expected_coverage"],
                "expected_timeline_head": current["head"],
                "restore_from": forged_restore_from,
            },
            status=409,
            code="timeline_restore_target_invalid",
        )


if __name__ == "__main__":
    unittest.main()
