from __future__ import annotations

import hashlib
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from backend.src.api import api_blueprint
from backend.src.media.blueprint import BlueprintService
from backend.src.media.coverage import CoverageService
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import TimelineLoweringService
from core.config import Settings
from core.db import ImageIndexRepository
from core.image_analysis_contract import seal_image_analysis_result
from core.media_db import MediaRepository
from tests.test_coverage_persistence import semantic
import tests.test_timeline_lowering_api as timeline_fixture


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_TOKEN = "domain-scope-inventory-token"

ATLAS_SCOPE_ACTIONS = frozenset(
    {
        "flask_http.POST:/v1/atlas/basket",
        "flask_http.POST:/v1/atlas/feedback",
        "flask_http.POST:/v1/atlas/stack/action",
    }
)
CREATOR_SCOPE_ACTIONS = frozenset(
    {
        "flask_http.POST:/v1/creative/briefs",
        "flask_http.PUT:/v1/creator/profile",
        "flask_http.PUT:/v1/inbox/assets/<asset_id>",
    }
)
JOB_SCOPE_ACTIONS = frozenset(
    {
        "flask_http.POST:/v1/index/jobs/<job_id>/cancel",
        "flask_http.POST:/v1/index/jobs/<job_id>/resume",
        "flask_http.POST:/v1/renders/<job_id>/cancel",
    }
)
CANONICAL_IDEMPOTENCY_ACTIONS = frozenset(
    {
        "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/commit",
        "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/restore",
        "flask_http.POST:/v1/creative/projects/<project_id>/timeline/edit",
        "flask_http.POST:/v1/creative/projects/<project_id>/timeline/structural-edit",
        "flask_http.POST:/v1/creative/projects/<project_id>/timeline/materialize",
        "flask_http.POST:/v1/creative/projects/<project_id>/timeline/reconcile",
        "flask_http.POST:/v1/creative/projects/<project_id>/timeline/restore",
    }
)
CANONICAL_SCOPE_ACTIONS = frozenset(
    {
        *CANONICAL_IDEMPOTENCY_ACTIONS,
        "flask_http.POST:/v1/creative/projects/<project_id>/coverage/materialize",
    }
)


RUNTIME_GENERATION = f"runtime_generation_{'d' * 64}"
IMAGE_PROFILE = {
    "profile_id": "canonical-image-local-v1",
    "profile_version": "1",
    "profile_sha256": hashlib.sha256(b"canonical-image-local-v1").hexdigest(),
}


def _provenance(rule: str) -> dict[str, object]:
    return {
        "producer_id": "memolens.image-worker",
        "producer_version": "1",
        "model_id": None,
        "model_version": None,
        "rule_id": rule,
        "rule_version": "1",
    }


def _disabled_stage(rule: str, reason: str) -> dict[str, object]:
    return {
        "status": "disabled",
        "provenance": _provenance(rule),
        "output": None,
        "artifact_sha256": None,
        "reason_code": reason,
    }


def _seed_canonical_image(repository: MediaRepository, library: Path) -> str:
    image_path = library / "oracle.png"
    Image.new("RGB", (32, 18), "blue").save(image_path)
    content = image_path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    observed = image_path.stat()
    root_id = str(repository.library_roots()[0]["id"])
    asset = repository.upsert_asset_source(
        root_id=root_id,
        relative_path=image_path.name,
        filename=image_path.name,
        kind="image",
        sha256=digest,
        mime_type="image/png",
        file_size=observed.st_size,
        mtime_ns=observed.st_mtime_ns,
        source_file_id=str(observed.st_ino),
    )
    asset_id = str(asset["id"])
    source_id = str(asset["asset_source_id"])
    repository.update_image_probe(asset_id, width=32, height=18)
    database = os.stat(repository.db_path, follow_symlinks=False)
    job = repository.enqueue_image_analysis(
        runtime_generation=RUNTIME_GENERATION,
        database_file_identity=(database.st_dev, database.st_ino),
        asset_id=asset_id,
        source_id=source_id,
        analysis_profile=IMAGE_PROFILE,
        expected_head=None,
        enqueue_scope="domain-scope-oracle/image-analysis",
        idempotency_key="canonical-oracle-image",
    )
    job_id = str(job["id"])
    binding = repository.get_image_analysis_job_binding(job_id)
    assert binding is not None
    metadata_artifact = b"metadata"
    embedding_artifact = b"embedding"
    image_vector = struct.pack("<fff", 1.0, 0.0, 0.0)
    result = seal_image_analysis_result(
        {
            "object": "memolens.image_analysis_result",
            "schema_version": "1",
            "asset_id": asset_id,
            "asset_sha256": digest,
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
                "id": IMAGE_PROFILE["profile_id"],
                "version": IMAGE_PROFILE["profile_version"],
                "content_sha256": IMAGE_PROFILE["profile_sha256"],
            },
            "stages": {
                "metadata": {
                    "status": "succeeded",
                    "provenance": _provenance("pillow-probe"),
                    "output": {
                        "mime_type": "image/png",
                        "file_size": observed.st_size,
                        "width": 32,
                        "height": 18,
                        "taken_at": None,
                        "latitude": None,
                        "longitude": None,
                        "altitude": None,
                    },
                    "artifact_sha256": hashlib.sha256(metadata_artifact).hexdigest(),
                    "reason_code": None,
                },
                "geocode": _disabled_stage("network-policy", "network_not_authorized"),
                "vision": _disabled_stage("provider-policy", "provider_not_authorized"),
                "embedding": {
                    "status": "succeeded",
                    "provenance": _provenance("controlled-oracle-vector"),
                    "output": {
                        "combined_text_sha256": None,
                        "vectors": [
                            {
                                "purpose": "image_search",
                                "signal": "visual",
                                "model_id": "controlled-oracle-visual-v1",
                                "dimensions": 3,
                                "artifact_sha256": hashlib.sha256(image_vector).hexdigest(),
                            }
                        ],
                    },
                    "artifact_sha256": hashlib.sha256(embedding_artifact).hexdigest(),
                    "reason_code": None,
                },
                "quality": _disabled_stage("quality-policy", "quality_not_configured"),
            },
        }
    )
    repository.publish_image_analysis(
        job_id=job_id,
        runtime_generation=RUNTIME_GENERATION,
        expected_attempt=1,
        result=result,
        artifacts=(
            {
                "stage": "metadata",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(metadata_artifact).hexdigest(),
                "bytes": metadata_artifact,
            },
            {
                "stage": "embedding",
                "name": "stage_output",
                "media_type": "application/json",
                "signal": None,
                "model_id": None,
                "dimensions": None,
                "artifact_sha256": hashlib.sha256(embedding_artifact).hexdigest(),
                "bytes": embedding_artifact,
            },
            {
                "stage": "embedding",
                "name": "vector:image_search",
                "media_type": "application/vnd.memolens.float32-vector",
                "signal": "visual",
                "model_id": "controlled-oracle-visual-v1",
                "dimensions": 3,
                "artifact_sha256": hashlib.sha256(image_vector).hexdigest(),
                "bytes": image_vector,
            },
        ),
    )
    repository.project_image_analysis_change(
        job_id=job_id,
        runtime_generation=RUNTIME_GENERATION,
        expected_attempt=1,
    )
    return asset_id


class BackendDomainScopeInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-domain-scope-inventory-"
        )
        cls.root = Path(cls.temporary.name).resolve()
        cls.library = cls.root / "library"
        cls.library.mkdir()
        cls.db_path = cls.root / "state" / "inventory.db"
        cls.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(cls.root / "state"),
                "IMAGE_LIBRARY_DIR": str(cls.library),
                "SQLITE_DB_PATH": str(cls.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": DESKTOP_TOKEN,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "domain-scope-main-token",
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        cls.environment.start()
        cls.app = create_app(Settings.from_env())
        cls.app.testing = True
        cls.client = cls.app.test_client()
        cls.repository = cls.app.extensions["media_repository"]
        cls.asset_id = _seed_canonical_image(cls.repository, cls.library)
        cls.image_id = cls.asset_id

        rebuilt = cls.client.post(
            "/v1/atlas/rebuild",
            json={"db_path": str(cls.db_path)},
            headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
        )
        if rebuilt.status_code != 200:
            raise AssertionError(rebuilt.get_data(as_text=True))

    @classmethod
    def tearDownClass(cls) -> None:
        shutdown_runtime_extensions(cls.app.extensions)
        cls.environment.stop()
        cls.temporary.cleanup()

    @property
    def auth(self) -> dict[str, str]:
        return {DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN}

    def _payload(self, value: dict[str, object]) -> dict[str, object]:
        return {"db_path": str(self.db_path), **value}

    def _rows(self, query: str, parameters: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return connection.execute(query, parameters).fetchall()

    def _db_dump(self) -> tuple[str, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(connection.iterdump())

    def test_atlas_mutations_reject_cross_resource_targets_atomically(self) -> None:
        self.assertEqual(
            ATLAS_SCOPE_ACTIONS,
            frozenset(
                {
                    "flask_http.POST:/v1/atlas/basket",
                    "flask_http.POST:/v1/atlas/feedback",
                    "flask_http.POST:/v1/atlas/stack/action",
                }
            ),
        )

        before_feedback = self._rows(
            "SELECT target_kind,target_id,action,weight,note FROM atlas_feedback ORDER BY id"
        )
        missing_feedback = self.client.post(
            "/v1/atlas/feedback",
            json=self._payload(
                {
                    "target_kind": "asset",
                    "target_id": "outside_asset",
                    "action": "hide",
                }
            ),
            headers=self.auth,
        )
        self.assertEqual(missing_feedback.status_code, 400)
        self.assertEqual(
            self._rows(
                "SELECT target_kind,target_id,action,weight,note FROM atlas_feedback ORDER BY id"
            ),
            before_feedback,
        )

        before_baskets = self._rows(
            "SELECT id,name,asset_ids_json,created_at,updated_at FROM atlas_baskets ORDER BY id"
        )
        mixed_basket = self.client.post(
            "/v1/atlas/basket",
            json=self._payload(
                {"asset_ids": [self.image_id, "outside_asset"], "name": "invalid"}
            ),
            headers=self.auth,
        )
        self.assertEqual(mixed_basket.status_code, 400)
        self.assertEqual(
            self._rows(
                "SELECT id,name,asset_ids_json,created_at,updated_at FROM atlas_baskets ORDER BY id"
            ),
            before_baskets,
        )
        oversized_basket = self.client.post(
            "/v1/atlas/basket",
            json=self._payload(
                {"asset_ids": [self.image_id] * 61, "name": "oversized"}
            ),
            headers=self.auth,
        )
        self.assertEqual(oversized_basket.status_code, 400)
        self.assertEqual(
            self._rows(
                "SELECT id,name,asset_ids_json,created_at,updated_at FROM atlas_baskets ORDER BY id"
            ),
            before_baskets,
        )

        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute(
                """INSERT INTO atlas_stacks(
                       id,kind,asset_ids_json,representative_image_id,best_image_id,
                       score,reason,layout_version,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "scope_stack",
                    "similar",
                    f'["{self.image_id}"]',
                    self.image_id,
                    self.image_id,
                    1.0,
                    "scope oracle",
                    "atlas_pca_kmeans_v1",
                    "2026-08-29T00:00:00+00:00",
                ),
            )
            connection.execute(
                "UPDATE atlas_projection_state SET stack_count = 1 WHERE singleton = 1"
            )
        before_stack_feedback = self._rows(
            "SELECT target_kind,target_id,action,weight,note FROM atlas_feedback ORDER BY id"
        )
        outside_keep = self.client.post(
            "/v1/atlas/stack/action",
            json=self._payload(
                {
                    "stack_id": "scope_stack",
                    "action": "keep_best",
                    "keep_asset_id": "outside_asset",
                }
            ),
            headers=self.auth,
        )
        self.assertEqual(outside_keep.status_code, 400)
        self.assertEqual(
            self._rows(
                "SELECT target_kind,target_id,action,weight,note FROM atlas_feedback ORDER BY id"
            ),
            before_stack_feedback,
        )

        missing_stack = self.client.post(
            "/v1/atlas/stack/action",
            json=self._payload(
                {
                    "stack_id": "outside_stack",
                    "action": "keep_best",
                    "keep_asset_id": self.image_id,
                }
            ),
            headers=self.auth,
        )
        self.assertEqual(missing_stack.status_code, 400)
        self.assertEqual(
            self._rows(
                "SELECT target_kind,target_id,action,weight,note FROM atlas_feedback ORDER BY id"
            ),
            before_stack_feedback,
        )

    def test_creator_profile_selection_and_review_heads_are_exact_resources(self) -> None:
        self.assertEqual(
            CREATOR_SCOPE_ACTIONS,
            frozenset(
                {
                    "flask_http.POST:/v1/creative/briefs",
                    "flask_http.PUT:/v1/creator/profile",
                    "flask_http.PUT:/v1/inbox/assets/<asset_id>",
                }
            ),
        )
        profile_headers = {**self.auth, "Idempotency-Key": "scope-profile-1"}
        profile_before = self.app.extensions[
            "creator_memory_service"
        ].current_profile()
        profile_base_revision = int(profile_before["revision"])
        profile = self.client.put(
            "/v1/creator/profile",
            json=self._payload(
                {
                    "base_revision": profile_base_revision,
                    "profile": {"tone": "scope-profile-winner"},
                    "evidence": [],
                    "source": "user_edit",
                }
            ),
            headers=profile_headers,
        )
        self.assertEqual(profile.status_code, 200, profile.get_data(as_text=True))
        saved_profile = profile.get_json()["profile"]

        project_rows_before = self._rows(
            "SELECT id,status FROM creative_projects ORDER BY id"
        )
        brief_rows_before = self._rows(
            "SELECT project_id,revision,content_sha256 FROM creative_briefs ORDER BY project_id,revision"
        )
        wrong_profile = self.client.post(
            "/v1/creative/briefs",
            json=self._payload(
                {
                    "goal": "oracle image",
                    "candidate_refs": [self.asset_id],
                    "tone": "scope-profile-winner",
                    "creator_profile_ref": {
                        "profile_id": "default",
                        "revision": saved_profile["revision"],
                        "content_sha256": "0" * 64,
                    },
                    "applied_profile_fields": ["tone"],
                }
            ),
            headers={**self.auth, "Idempotency-Key": "scope-brief-profile"},
        )
        self.assertEqual(wrong_profile.status_code, 400)
        self.assertEqual(
            self._rows("SELECT id,status FROM creative_projects ORDER BY id"),
            project_rows_before,
        )
        self.assertEqual(
            self._rows(
                "SELECT project_id,revision,content_sha256 FROM creative_briefs ORDER BY project_id,revision"
            ),
            brief_rows_before,
        )

        missing_selection = self.client.post(
            "/v1/creative/briefs",
            json=self._payload(
                {
                    "goal": "oracle image",
                    "candidate_refs": ["outside_candidate"],
                }
            ),
            headers={**self.auth, "Idempotency-Key": "scope-brief-selection"},
        )
        self.assertEqual(missing_selection.status_code, 400)
        self.assertEqual(missing_selection.get_json()["code"], "candidate_unavailable")
        self.assertEqual(
            self._rows("SELECT id,status FROM creative_projects ORDER BY id"),
            project_rows_before,
        )

        review_before = self.repository.get_asset_review(self.asset_id)
        assert review_before is not None
        review_base_revision = int(review_before["revision"])
        review_headers = {**self.auth, "Idempotency-Key": "scope-review-winner"}
        winner = self.client.put(
            f"/v1/inbox/assets/{self.asset_id}",
            json=self._payload(
                {
                    "base_revision": review_base_revision,
                    "favorite": not bool(review_before["favorite"]),
                }
            ),
            headers=review_headers,
        )
        self.assertEqual(winner.status_code, 200, winner.get_data(as_text=True))
        review_rows = self._rows(
            """SELECT asset_id,revision,inbox_state,favorite,project_ready,note
               FROM asset_review_revisions ORDER BY asset_id,revision"""
        )
        stale_review = self.client.put(
            f"/v1/inbox/assets/{self.asset_id}",
            json=self._payload(
                {"base_revision": review_base_revision, "favorite": False}
            ),
            headers={**self.auth, "Idempotency-Key": "scope-review-stale"},
        )
        self.assertEqual(stale_review.status_code, 409)
        self.assertEqual(stale_review.get_json()["code"], "review_revision_conflict")
        self.assertEqual(
            self._rows(
                """SELECT asset_id,revision,inbox_state,favorite,project_ready,note
                   FROM asset_review_revisions ORDER BY asset_id,revision"""
            ),
            review_rows,
        )

        missing_asset = self.client.put(
            "/v1/inbox/assets/outside_asset",
            json=self._payload({"base_revision": 0, "favorite": True}),
            headers={**self.auth, "Idempotency-Key": "scope-review-outside-asset"},
        )
        self.assertEqual(missing_asset.status_code, 404)
        self.assertEqual(missing_asset.get_json()["code"], "asset_not_found")
        self.assertEqual(
            self._rows(
                """SELECT asset_id,revision,inbox_state,favorite,project_ready,note
                   FROM asset_review_revisions ORDER BY asset_id,revision"""
            ),
            review_rows,
        )

        profile_rows = self._rows(
            """SELECT profile_id,revision,content_sha256,profile_json
               FROM creator_profile_revisions ORDER BY profile_id,revision"""
        )
        stale_profile = self.client.put(
            "/v1/creator/profile",
            json=self._payload(
                {
                    "base_revision": profile_base_revision,
                    "profile": {"tone": "warm"},
                    "evidence": [],
                    "source": "user_edit",
                }
            ),
            headers={**self.auth, "Idempotency-Key": "scope-profile-stale"},
        )
        self.assertEqual(stale_profile.status_code, 409)
        self.assertEqual(stale_profile.get_json()["code"], "profile_revision_conflict")
        self.assertEqual(
            self._rows(
                """SELECT profile_id,revision,content_sha256,profile_json
                   FROM creator_profile_revisions ORDER BY profile_id,revision"""
            ),
            profile_rows,
        )

    def test_job_mutations_reject_unknown_job_ids_and_missing_idempotency_before_write(
        self,
    ) -> None:
        self.assertEqual(
            JOB_SCOPE_ACTIONS,
            frozenset(
                {
                    "flask_http.POST:/v1/index/jobs/<job_id>/cancel",
                    "flask_http.POST:/v1/index/jobs/<job_id>/resume",
                    "flask_http.POST:/v1/renders/<job_id>/cancel",
                }
            ),
        )
        cases = {
            "flask_http.POST:/v1/index/jobs/<job_id>/cancel": (
                "/v1/index/jobs/outside_job/cancel",
                "media_jobs",
            ),
            "flask_http.POST:/v1/index/jobs/<job_id>/resume": (
                "/v1/index/jobs/outside_job/resume",
                "media_jobs",
            ),
            "flask_http.POST:/v1/renders/<job_id>/cancel": (
                "/v1/renders/outside_job/cancel",
                "render_jobs",
            ),
        }
        self.assertEqual(frozenset(cases), JOB_SCOPE_ACTIONS)
        for index, (action_id, (url, table)) in enumerate(cases.items()):
            with self.subTest(action_id=action_id, denial="missing-idempotency"):
                before = self._rows(f"SELECT * FROM {table} ORDER BY id")
                missing_key = self.client.post(
                    url,
                    json=self._payload({}),
                    headers=self.auth,
                )
                self.assertEqual(missing_key.status_code, 400)
                self.assertIn("Idempotency-Key", missing_key.get_data(as_text=True))
                self.assertEqual(self._rows(f"SELECT * FROM {table} ORDER BY id"), before)

            with self.subTest(action_id=action_id, denial="unknown-job"):
                before = self._rows(f"SELECT * FROM {table} ORDER BY id")
                unknown = self.client.post(
                    url,
                    json=self._payload({}),
                    headers={
                        **self.auth,
                        "Idempotency-Key": f"scope-unknown-job-{index}",
                    },
                )
                self.assertEqual(unknown.status_code, 409)
                self.assertEqual(self._rows(f"SELECT * FROM {table} ORDER BY id"), before)

        video_path = self.library / "job-oracle.mp4"
        video_bytes = b"bounded domain scope video fixture"
        video_path.write_bytes(video_bytes)
        observed_video = video_path.stat()
        video = self.repository.upsert_asset_source(
            root_id=str(self.repository.library_roots()[0]["id"]),
            relative_path=video_path.name,
            filename=video_path.name,
            kind="video",
            sha256=hashlib.sha256(video_bytes).hexdigest(),
            mime_type="video/mp4",
            file_size=observed_video.st_size,
            mtime_ns=observed_video.st_mtime_ns,
            source_file_id=str(observed_video.st_ino),
        )
        video_id = str(video["id"])
        self.repository.update_asset_probe(
            video_id,
            {
                "duration_ms": 2_000,
                "width": 320,
                "height": 180,
                "rotation_degrees": 0,
                "captured_at": None,
                "codec": {"video_codec": "fixture", "audio_streams": []},
            },
        )
        analysis_job = self.repository.create_analysis_job(asset_id=video_id)
        analysis_job_id = str(analysis_job["id"])
        cancel_url = f"/v1/index/jobs/{analysis_job_id}/cancel"
        cancel_headers = {**self.auth, "Idempotency-Key": "rebound-index-cancel"}
        cancelled = self.client.post(
            cancel_url,
            json=self._payload({}),
            headers=cancel_headers,
        )
        self.assertEqual(cancelled.status_code, 202, cancelled.get_data(as_text=True))
        before_cancel_rebound = self._db_dump()
        cancel_rebound = self.client.post(
            cancel_url,
            json=self._payload({"rebound": True}),
            headers=cancel_headers,
        )
        self.assertEqual(cancel_rebound.status_code, 409)
        self.assertEqual(cancel_rebound.get_json()["code"], "idempotency_conflict")
        self.assertEqual(self._db_dump(), before_cancel_rebound)

        resume_url = f"/v1/index/jobs/{analysis_job_id}/resume"
        resume_headers = {**self.auth, "Idempotency-Key": "rebound-index-resume"}
        media_runner = self.app.extensions["media_job_runner"]
        with patch.object(media_runner, "submit") as submit:
            resumed = self.client.post(
                resume_url,
                json=self._payload({}),
                headers=resume_headers,
            )
            self.assertEqual(resumed.status_code, 202, resumed.get_data(as_text=True))
            submit.assert_called_once_with(analysis_job_id)
        before_resume_rebound = self._db_dump()
        resume_rebound = self.client.post(
            resume_url,
            json=self._payload({"rebound": True}),
            headers=resume_headers,
        )
        self.assertEqual(resume_rebound.status_code, 409)
        self.assertEqual(resume_rebound.get_json()["code"], "idempotency_conflict")
        self.assertEqual(self._db_dump(), before_resume_rebound)

        project = self.repository.create_project(
            "Domain scope render",
            {
                "duration_ms": 1_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": self.asset_id,
                        "asset_id": self.asset_id,
                        "asset_source_id": self.repository.available_sources(
                            self.asset_id
                        )[0]["asset_source_id"],
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "domain_scope_oracle", "external_model": False},
        )
        timeline = TimelineService(self.repository).create_from_project(
            str(project["id"])
        )
        render_job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=1,
            profile="preview-low",
            output_root_id=str(self.app.extensions["app_preview_root_id"]),
            output_relative_path=None,
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        render_job_id = str(render_job["id"])
        render_url = f"/v1/renders/{render_job_id}/cancel"
        render_headers = {**self.auth, "Idempotency-Key": "rebound-render-cancel"}
        render_cancelled = self.client.post(
            render_url,
            json=self._payload({}),
            headers=render_headers,
        )
        self.assertEqual(
            render_cancelled.status_code,
            202,
            render_cancelled.get_data(as_text=True),
        )
        before_render_rebound = self._db_dump()
        render_rebound = self.client.post(
            render_url,
            json=self._payload({"rebound": True}),
            headers=render_headers,
        )
        self.assertEqual(render_rebound.status_code, 409)
        self.assertEqual(render_rebound.get_json()["code"], "idempotency_conflict")
        self.assertEqual(self._db_dump(), before_render_rebound)

        start_url = "/v1/renders"
        start_payload = {
            "timeline_id": str(timeline["timeline"]["id"]),
            "timeline_revision": 1,
            "expected_timeline_sha256": str(timeline["content_sha256"]),
            "profile": "preview-low",
            "output": {"root_id": "app-preview-root"},
        }
        before_missing_start = self._db_dump()
        missing_start = self.client.post(
            start_url,
            json=self._payload(start_payload),
            headers=self.auth,
        )
        self.assertEqual(missing_start.status_code, 400)
        self.assertEqual(missing_start.get_json()["code"], "invalid_idempotency_key")
        self.assertEqual(self._db_dump(), before_missing_start)

        render_runner = self.app.extensions["render_job_runner"]
        start_headers = {**self.auth, "Idempotency-Key": "rebound-render-start"}
        with patch.object(render_runner, "submit") as submit_render:
            started = self.client.post(
                start_url,
                json=self._payload(start_payload),
                headers=start_headers,
            )
            self.assertEqual(started.status_code, 202, started.get_data(as_text=True))
            submit_render.assert_called_once()
        before_start_rebound = self._db_dump()
        start_rebound = self.client.post(
            start_url,
            json=self._payload({**start_payload, "profile": "export-1080p"}),
            headers=start_headers,
        )
        self.assertEqual(start_rebound.status_code, 409)
        self.assertEqual(start_rebound.get_json()["code"], "idempotency_conflict")
        self.assertEqual(self._db_dump(), before_start_rebound)

    def test_legacy_timeline_routes_reject_project_lineage_brief_and_revision_scope_before_write(
        self,
    ) -> None:
        source = self.repository.available_sources(self.asset_id)[0]
        project = self.repository.create_project(
            "Legacy resource scope",
            {
                "duration_ms": 3_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": self.asset_id,
                        "asset_id": self.asset_id,
                        "asset_source_id": source["asset_source_id"],
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "domain_scope_oracle", "external_model": False},
        )
        timeline_url = f"/v1/creative/projects/{project['id']}/timelines"

        create_denials = (
            (
                timeline_url,
                {"brief_revision": 1, "project_id": "project-outside"},
                400,
            ),
            (
                timeline_url,
                {
                    "brief_revision": 1,
                    "lineage": {"project_id": "project-outside"},
                },
                400,
            ),
            (timeline_url, {"brief_revision": 0}, 400),
            (timeline_url, {"brief_revision": 2}, 404),
            (
                "/v1/creative/projects/project-outside/timelines",
                {"brief_revision": 1},
                404,
            ),
        )
        for index, (url, payload, expected_status) in enumerate(create_denials):
            with self.subTest(create_denial=index):
                before = self._db_dump()
                denied = self.client.post(
                    url,
                    json=self._payload(payload),
                    headers={
                        **self.auth,
                        "Idempotency-Key": f"legacy-create-scope-{index}",
                    },
                )
                self.assertEqual(
                    denied.status_code,
                    expected_status,
                    denied.get_data(as_text=True),
                )
                self.assertEqual(self._db_dump(), before)

        created = self.client.post(
            timeline_url,
            json=self._payload({"brief_revision": 1}),
            headers={**self.auth, "Idempotency-Key": "legacy-scope-create"},
        )
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        timeline = created.get_json()["timeline"]
        timeline_id = str(timeline["id"])
        clip_id = str(timeline["tracks"][0]["clips"][0]["id"])
        revise_url = f"/v1/timelines/{timeline_id}/revise"
        operation = {
            "op": "set_volume",
            "clip_id": clip_id,
            "volume_db": -3,
            "preconditions": {"timeline_revision": 1},
        }
        revised = self.client.post(
            revise_url,
            json=self._payload(
                {
                    "base_revision": 1,
                    "operations": [operation],
                    "apply": True,
                }
            ),
            headers={**self.auth, "Idempotency-Key": "legacy-scope-revise"},
        )
        self.assertEqual(revised.status_code, 201, revised.get_data(as_text=True))

        before_stale = self._db_dump()
        stale = self.client.post(
            revise_url,
            json=self._payload(
                {
                    "base_revision": 1,
                    "operations": [
                        {
                            **operation,
                            "volume_db": -6,
                        }
                    ],
                    "apply": True,
                }
            ),
            headers={
                **self.auth,
                "Idempotency-Key": "legacy-scope-stale-revision",
            },
        )
        self.assertEqual(stale.status_code, 409, stale.get_data(as_text=True))
        self.assertEqual(stale.get_json()["code"], "revision_conflict")
        self.assertEqual(self._db_dump(), before_stale)

    def test_additional_domain_writes_require_non_rebindable_idempotency_keys(
        self,
    ) -> None:
        audited = frozenset(
            {
                "flask_http.POST:/v1/creative/briefs",
                "flask_http.POST:/v1/creative/projects/<project_id>/timelines",
                "flask_http.POST:/v1/timelines/<timeline_id>/revise",
                "flask_http.PUT:/v1/creator/profile",
                "flask_http.PUT:/v1/inbox/assets/<asset_id>",
            }
        )

        def assert_missing(method: str, url: str, payload: dict[str, object]) -> None:
            before = self._db_dump()
            response = self.client.open(
                url,
                method=method,
                json=self._payload(payload),
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["code"], "invalid_idempotency_key")
            self.assertEqual(self._db_dump(), before)

        def assert_rebound(
            method: str,
            url: str,
            *,
            key: str,
            first: dict[str, object],
            rebound: dict[str, object],
            accepted_status: int,
        ):
            headers = {**self.auth, "Idempotency-Key": key}
            accepted = self.client.open(
                url,
                method=method,
                json=self._payload(first),
                headers=headers,
            )
            self.assertEqual(
                accepted.status_code,
                accepted_status,
                accepted.get_data(as_text=True),
            )
            before = self._db_dump()
            denied = self.client.open(
                url,
                method=method,
                json=self._payload(rebound),
                headers=headers,
            )
            self.assertEqual(denied.status_code, 409, denied.get_data(as_text=True))
            self.assertEqual(denied.get_json()["code"], "idempotency_conflict")
            self.assertEqual(self._db_dump(), before)
            return accepted

        image_state = self.repository.canonical_image_consumer_states(
            [self.asset_id]
        )[self.asset_id]
        observation = image_state.get("canonical_image_observation")
        self.assertIsInstance(observation, dict)
        brief = {
            "goal": "domain scope oracle image",
            "candidate_refs": [self.asset_id],
            "candidate_observations": [observation],
        }
        assert_missing("POST", "/v1/creative/briefs", brief)
        assert_rebound(
            "POST",
            "/v1/creative/briefs",
            key="rebound-creative-brief",
            first=brief,
            rebound={**brief, "goal": "distinct domain scope command"},
            accepted_status=201,
        )

        current_profile = self.app.extensions[
            "creator_memory_service"
        ].current_profile()
        profile_base = int(current_profile["revision"])
        profile = {
            "base_revision": profile_base,
            "profile": {"tone": "domain-rebound-one"},
            "evidence": [],
            "source": "user_edit",
        }
        assert_missing("PUT", "/v1/creator/profile", profile)
        assert_rebound(
            "PUT",
            "/v1/creator/profile",
            key="rebound-creator-profile",
            first=profile,
            rebound={**profile, "profile": {"tone": "domain-rebound-two"}},
            accepted_status=200,
        )

        current_review = self.repository.get_asset_review(self.asset_id)
        assert current_review is not None
        review = {
            "base_revision": int(current_review["revision"]),
            "favorite": not bool(current_review["favorite"]),
        }
        review_url = f"/v1/inbox/assets/{self.asset_id}"
        assert_missing("PUT", review_url, review)
        assert_rebound(
            "PUT",
            review_url,
            key="rebound-asset-review",
            first=review,
            rebound={**review, "note": "distinct review command"},
            accepted_status=200,
        )

        source = self.repository.available_sources(self.asset_id)[0]
        project = self.repository.create_project(
            "Legacy idempotency scope",
            {
                "duration_ms": 3_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": self.asset_id,
                        "asset_id": self.asset_id,
                        "asset_source_id": source["asset_source_id"],
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "domain_scope_oracle", "external_model": False},
        )
        timeline_url = f"/v1/creative/projects/{project['id']}/timelines"
        timeline_request = {"brief_revision": 1}
        assert_missing("POST", timeline_url, timeline_request)
        created = assert_rebound(
            "POST",
            timeline_url,
            key="rebound-legacy-timeline",
            first=timeline_request,
            rebound={**timeline_request, "brief_revision": 2},
            accepted_status=201,
        )
        timeline = created.get_json()["timeline"]
        timeline_id = str(timeline["id"])
        clip_id = str(timeline["tracks"][0]["clips"][0]["id"])
        revise_url = f"/v1/timelines/{timeline_id}/revise"
        revision_request = {
            "base_revision": 1,
            "operations": [
                {
                    "op": "set_volume",
                    "clip_id": clip_id,
                    "volume_db": -3,
                    "preconditions": {"timeline_revision": 1},
                }
            ],
            "apply": True,
        }
        assert_missing("POST", revise_url, revision_request)
        assert_rebound(
            "POST",
            revise_url,
            key="rebound-legacy-revision",
            first=revision_request,
            rebound={
                **revision_request,
                "operations": [
                    {
                        **revision_request["operations"][0],
                        "volume_db": -6,
                    }
                ],
            },
            accepted_status=201,
        )

        self.assertEqual(
            audited,
            frozenset(
                {
                    "flask_http.POST:/v1/creative/briefs",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timelines",
                    "flask_http.POST:/v1/timelines/<timeline_id>/revise",
                    "flask_http.PUT:/v1/creator/profile",
                    "flask_http.PUT:/v1/inbox/assets/<asset_id>",
                }
            ),
        )


class BackendCanonicalDomainScopeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-canonical-domain-scope-"
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
        self.token = "canonical-domain-scope-token"
        self._fixture_build_serial = 0

        app = Flask("canonical-domain-scope-inventory")
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
            self, scoped_name, content
        )

    def _build_project(self) -> tuple[str, dict[str, object], list[str]]:
        self._fixture_build_serial += 1
        return timeline_fixture.TimelineLoweringApiTests._build_project(self)

    def _advance_coverage(self, project_id: str) -> dict[str, object]:
        return timeline_fixture.TimelineLoweringApiTests._advance_coverage(
            self, project_id
        )

    def _db_dump(self) -> tuple[str, ...]:
        with closing(sqlite3.connect(self.db_path)) as connection:
            return tuple(connection.iterdump())

    @property
    def headers(self) -> dict[str, str]:
        return {DESKTOP_TOKEN_HEADER: self.token}

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
    def _timeline_restore_payload(
        materialize: dict[str, object],
        current: dict[str, object],
    ) -> dict[str, object]:
        selected = current["selected_revision"]
        assert isinstance(selected, dict)
        return {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": current["head"],
            "restore_from": {
                key: value for key, value in selected.items() if key != "is_head"
            },
        }

    def _post_without_key(
        self,
        url: str,
        payload: dict[str, object],
    ):
        before = self._db_dump()
        response = self.client.post(
            url,
            query_string=self.query,
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["code"], "invalid_idempotency_key")
        self.assertEqual(self._db_dump(), before)
        return response

    def _post_stale(
        self,
        url: str,
        payload: dict[str, object],
        key: str,
    ):
        before = self._db_dump()
        response = self.client.post(
            url,
            query_string=self.query,
            json=payload,
            headers={**self.headers, "Idempotency-Key": key},
        )
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertEqual(self._db_dump(), before)
        return response

    def _post_rebound(
        self,
        url: str,
        *,
        key: str,
        first: dict[str, object],
        rebound: dict[str, object],
    ) -> None:
        headers = {**self.headers, "Idempotency-Key": key}
        accepted = self.client.post(
            url,
            query_string=self.query,
            json=first,
            headers=headers,
        )
        self.assertIn(
            accepted.status_code,
            {200, 201},
            accepted.get_data(as_text=True),
        )
        before_rebound = self._db_dump()
        denied = self.client.post(
            url,
            query_string=self.query,
            json=rebound,
            headers=headers,
        )
        self.assertEqual(denied.status_code, 409, denied.get_data(as_text=True))
        code = str(denied.get_json().get("code") or "")
        self.assertIn("conflict", code)
        self.assertEqual(self._db_dump(), before_rebound)

    def test_all_canonical_domain_writes_require_idempotency_before_transaction(self) -> None:
        project_id, materialize, _source_ids = self._build_project()
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        commit_payload = {
            "expected_head": self._blueprint_ref(blueprint),
            "initial_legacy_brief": None,
            "source_candidate_sha256": None,
            "semantic": semantic("canonical missing idempotency"),
        }
        restore_blueprint_payload = {
            "expected_head": self._blueprint_ref(blueprint),
            "restore_from": self._blueprint_ref(blueprint),
        }
        cases_before_timeline = {
            "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/commit": (
                f"/v1/creative/projects/{project_id}/blueprint/commit",
                commit_payload,
            ),
            "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/restore": (
                f"/v1/creative/projects/{project_id}/blueprint/restore",
                restore_blueprint_payload,
            ),
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/materialize": (
                f"/v1/creative/projects/{project_id}/timeline/materialize",
                materialize,
            ),
        }
        requested: set[str] = set()
        for action_id, (url, payload) in cases_before_timeline.items():
            with self.subTest(action_id=action_id):
                requested.add(action_id)
                self._post_without_key(url, payload)

        materialized = self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string=self.query,
            json=materialize,
            headers={**self.headers, "Idempotency-Key": "canonical-scope-materialize"},
        )
        self.assertEqual(materialized.status_code, 201, materialized.get_data(as_text=True))
        current = self.timelines.read(project_id)
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        edit_payload = {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": current["head"],
            "edit": {
                "op": "set_clip_duration",
                "clip_id": clip["clip_id"],
                "duration_ms": 1_400,
            },
        }
        timeline_cases = {
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/edit": (
                f"/v1/creative/projects/{project_id}/timeline/edit",
                edit_payload,
            ),
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/structural-edit": (
                f"/v1/creative/projects/{project_id}/timeline/structural-edit",
                {
                    "expected_blueprint": materialize["expected_blueprint"],
                    "expected_coverage": materialize["expected_coverage"],
                    "expected_timeline_head": current["head"],
                    "structural_edit": {
                        "op": "delete_clip",
                        "clip_id": clip["clip_id"],
                    },
                },
            ),
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/reconcile": (
                f"/v1/creative/projects/{project_id}/timeline/reconcile",
                {
                    "expected_blueprint": materialize["expected_blueprint"],
                    "expected_coverage": materialize["expected_coverage"],
                    "expected_timeline_head": current["head"],
                },
            ),
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/restore": (
                f"/v1/creative/projects/{project_id}/timeline/restore",
                self._timeline_restore_payload(materialize, current),
            ),
        }
        for action_id, (url, payload) in timeline_cases.items():
            with self.subTest(action_id=action_id):
                requested.add(action_id)
                self._post_without_key(url, payload)
        self.assertEqual(frozenset(requested), CANONICAL_IDEMPOTENCY_ACTIONS)

    def test_canonical_resource_heads_and_revisions_are_cas_bound_with_zero_partial_write(
        self,
    ) -> None:
        project_id, materialize, _source_ids = self._build_project()
        blueprint = self.repository.get_blueprint_head(project_id)
        coverage = self.repository.get_coverage_head(project_id)
        assert blueprint is not None and coverage is not None
        stale_blueprint = {
            "revision": blueprint["revision"],
            "content_sha256": "0" * 64,
        }
        self._post_stale(
            f"/v1/creative/projects/{project_id}/blueprint/commit",
            {
                "expected_head": stale_blueprint,
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic("stale blueprint commit"),
            },
            "canonical-stale-blueprint-commit",
        )
        self._post_stale(
            f"/v1/creative/projects/{project_id}/blueprint/restore",
            {
                "expected_head": stale_blueprint,
                "restore_from": self._blueprint_ref(blueprint),
            },
            "canonical-stale-blueprint-restore",
        )
        self._post_stale(
            f"/v1/creative/projects/{project_id}/coverage/materialize",
            {
                "expected_blueprint": {
                    key: materialize["expected_blueprint"][key]
                    for key in ("revision", "content_sha256", "semantic_sha256")
                },
                "expected_plan_head": None,
            },
            "canonical-stale-coverage",
        )

        materialized = self.client.post(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            query_string=self.query,
            json=materialize,
            headers={**self.headers, "Idempotency-Key": "canonical-head-winner"},
        )
        self.assertEqual(materialized.status_code, 201, materialized.get_data(as_text=True))
        current = self.timelines.read(project_id)
        timeline = current["timeline"]
        head = current["head"]
        assert isinstance(timeline, dict) and isinstance(head, dict)

        self._post_stale(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            materialize,
            "canonical-stale-materialize",
        )
        stale_head = {**head, "timeline_content_sha256": "0" * 64}
        clip = timeline["tracks"][0]["clips"][0]
        shared = {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": stale_head,
        }
        self._post_stale(
            f"/v1/creative/projects/{project_id}/timeline/edit",
            {
                **shared,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_450,
                },
            },
            "canonical-stale-edit",
        )
        self._post_stale(
            f"/v1/creative/projects/{project_id}/timeline/structural-edit",
            {
                **shared,
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": clip["clip_id"],
                },
            },
            "canonical-stale-structural-edit",
        )
        self._post_stale(
            f"/v1/creative/projects/{project_id}/timeline/reconcile",
            shared,
            "canonical-stale-reconcile",
        )
        restore_payload = self._timeline_restore_payload(materialize, current)
        restore_payload["expected_timeline_head"] = stale_head
        self._post_stale(
            f"/v1/creative/projects/{project_id}/timeline/restore",
            restore_payload,
            "canonical-stale-restore",
        )
        self.assertEqual(
            CANONICAL_SCOPE_ACTIONS,
            frozenset(
                {
                    "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/commit",
                    "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/restore",
                    "flask_http.POST:/v1/creative/projects/<project_id>/coverage/materialize",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timeline/edit",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timeline/structural-edit",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timeline/materialize",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timeline/reconcile",
                    "flask_http.POST:/v1/creative/projects/<project_id>/timeline/restore",
                }
            ),
        )

    def test_canonical_idempotency_keys_cannot_rebind_to_distinct_commands(self) -> None:
        requested: set[str] = set()

        project_id, _materialize, _source_ids = self._build_project()
        blueprint = self.repository.get_blueprint_head(project_id)
        assert blueprint is not None
        commit_base = {
            "expected_head": self._blueprint_ref(blueprint),
            "initial_legacy_brief": None,
            "source_candidate_sha256": None,
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/blueprint/commit",
            key="rebound-blueprint-commit",
            first={**commit_base, "semantic": semantic("rebound commit one")},
            rebound={**commit_base, "semantic": semantic("rebound commit two")},
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/commit"
        )

        project_id, _materialize, _source_ids = self._build_project()
        head_one = self.repository.get_blueprint_head(project_id)
        assert head_one is not None
        self.blueprints.commit_proposal(
            project_id,
            {
                "expected_head": self._blueprint_ref(head_one),
                "initial_legacy_brief": None,
                "source_candidate_sha256": None,
                "semantic": semantic("restore successor"),
            },
            idempotency_key="rebound-restore-parent",
        )
        head_two = self.repository.get_blueprint_head(project_id)
        assert head_two is not None
        restore_base = {
            "expected_head": self._blueprint_ref(head_two),
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/blueprint/restore",
            key="rebound-blueprint-restore",
            first={**restore_base, "restore_from": self._blueprint_ref(head_one)},
            rebound={**restore_base, "restore_from": self._blueprint_ref(head_two)},
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/blueprint/restore"
        )

        project_id, materialize, _source_ids = self._build_project()
        changed_materialize = {
            **materialize,
            "expected_blueprint": {
                **materialize["expected_blueprint"],
                "content_sha256": "0" * 64,
            },
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/timeline/materialize",
            key="rebound-timeline-materialize",
            first=materialize,
            rebound=changed_materialize,
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/materialize"
        )

        project_id, materialize, _source_ids = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            materialize,
            idempotency_key="rebound-edit-parent",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_id)
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        edit_base = {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": current["head"],
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/timeline/edit",
            key="rebound-timeline-edit",
            first={
                **edit_base,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_400,
                },
            },
            rebound={
                **edit_base,
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_450,
                },
            },
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/edit"
        )

        project_id, materialize, _source_ids = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            materialize,
            idempotency_key="rebound-structural-parent",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_id)
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clips = timeline["tracks"][0]["clips"]
        self.assertGreaterEqual(len(clips), 2)
        structural_base = {
            "expected_blueprint": materialize["expected_blueprint"],
            "expected_coverage": materialize["expected_coverage"],
            "expected_timeline_head": current["head"],
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/timeline/structural-edit",
            key="rebound-timeline-structural-edit",
            first={
                **structural_base,
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": clips[0]["clip_id"],
                },
            },
            rebound={
                **structural_base,
                "structural_edit": {
                    "op": "delete_clip",
                    "clip_id": clips[1]["clip_id"],
                },
            },
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/structural-edit"
        )

        project_id, materialize, _source_ids = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            materialize,
            idempotency_key="rebound-reconcile-parent",
            expected_database_uuid=self.repository.database_uuid,
        )
        self._advance_coverage(project_id)
        current = self.timelines.read(project_id)
        blueprint = self.repository.get_blueprint_head(project_id)
        coverage = self.repository.get_coverage_head(project_id)
        assert blueprint is not None and coverage is not None
        reconcile = {
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
            "expected_timeline_head": current["head"],
        }
        rebound_reconcile = {
            **reconcile,
            "expected_timeline_head": {
                **reconcile["expected_timeline_head"],
                "timeline_content_sha256": "0" * 64,
            },
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/timeline/reconcile",
            key="rebound-timeline-reconcile",
            first=reconcile,
            rebound=rebound_reconcile,
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/reconcile"
        )

        project_id, materialize, _source_ids = self._build_project()
        self.timelines.materialize_first_cut(
            project_id,
            materialize,
            idempotency_key="rebound-restore-materialize",
            expected_database_uuid=self.repository.database_uuid,
        )
        revision_one = self.timelines.read(project_id, revision=1)
        current = self.timelines.read(project_id)
        timeline = current["timeline"]
        assert isinstance(timeline, dict)
        clip = timeline["tracks"][0]["clips"][0]
        self.timelines.apply_edit(
            project_id,
            {
                "expected_blueprint": materialize["expected_blueprint"],
                "expected_coverage": materialize["expected_coverage"],
                "expected_timeline_head": current["head"],
                "edit": {
                    "op": "set_clip_duration",
                    "clip_id": clip["clip_id"],
                    "duration_ms": 1_400,
                },
            },
            idempotency_key="rebound-restore-parent-edit",
            expected_database_uuid=self.repository.database_uuid,
        )
        current = self.timelines.read(project_id)
        first_restore = self._timeline_restore_payload(
            materialize,
            {
                **revision_one,
                "head": current["head"],
            },
        )
        current_selected = current["selected_revision"]
        assert isinstance(current_selected, dict)
        rebound_restore = {
            **first_restore,
            "restore_from": {
                key: value
                for key, value in current_selected.items()
                if key != "is_head"
            },
        }
        self._post_rebound(
            f"/v1/creative/projects/{project_id}/timeline/restore",
            key="rebound-timeline-restore",
            first=first_restore,
            rebound=rebound_restore,
        )
        requested.add(
            "flask_http.POST:/v1/creative/projects/<project_id>/timeline/restore"
        )

        self.assertEqual(frozenset(requested), CANONICAL_IDEMPOTENCY_ACTIONS)

    def test_coverage_idempotency_key_is_required_and_non_rebindable(self) -> None:
        project_id, materialize, _source_ids = self._build_project()
        coverage = self.repository.get_coverage_head(project_id)
        assert coverage is not None
        payload = {
            "expected_blueprint": {
                key: materialize["expected_blueprint"][key]
                for key in ("revision", "content_sha256", "semantic_sha256")
            },
            "expected_plan_head": {
                "revision": coverage["revision"],
                "content_sha256": coverage["content_sha256"],
            },
        }
        url = f"/v1/creative/projects/{project_id}/coverage/materialize"
        self._post_without_key(url, payload)
        rebound = {
            **payload,
            "expected_plan_head": {
                **payload["expected_plan_head"],
                "content_sha256": "0" * 64,
            },
        }
        self._post_rebound(
            url,
            key="rebound-coverage-materialize",
            first=payload,
            rebound=rebound,
        )


if __name__ == "__main__":
    unittest.main()
