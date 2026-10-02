from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
import hashlib
import os
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
from core.image_analysis_contract import seal_image_analysis_result
from core.media_db import (
    CanonicalTimelineIntegrityError,
    MediaRepository,
    utc_now_iso,
)
from tests.test_coverage_persistence import semantic
from tests import test_image_projection_schema_guards as image_fixture


class TimelineLoweringIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-timeline-")
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
        path = self.library / name
        if not path.exists() or path.read_bytes() != content:
            path.write_bytes(content)
        observed = path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="image",
            sha256=hashlib.sha256(content).hexdigest(),
            mime_type="image/jpeg",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        source_id = str(asset["asset_source_id"])
        self.repository.update_image_probe(asset_id, width=24, height=12)
        current = self.repository.get_current_image_analysis(asset_id)
        if (
            current is not None
            and current["projection"]["status"] == "current"
            and current["source"]["source_id"] == source_id
        ):
            return asset
        database_stat = os.stat(self.db_path, follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=asset_id,
            source_id=source_id,
            analysis_profile=image_fixture.PROFILE,
            expected_head=None,
            enqueue_scope="timeline-integration/image-analysis",
            idempotency_key=f"timeline-image-{asset_id}",
        )
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        result = seal_image_analysis_result(
            {
                "object": "memolens.image_analysis_result",
                "schema_version": "1",
                "asset_id": asset_id,
                "asset_sha256": hashlib.sha256(content).hexdigest(),
                "analysis_run_id": binding["analysis_run_id"],
                "revision": binding["intended_revision"],
                "source_binding": {
                    "source_id": source_id,
                    "library_root_id": root_id,
                    "observed_size": binding["observed_size"],
                    "observed_mtime_ns": binding["observed_mtime_ns"],
                    "file_identity_sha256": binding["file_identity_sha256"],
                },
                "analysis_profile": {
                    "id": image_fixture.PROFILE["profile_id"],
                    "version": image_fixture.PROFILE["profile_version"],
                    "content_sha256": image_fixture.PROFILE["profile_sha256"],
                },
                "stages": {
                    "metadata": {
                        "status": "succeeded",
                        "provenance": image_fixture._provenance("timeline-probe"),
                        "output": {
                            "mime_type": "image/jpeg",
                            "file_size": binding["observed_size"],
                            "width": 24,
                            "height": 12,
                            "taken_at": None,
                            "latitude": None,
                            "longitude": None,
                            "altitude": None,
                        },
                        "artifact_sha256": hashlib.sha256(
                            image_fixture.METADATA_ARTIFACT
                        ).hexdigest(),
                        "reason_code": None,
                    },
                    "geocode": image_fixture._disabled_stage(
                        "network-policy",
                        "network_not_authorized",
                    ),
                    "vision": image_fixture._disabled_stage(
                        "provider-policy",
                        "provider_not_authorized",
                    ),
                    "embedding": {
                        "status": "succeeded",
                        "provenance": image_fixture._provenance(
                            "timeline-embedding"
                        ),
                        "output": {
                            "combined_text_sha256": None,
                            "vectors": [
                                {
                                    "purpose": "image_search",
                                    "signal": "visual",
                                    "model_id": "timeline-visual-v1",
                                    "dimensions": 3,
                                    "artifact_sha256": hashlib.sha256(
                                        image_fixture.IMAGE_VECTOR_ARTIFACT
                                    ).hexdigest(),
                                }
                            ],
                        },
                        "artifact_sha256": hashlib.sha256(
                            image_fixture.EMBEDDING_ARTIFACT
                        ).hexdigest(),
                        "reason_code": None,
                    },
                    "quality": image_fixture._disabled_stage(
                        "quality-policy",
                        "quality_not_configured",
                    ),
                },
            }
        )
        artifacts = [
            {
                "stage": "metadata",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.METADATA_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.METADATA_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.EMBEDDING_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.EMBEDDING_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/vnd.memolens.float32-vector",
                "signal": "visual",
                "model_id": "timeline-visual-v1",
                "dimensions": 3,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.IMAGE_VECTOR_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.IMAGE_VECTOR_ARTIFACT,
            },
        ]
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=result,
            artifacts=artifacts,
        )
        self.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        return asset

    def _reanalyze_image(
        self,
        asset_id: str,
        *,
        key: str,
    ) -> dict[str, object]:
        current = self.repository.get_current_image_analysis(asset_id)
        assert current is not None
        prior_result = current["result"]
        prior_head = current["analysis_binding"]
        assert isinstance(prior_result, dict) and isinstance(prior_head, dict)
        prior_source = prior_result["source_binding"]
        assert isinstance(prior_source, dict)
        database_stat = os.stat(self.db_path, follow_symlinks=False)
        job = self.repository.enqueue_image_analysis(
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            database_file_identity=(database_stat.st_dev, database_stat.st_ino),
            asset_id=asset_id,
            source_id=str(prior_source["source_id"]),
            analysis_profile=image_fixture.PROFILE,
            expected_head=prior_head,
            enqueue_scope="timeline-integration/image-reanalysis",
            idempotency_key=key,
        )
        job_id = str(job["id"])
        binding = self.repository.get_image_analysis_job_binding(job_id)
        assert binding is not None
        successor = deepcopy(prior_result)
        successor.pop("content_sha256", None)
        successor["analysis_run_id"] = binding["analysis_run_id"]
        successor["revision"] = binding["intended_revision"]
        successor["source_binding"] = {
            "source_id": binding["source_id"],
            "library_root_id": binding["library_root_id"],
            "observed_size": binding["observed_size"],
            "observed_mtime_ns": binding["observed_mtime_ns"],
            "file_identity_sha256": binding["file_identity_sha256"],
        }
        sealed = seal_image_analysis_result(successor)
        artifacts = [
            {
                "stage": "metadata",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.METADATA_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.METADATA_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.EMBEDDING_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.EMBEDDING_ARTIFACT,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/vnd.memolens.float32-vector",
                "signal": "visual",
                "model_id": "timeline-visual-v1",
                "dimensions": 3,
                "artifact_sha256": hashlib.sha256(
                    image_fixture.IMAGE_VECTOR_ARTIFACT
                ).hexdigest(),
                "bytes": image_fixture.IMAGE_VECTOR_ARTIFACT,
            },
        ]
        self.repository.publish_image_analysis(
            job_id=job_id,
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
            result=sealed,
            artifacts=artifacts,
        )
        self.repository.project_image_analysis_change(
            job_id=job_id,
            runtime_generation=image_fixture.RUNTIME_GENERATION,
            expected_attempt=1,
        )
        return {
            "analysis_run_id": binding["analysis_run_id"],
            "revision": binding["intended_revision"],
            "content_sha256": sealed["content_sha256"],
        }

    def _register_video_spans(
        self,
        ranges: tuple[tuple[int, int], ...],
    ) -> list[str]:
        name = "ending.mp4"
        content = b"timeline-video-span"
        path = self.library / name
        path.write_bytes(content)
        observed = path.stat()
        digest = hashlib.sha256(content).hexdigest()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        asset = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=name,
            filename=name,
            kind="video",
            sha256=digest,
            mime_type="video/mp4",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        asset_id = str(asset["id"])
        run_id = f"arun_{'b' * 32}"
        now = utc_now_iso()
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,
                     analysis_profile_json,input_asset_sha256,status,
                     transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','timeline-test','{}',?,'succeeded',
                          'available','ready',?)""",
                (run_id, asset_id, digest, now),
            )
            for ordinal, (start_ms, end_ms) in enumerate(ranges):
                span_id = (
                    f"seg_{asset_id.removeprefix('asset_')}_1_{ordinal}"
                )
                connection.execute(
                    """INSERT INTO video_segments(
                         id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                         boundary_reason,semantic_json,combined_text,visual_status,
                         transcript_status,created_at)
                       VALUES(?,?,?,?,?,?,'timeline-test','{}','',
                              'ready','available',?)""",
                    (
                        span_id,
                        asset_id,
                        run_id,
                        ordinal,
                        start_ms,
                        end_ms,
                        now,
                    ),
                )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                     asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, run_id, now),
            )
        return [
            (
                "memolens://evidence/span/"
                f"seg_{asset_id.removeprefix('asset_')}_1_{ordinal}"
            )
            for ordinal in range(len(ranges))
        ]

    def _register_video_span(self) -> str:
        return TimelineLoweringIntegrationTests._register_video_spans(
            self,
            ((1_000, 11_000),),
        )[0]

    def _build_project(
        self,
        *,
        lowerable: bool = True,
        include_unselected_alternative: bool = False,
    ) -> tuple[str, dict[str, object], dict[str, object], dict[str, object]]:
        first = self._register_image("opening.jpg", b"timeline-opening")
        first_ref = f"memolens://evidence/asset/{first['id']}"
        proposed = semantic("timeline", evidence_ref=first_ref)
        if lowerable:
            second = self._register_image("ending.jpg", b"timeline-ending")
            proposed["material_hints"].append(
                {
                    "hint_id": "ending_image",
                    "evidence_ref": f"memolens://evidence/asset/{second['id']}",
                    "script_block_ids": ["ending"],
                    "reason": "Use the second exact image for the ending.",
                }
            )
        if include_unselected_alternative:
            alternative = self._register_image(
                "opening-alternative.jpg",
                b"timeline-opening-alternative",
            )
            proposed["material_hints"].append(
                {
                    "hint_id": "opening_alternative",
                    "evidence_ref": (
                        f"memolens://evidence/asset/{alternative['id']}"
                    ),
                    "script_block_ids": ["opening"],
                    "reason": "Keep one exact alternative for the opening.",
                }
            )
        project = self.repository.create_project(
            "Canonical first cut",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "timeline-integration-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key="blueprint-timeline",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            key: blueprint[key]
            for key in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint_binding[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="coverage-timeline",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        coverage_binding = {
            key: coverage[key]
            for key in (
                "revision",
                "content_sha256",
                "evidence_manifest_sha256",
                "operation_id",
            )
        }
        payload = {
            "expected_blueprint": blueprint_binding,
            "expected_coverage": coverage_binding,
            "expected_timeline_head": None,
        }
        return project_id, blueprint, coverage, payload

    def _materialize(
        self,
        project_id: str,
        payload: dict[str, object],
        *,
        key: str,
    ):
        return self.timelines.materialize_first_cut(
            project_id,
            payload,
            idempotency_key=key,
            expected_database_uuid=self.repository.database_uuid,
        )

    def _advance_coverage(
        self,
        project_id: str,
        blueprint: dict[str, object],
        coverage: dict[str, object],
        *,
        key: str,
    ) -> dict[str, object]:
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    field: blueprint[field]
                    for field in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": {
                    "revision": coverage["revision"],
                    "content_sha256": coverage["content_sha256"],
                },
            },
            idempotency_key=key,
            expected_database_uuid=self.repository.database_uuid,
        )
        advanced = self.repository.get_coverage_head(project_id)
        assert advanced is not None
        return advanced

    def _reconcile_payload(
        self,
        blueprint: dict[str, object],
        coverage: dict[str, object],
        timeline_head: dict[str, object],
    ) -> dict[str, object]:
        return {
            "expected_blueprint": {
                field: blueprint[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "semantic_sha256",
                    "operation_id",
                )
            },
            "expected_coverage": {
                field: coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": dict(timeline_head),
        }

    def test_materialize_replay_and_trusted_read_are_exact(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()

        first = self._materialize(project_id, payload, key="timeline-first")
        replay = self._materialize(project_id, payload, key="timeline-first")
        workspace = self.timelines.read(project_id)

        self.assertEqual(first.response_status, 201)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(workspace["freshness"], {"state": "current", "reasons": []})
        self.assertEqual(workspace["lifecycle"]["state"], "draft")
        self.assertEqual(workspace["lifecycle"]["approval"], "not_established")
        self.assertFalse(workspace["lifecycle"]["exportable"])
        timeline = workspace["timeline"]
        self.assertEqual(len(timeline["tracks"][0]["clips"]), 2)
        coverage_head = self.repository.get_coverage_head(project_id)
        assert coverage_head is not None
        first_clip = timeline["tracks"][0]["clips"][0]
        image_proof = next(
            row["proof"]
            for row in coverage_head["plan"]["evidence_manifest"]
            if row["evidence_ref"] == first_clip["evidence_ref"]
        )
        self.assertEqual(
            set(image_proof),
            {
                "kind",
                "asset_id",
                "asset_sha256",
                "analysis_run_id",
                "analysis_revision",
                "analysis_content_sha256",
                "source_binding_sha256",
            },
        )
        first_source = workspace["source_bindings"][0]
        for field in (
            "asset_id",
            "asset_sha256",
            "analysis_run_id",
            "analysis_revision",
            "analysis_content_sha256",
            "source_binding_sha256",
        ):
            self.assertEqual(first_clip[field], image_proof[field])
            self.assertEqual(first_source[field], image_proof[field])
        self.assertNotIn("asset_source_id", str(timeline))
        self.assertNotIn(str(self.library), str(workspace))
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
                        "canonical_timeline_heads",
                        "canonical_timeline_receipts",
                    )
                ),
                (1, 1, 1, 1),
            )

    def test_reconcile_advances_one_revision_replays_and_preserves_history(self) -> None:
        project_id, blueprint, coverage, materialize_payload = self._build_project()
        self._materialize(project_id, materialize_payload, key="timeline-before-reconcile")
        old_workspace = self.timelines.read(project_id)
        old_head = old_workspace["head"]
        assert isinstance(old_head, dict)
        advanced_coverage = self._advance_coverage(
            project_id,
            blueprint,
            coverage,
            key="coverage-for-reconcile",
        )
        stale = self.timelines.read(project_id)
        self.assertEqual(stale["freshness"]["state"], "stale_coverage")
        reconcile_payload = self._reconcile_payload(
            blueprint,
            advanced_coverage,
            old_head,
        )

        first = self.timelines.reconcile_from_coverage(
            project_id,
            reconcile_payload,
            idempotency_key="timeline-reconcile",
            expected_database_uuid=self.repository.database_uuid,
        )
        replay = self.timelines.reconcile_from_coverage(
            project_id,
            reconcile_payload,
            idempotency_key="timeline-reconcile",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_id)
        historical = self.timelines.read(project_id, revision=1)

        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)
        self.assertEqual(first.response["command_type"], "timeline.reconcile_from_coverage")
        self.assertEqual(first.response["result"]["result_head"]["revision"], 2)
        self.assertEqual(current["freshness"], {"state": "current", "reasons": []})
        self.assertEqual(current["head"]["revision"], 2)
        self.assertEqual(current["timeline"]["parent"], {
            "revision": 1,
            "content_sha256": old_head["timeline_content_sha256"],
        })
        self.assertEqual(current["head"]["timeline_id"], old_head["timeline_id"])
        self.assertEqual(historical["selected_revision"]["revision"], 1)
        self.assertFalse(historical["selected_revision"]["is_head"])
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
                        "canonical_timeline_heads",
                        "canonical_timeline_receipts",
                    )
                ),
                (2, 2, 1, 2),
            )
            rows = connection.execute(
                """SELECT sequence,command_type,expected_timeline_head_json
                     FROM canonical_timeline_operations
                     WHERE project_id=? ORDER BY sequence""",
                (project_id,),
            ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [
                (1, "timeline.materialize_first_cut"),
                (2, "timeline.reconcile_from_coverage"),
            ],
        )
        self.assertEqual(rows[0][2], "null")

    def test_reconcile_rejects_current_and_stale_expected_heads_without_writes(self) -> None:
        project_id, blueprint, coverage, materialize_payload = self._build_project()
        self._materialize(project_id, materialize_payload, key="timeline-reconcile-conflict")
        current_head = self.timelines.read(project_id)["head"]
        assert isinstance(current_head, dict)
        current_payload = self._reconcile_payload(blueprint, coverage, current_head)
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.reconcile_from_coverage(
                project_id,
                current_payload,
                idempotency_key="timeline-reconcile-noop",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "timeline_already_current")

        advanced_coverage = self._advance_coverage(
            project_id,
            blueprint,
            coverage,
            key="coverage-reconcile-conflict",
        )
        stale_payload = self._reconcile_payload(
            blueprint,
            advanced_coverage,
            current_head,
        )
        self.timelines.reconcile_from_coverage(
            project_id,
            stale_payload,
            idempotency_key="timeline-reconcile-winner",
            expected_database_uuid=self.repository.database_uuid,
        )
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.reconcile_from_coverage(
                project_id,
                stale_payload,
                idempotency_key="timeline-reconcile-loser",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "timeline_head_conflict")
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
                        "canonical_timeline_heads",
                        "canonical_timeline_receipts",
                    )
                ),
                (2, 2, 1, 2),
            )

    def test_cold_read_rejects_tampered_reconcile_parent_binding(self) -> None:
        project_id, blueprint, coverage, materialize_payload = self._build_project()
        self._materialize(project_id, materialize_payload, key="timeline-before-tamper")
        old_head = self.timelines.read(project_id)["head"]
        assert isinstance(old_head, dict)
        advanced_coverage = self._advance_coverage(
            project_id,
            blueprint,
            coverage,
            key="coverage-before-tamper",
        )
        self.timelines.reconcile_from_coverage(
            project_id,
            self._reconcile_payload(blueprint, advanced_coverage, old_head),
            idempotency_key="timeline-reconcile-before-tamper",
            expected_database_uuid=self.repository.database_uuid,
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE name='trg_canonical_timeline_operations_no_update'"""
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_operations_no_update"
            )
            connection.execute(
                """UPDATE canonical_timeline_operations
                   SET expected_timeline_head_json='null'
                   WHERE project_id=? AND sequence=2""",
                (project_id,),
            )
            connection.execute(trigger)
            connection.commit()
        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaises(CanonicalTimelineIntegrityError):
                cold.get_canonical_timeline_head(project_id)
        finally:
            cold.close()

    def test_gap_fails_without_partial_timeline_rows(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project(
            lowerable=False
        )

        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize(project_id, payload, key="timeline-gap")
        self.assertEqual(raised.exception.code, "coverage_not_lowerable")
        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in (
                "canonical_timeline_operations",
                "canonical_timeline_revisions",
                "canonical_timeline_heads",
                "canonical_timeline_receipts",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone(),
                    (0,),
                )

    def test_fixed_source_becomes_stale_without_implicit_failover(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()
        self._materialize(project_id, payload, key="timeline-source")
        workspace = self.timelines.read(project_id)
        first_binding = workspace["source_bindings"][0]
        self.repository.mark_source_availability(
            str(first_binding["asset_source_id"]),
            "missing",
        )

        stale = self.timelines.read(project_id)

        self.assertEqual(stale["freshness"]["state"], "stale_source_binding")
        self.assertEqual(
            stale["source_bindings"][0]["asset_source_id"],
            first_binding["asset_source_id"],
        )

    def test_image_materialization_uses_result_bound_source_not_preferred_alternate(
        self,
    ) -> None:
        project_id, _blueprint, coverage, payload = self._build_project()
        first_ref = coverage["plan"]["beats"][0]["selected_assignments"][0][
            "evidence_ref"
        ]
        first_proof = next(
            row["proof"]
            for row in coverage["plan"]["evidence_manifest"]
            if row["evidence_ref"] == first_ref
        )
        asset_id = str(first_proof["asset_id"])
        current = self.repository.get_current_image_analysis(asset_id)
        assert current is not None
        result_bound_source_id = str(current["source"]["source_id"])
        duplicate_path = self.library / "opening-copy.jpg"
        duplicate_content = b"timeline-opening"
        duplicate_path.write_bytes(duplicate_content)
        observed = duplicate_path.stat()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        duplicate = self.repository.upsert_asset_source(
            root_id=root_id,
            relative_path=duplicate_path.name,
            filename=duplicate_path.name,
            kind="image",
            sha256=hashlib.sha256(duplicate_content).hexdigest(),
            mime_type="image/jpeg",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        alternate_source_id = str(duplicate["asset_source_id"])
        self.assertNotEqual(alternate_source_id, result_bound_source_id)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET is_preferred=0 WHERE asset_id=?",
                (asset_id,),
            )
            connection.execute(
                "UPDATE asset_sources SET is_preferred=1 WHERE id=?",
                (alternate_source_id,),
            )

        self._materialize(project_id, payload, key="timeline-result-bound-source")
        workspace = self.timelines.read(project_id)
        binding = next(
            row
            for row in workspace["source_bindings"]
            if row["asset_id"] == asset_id
        )
        self.assertEqual(binding["asset_source_id"], result_bound_source_id)
        self.repository.mark_source_availability(result_bound_source_id, "missing")
        stale = self.timelines.read(project_id)
        self.assertEqual(stale["freshness"]["state"], "stale_source_binding")
        self.assertEqual(binding["asset_source_id"], result_bound_source_id)

    def test_image_n_plus_one_stales_coverage_and_timeline_before_zero_write_reconcile(
        self,
    ) -> None:
        project_id, blueprint, coverage, payload = self._build_project()
        self._materialize(project_id, payload, key="timeline-before-image-n-plus-one")
        before = self.timelines.read(project_id)
        head = before["head"]
        first_binding = before["source_bindings"][0]
        assert isinstance(head, dict) and isinstance(first_binding, dict)
        old_run_id = str(first_binding["analysis_run_id"])

        successor = self._reanalyze_image(
            str(first_binding["asset_id"]),
            key="timeline-image-n-plus-one",
        )

        self.assertNotEqual(successor["analysis_run_id"], old_run_id)
        self.assertEqual(
            self.coverage.read(project_id)["freshness"]["state"],
            "stale_evidence",
        )
        self.assertEqual(
            self.timelines.read(project_id)["freshness"],
            {
                "state": "stale_source_binding",
                "reasons": ["fixed_source_binding_changed"],
            },
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            before_counts = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                for table in (
                    "canonical_timeline_operations",
                    "canonical_timeline_revisions",
                    "canonical_timeline_receipts",
                )
            )
        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self.timelines.reconcile_from_coverage(
                project_id,
                self._reconcile_payload(blueprint, coverage, head),
                idempotency_key="timeline-stale-image-n-plus-one",
                expected_database_uuid=self.repository.database_uuid,
            )
        self.assertEqual(raised.exception.code, "coverage_plan_stale_evidence")
        with closing(sqlite3.connect(self.db_path)) as connection:
            after_counts = tuple(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0]
                for table in (
                    "canonical_timeline_operations",
                    "canonical_timeline_revisions",
                    "canonical_timeline_receipts",
                )
            )
        self.assertEqual(after_counts, before_counts)

        refreshed_coverage = self._advance_coverage(
            project_id,
            blueprint,
            coverage,
            key="coverage-image-n-plus-one",
        )
        self.timelines.reconcile_from_coverage(
            project_id,
            self._reconcile_payload(blueprint, refreshed_coverage, head),
            idempotency_key="timeline-image-n-plus-one-reconcile",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_id)
        self.assertEqual(current["freshness"], {"state": "current", "reasons": []})
        self.assertEqual(
            current["source_bindings"][0]["analysis_run_id"],
            successor["analysis_run_id"],
        )

    def test_unselected_coverage_evidence_staleness_blocks_and_invalidates_timeline(
        self,
    ) -> None:
        project_id, blueprint, coverage, payload = self._build_project(
            include_unselected_alternative=True,
        )
        plan = coverage["plan"]
        selected_refs = {
            assignment["evidence_ref"]
            for beat in plan["beats"]
            for assignment in beat["selected_assignments"]
        }
        alternative = next(
            item
            for item in plan["evidence_manifest"]
            if item["evidence_ref"] not in selected_refs
        )
        alternative_asset = self.repository.get_asset(
            str(alternative["proof"]["asset_id"])
        )
        assert alternative_asset is not None
        alternative_source_id = str(alternative_asset["asset_source_id"])
        self.repository.mark_source_availability(
            alternative_source_id,
            "missing",
        )

        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize(project_id, payload, key="timeline-stale-alternative")
        self.assertEqual(raised.exception.code, "coverage_plan_stale_evidence")
        with self.repository.transaction() as connection:
            removal = connection.execute(
                """SELECT position FROM image_projection_changes
                    WHERE asset_id=? AND operation='remove'
                    ORDER BY position DESC LIMIT 1""",
                (alternative["proof"]["asset_id"],),
            ).fetchone()
        assert removal is not None
        self.repository.project_image_source_removal(
            change_position=int(removal["position"]),
        )
        self.repository.mark_source_availability(
            alternative_source_id,
            "available",
        )
        self._reanalyze_image(
            str(alternative["proof"]["asset_id"]),
            key="timeline-restored-alternative-reanalysis",
        )
        refreshed_coverage = self._advance_coverage(
            project_id,
            blueprint,
            coverage,
            key="coverage-after-restored-alternative",
        )
        refreshed_payload = {
            **payload,
            "expected_coverage": {
                field: refreshed_coverage[field]
                for field in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
        }
        self._materialize(
            project_id,
            refreshed_payload,
            key="timeline-after-restored-alternative",
        )
        self.repository.mark_source_availability(
            alternative_source_id,
            "missing",
        )

        stale = self.timelines.read(project_id)

        self.assertEqual(stale["freshness"]["state"], "stale_coverage")
        self.assertEqual(
            stale["freshness"]["reasons"],
            ["coverage_plan_stale_evidence"],
        )
        self.assertEqual(stale["selected_revision"]["blueprint_binding"]["revision"], blueprint["revision"])

    def test_existing_head_wins_but_lost_response_replay_remains_available(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()
        first = self._materialize(project_id, payload, key="timeline-replay")
        replay = self._materialize(project_id, payload, key="timeline-replay")
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.response, first.response)

        with self.assertRaises(TimelineLoweringServiceError) as raised:
            self._materialize(project_id, payload, key="timeline-second-command")
        self.assertEqual(raised.exception.code, "timeline_manual_current_conflict")

    def test_verified_video_span_lowers_to_exact_silent_range(self) -> None:
        image = self._register_image("span-opening.jpg", b"span-opening")
        span_ref = self._register_video_span()
        proposed = semantic(
            "span-timeline",
            evidence_ref=f"memolens://evidence/asset/{image['id']}",
        )
        proposed["material_hints"].append(
            {
                "hint_id": "ending_span",
                "evidence_ref": span_ref,
                "script_block_ids": ["ending"],
                "reason": "Use the exact current span for the ending.",
            }
        )
        project = self.repository.create_project(
            "Span first cut",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "timeline-integration-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key="blueprint-span-timeline",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            key: blueprint[key]
            for key in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint_binding[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="coverage-span-timeline",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        payload = {
            "expected_blueprint": blueprint_binding,
            "expected_coverage": {
                key: coverage[key]
                for key in (
                    "revision",
                    "content_sha256",
                    "evidence_manifest_sha256",
                    "operation_id",
                )
            },
            "expected_timeline_head": None,
        }

        self._materialize(project_id, payload, key="timeline-span")
        workspace = self.timelines.read(project_id)
        video = workspace["timeline"]["tracks"][0]["clips"][1]

        self.assertEqual(video["media_kind"], "video")
        self.assertEqual(video["source_in_ms"], 1000)
        self.assertEqual(
            video["source_out_ms"] - video["source_in_ms"],
            video["end_ms"] - video["start_ms"],
        )
        self.assertFalse(video["audio_enabled"])

    def test_distinct_spans_from_one_video_preserve_one_fixed_source(self) -> None:
        span_refs = self._register_video_spans(
            ((1_000, 11_000), (12_000, 22_000))
        )
        proposed = semantic(
            "shared-video-source",
            evidence_ref=span_refs[0],
        )
        proposed["material_hints"][0]["reason"] = (
            "Use the first exact video span for the opening."
        )
        proposed["material_hints"].append(
            {
                "hint_id": "ending_video_span",
                "evidence_ref": span_refs[1],
                "script_block_ids": ["ending"],
                "reason": "Use another exact span from the same video for the ending.",
            }
        )
        project = self.repository.create_project(
            "Shared video source first cut",
            {"goal": "legacy base", "candidate_refs": []},
            {"created_by": "timeline-integration-test"},
        )
        project_id = str(project["id"])
        brief = self.repository.get_brief(project_id, 1)
        assert brief is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": None,
                "initial_legacy_brief": {
                    "revision": 1,
                    "content_sha256": brief["content_sha256"],
                },
                "source_candidate_sha256": None,
                "semantic": proposed,
            },
            idempotency_key="blueprint-shared-video-source",
        )
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        blueprint_binding = {
            key: blueprint[key]
            for key in (
                "revision",
                "content_sha256",
                "semantic_sha256",
                "operation_id",
            )
        }
        self.coverage.materialize_baseline(
            project_id,
            {
                "expected_blueprint": {
                    key: blueprint_binding[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "semantic_sha256",
                    )
                },
                "expected_plan_head": None,
            },
            idempotency_key="coverage-shared-video-source",
            expected_database_uuid=self.repository.database_uuid,
        )
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        self._materialize(
            project_id,
            {
                "expected_blueprint": blueprint_binding,
                "expected_coverage": {
                    key: coverage[key]
                    for key in (
                        "revision",
                        "content_sha256",
                        "evidence_manifest_sha256",
                        "operation_id",
                    )
                },
                "expected_timeline_head": None,
            },
            key="timeline-shared-video-source",
        )

        workspace = self.timelines.read(project_id)
        bindings = workspace["source_bindings"]
        self.assertEqual(
            [row["evidence_ref"] for row in bindings],
            span_refs,
        )
        self.assertEqual(len({row["asset_source_id"] for row in bindings}), 1)
        self.assertEqual(len({row["asset_id"] for row in bindings}), 1)
        self.assertEqual(workspace["freshness"], {"state": "current", "reasons": []})

    def test_fault_after_revision_insert_rolls_back_all_four_layers(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()
        original = self.repository.append_canonical_timeline_revision

        def fail_after(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected_after_timeline_revision")

        with patch.object(
            self.repository,
            "append_canonical_timeline_revision",
            side_effect=fail_after,
        ), self.assertRaisesRegex(RuntimeError, "injected_after_timeline_revision"):
            self._materialize(project_id, payload, key="timeline-fault")
        with closing(sqlite3.connect(self.db_path)) as connection:
            for table in (
                "canonical_timeline_operations",
                "canonical_timeline_revisions",
                "canonical_timeline_heads",
                "canonical_timeline_receipts",
            ):
                self.assertEqual(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE project_id=?",
                        (project_id,),
                    ).fetchone(),
                    (0,),
                )

    def test_cold_read_rejects_timeline_tamper(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()
        self._materialize(project_id, payload, key="timeline-tamper")
        with closing(sqlite3.connect(self.db_path)) as connection:
            trigger = connection.execute(
                """SELECT sql FROM sqlite_schema
                    WHERE name='trg_canonical_timeline_revisions_no_update'"""
            ).fetchone()[0]
            connection.execute(
                "DROP TRIGGER trg_canonical_timeline_revisions_no_update"
            )
            connection.execute(
                """UPDATE canonical_timeline_revisions SET timeline_json='{}'
                    WHERE project_id=?""",
                (project_id,),
            )
            connection.execute(trigger)
            connection.commit()
        cold = MediaRepository(self.db_path)
        try:
            with self.assertRaises(CanonicalTimelineIntegrityError):
                cold.get_canonical_timeline_head(project_id)
        finally:
            cold.close()

    def test_trusted_read_rejects_complete_timeline_ledger_rollback(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()
        self._materialize(project_id, payload, key="timeline-rollback")
        self.assertEqual(self.timelines.read(project_id)["freshness"]["state"], "current")
        trigger_names = (
            "trg_canonical_timeline_heads_no_delete",
            "trg_canonical_timeline_receipts_no_delete",
            "trg_canonical_timeline_operations_no_delete",
            "trg_canonical_timeline_revisions_no_delete",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            triggers = {
                name: connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE name=?",
                    (name,),
                ).fetchone()[0]
                for name in trigger_names
            }
            for name in trigger_names:
                connection.execute(f"DROP TRIGGER {name}")
            for table in (
                "canonical_timeline_heads",
                "canonical_timeline_receipts",
                "canonical_timeline_operations",
                "canonical_timeline_revisions",
            ):
                connection.execute(
                    f"DELETE FROM {table} WHERE project_id=?",
                    (project_id,),
                )
            for sql in triggers.values():
                connection.execute(sql)
            connection.commit()

        with self.assertRaises(CanonicalTimelineIntegrityError):
            self.timelines.read(project_id)

    def test_concurrent_first_cut_has_one_winner_and_no_partial_rows(self) -> None:
        project_id, _blueprint, _coverage, payload = self._build_project()

        def attempt(index: int) -> str:
            try:
                self._materialize(project_id, payload, key=f"timeline-race-{index}")
                return "created"
            except TimelineLoweringServiceError as exc:
                return exc.code

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(16)))

        self.assertEqual(outcomes.count("created"), 1)
        self.assertEqual(
            outcomes.count("timeline_manual_current_conflict"),
            15,
        )
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
                        "canonical_timeline_heads",
                        "canonical_timeline_receipts",
                    )
                ),
                (1, 1, 1, 1),
            )


if __name__ == "__main__":
    unittest.main()
