from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.src import DESKTOP_TOKEN_HEADER, create_app
from backend.src.media.creator_memory import (
    CreatorMemoryService,
    ProfileRevisionConflictError,
)
from backend.src.media.director import CreativeDirector
from backend.src.media.inbox import MediaInboxService, ReviewRevisionConflictError
from backend.src.media.retrieval import MixedRetrievalService
from core.config import Settings
from core.db import ImageIndexRepository
from core.media_db import MediaRepository, V2_CHECKSUM
from core.photo_atlas import PhotoAtlasService


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def initialize_repository(root: Path) -> tuple[MediaRepository, Path, Path]:
    library = root / "library"
    library.mkdir(parents=True)
    db_path = root / "state" / "media.db"
    db_path.parent.mkdir(parents=True)
    ImageIndexRepository(db_path).ensure_schema()
    repository = MediaRepository(db_path)
    repository.ensure_schema(library)
    return repository, library, db_path


def register_asset(
    repository: MediaRepository,
    library: Path,
    relative_path: str,
    content: bytes,
    *,
    kind: str = "image",
) -> dict[str, object]:
    path = library / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    stat_result = path.stat()
    root_id = str(repository.library_roots()[0]["id"])
    asset = repository.upsert_asset_source(
        root_id=root_id,
        relative_path=relative_path,
        filename=path.name,
        kind=kind,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/jpeg" if kind == "image" else "video/mp4",
        file_size=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        source_file_id=str(stat_result.st_ino),
    )
    if kind == "image":
        repository.update_image_probe(str(asset["id"]), width=640, height=480)
    return asset


def add_legacy_image_record(
    db_path: Path,
    asset: dict[str, object],
    *,
    description: str = "Mountain morning creator footage",
) -> None:
    now = "2026-08-12T12:00:00+00:00"
    vector = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT INTO image_index(
                   id,sha256,filename,relative_path,mime_type,file_size,width,height,taken_at,
                   lat,lon,altitude,place_name,country,description,tags_json,combined_text,
                   text_embedding_model,combined_text_embedding,embedding_backend,embedding,
                   created_at,updated_at)
               SELECT a.id,a.sha256,s.display_filename,s.relative_path,a.mime_type,a.file_size,
                      a.width,a.height,?,NULL,NULL,NULL,NULL,NULL,?,'["mountain"]',?,NULL,NULL,
                      'fixture',?,?,?
                 FROM assets a JOIN asset_sources s ON s.asset_id=a.id AND s.is_preferred=1
                WHERE a.id=?""",
            (now, description, description, vector, now, now, asset["id"]),
        )


def add_video_analysis(repository: MediaRepository, asset_id: str) -> str:
    repository.update_asset_probe(
        asset_id,
        {
            "duration_ms": 2_000,
            "width": 1080,
            "height": 1920,
            "rotation_degrees": 0,
            "captured_at": "2026-08-12T13:00:00+00:00",
            "codec": {"video_codec": "fixture", "audio_streams": []},
        },
    )
    job = repository.create_analysis_job(asset_id=asset_id)
    segment_id = "segment-inbox-video"
    keyframe_id = "keyframe-inbox-video"
    repository.commit_video_analysis(
        job_id=str(job["id"]),
        segments=[
            {
                "id": segment_id,
                "ordinal": 0,
                "start_ms": 0,
                "end_ms": 2_000,
                "boundary_reason": "fixture",
                "summary": "Mountain creator video",
                "semantic": {"tags": ["mountain"]},
                "combined_text": "Mountain creator video",
                "visual_status": "local_fallback",
                "transcript_status": "unavailable",
                "confidence": None,
            }
        ],
        keyframes=[
            {
                "id": keyframe_id,
                "segment_id": segment_id,
                "timestamp_ms": 1_000,
                "cache_key": "keyframes/inbox-video.jpg",
                "sha256": "a" * 64,
                "width": 1080,
                "height": 1920,
                "selection_reason": "representative",
                "is_representative": True,
            }
        ],
        transcripts=[],
    )
    return keyframe_id


def shutdown_app(app) -> None:
    for name in ("media_job_runner", "render_job_runner"):
        runner = app.extensions.get(name)
        if runner is not None:
            runner.shutdown()


class SchemaV3ContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-schema-v3-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = initialize_repository(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _downgrade_fixture_to_v2(self) -> str:
        database_uuid = self.repository.database_uuid
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("DROP VIEW current_asset_reviews")
            connection.execute("DROP INDEX idx_asset_reviews_state_created")
            connection.execute("DROP INDEX idx_creator_profiles_created")
            connection.execute("DROP TABLE asset_review_revisions")
            connection.execute("DROP TABLE creator_profile_revisions")
            connection.execute("DELETE FROM schema_migrations WHERE version=3")
            connection.execute("UPDATE database_meta SET schema_version=2 WHERE singleton=1")
        return database_uuid

    def test_v2_checksum_is_frozen_and_v2_upgrade_preserves_database_identity(self) -> None:
        self.assertEqual(
            V2_CHECKSUM,
            "8563c16d6ccb95c36e13abf4143a888b109e8a6606da9744c64307755f052654",
        )
        asset = register_asset(self.repository, self.library, "before-upgrade.jpg", b"before-upgrade")
        database_uuid = self._downgrade_fixture_to_v2()

        self.repository.ensure_schema(self.library)

        self.assertEqual(self.repository.database_uuid, database_uuid)
        self.assertIsNotNone(self.repository.get_asset(str(asset["id"])))
        with sqlite3.connect(self.db_path) as connection:
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta").fetchone(),
                (3,),
            )
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM schema_migrations WHERE version=3"
                ).fetchone(),
                ("creator_memory_media_inbox",),
            )

    def test_failed_v3_ddl_rolls_back_every_change(self) -> None:
        database_uuid = self._downgrade_fixture_to_v2()
        broken = (
            "CREATE TABLE should_rollback(value TEXT)",
            "CREATE TABLE syntactically_broken(",
        )
        with patch("core.media_db.V3_SCHEMA_STATEMENTS", broken), self.assertRaises(sqlite3.Error):
            self.repository.ensure_schema(self.library)

        with sqlite3.connect(self.db_path) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute("SELECT schema_version,database_uuid FROM database_meta").fetchone(),
                (2, database_uuid),
            )
            self.assertIsNone(
                connection.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone()
            )


class MediaInboxContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-inbox-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = initialize_repository(self.root)
        self.service = MediaInboxService(self.repository)
        self.image = register_asset(self.repository, self.library, "creator.jpg", b"creator-image")
        add_legacy_image_record(self.db_path, self.image)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _put(
        self,
        asset_id: str,
        payload: dict[str, object],
        key: str,
    ) -> tuple[dict[str, object], bool]:
        return self.service.update_review(
            asset_id,
            payload,
            idempotency_scope=f"desktop:PUT:/v1/inbox/assets/{asset_id}",
            idempotency_key=key,
            request_sha256=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )

    def test_default_review_summary_cursor_and_video_representative_thumbnail(self) -> None:
        video = register_asset(
            self.repository,
            self.library,
            "creator.mp4",
            b"creator-video",
            kind="video",
        )
        keyframe_id = add_video_analysis(self.repository, str(video["id"]))
        first_page = self.service.list_assets(state="inbox", kinds="image,video", limit=1)
        second_page = self.service.list_assets(
            state="inbox",
            kinds="image,video",
            limit=1,
            cursor=first_page.next_cursor,
        )

        self.assertEqual(first_page.summary, {"inbox": 2, "kept": 0, "archived": 0, "all": 2})
        self.assertTrue(first_page.has_more)
        self.assertFalse(second_page.has_more)
        self.assertEqual(
            {item["id"] for item in [*first_page.items, *second_page.items]},
            {self.image["id"], video["id"]},
        )
        video_item = next(
            item for item in [*first_page.items, *second_page.items] if item["kind"] == "video"
        )
        self.assertEqual(video_item["thumbnail_url"], f"/v1/keyframes/{keyframe_id}")
        self.assertEqual(video_item["review"]["revision"], 0)

    def test_archive_exits_default_photo_video_atlas_and_undo_restores(self) -> None:
        atlas = PhotoAtlasService(ImageIndexRepository(self.db_path))
        atlas.rebuild()
        self.assertEqual(atlas.overview()["asset_count"], 1)

        archived, replayed = self._put(
            str(self.image["id"]),
            {"base_revision": 0, "inbox_state": "archived", "favorite": True},
            "archive-image",
        )
        self.assertFalse(replayed)
        self.assertEqual(archived["revision"], 1)
        self.assertEqual(self.repository.mixed_candidates()[0], [])
        explicit, _ = self.repository.mixed_candidates(include_archived=True)
        self.assertEqual(explicit[0]["review"]["inbox_state"], "archived")
        self.assertEqual(ImageIndexRepository(self.db_path).fetch_candidates(), [])
        self.assertEqual(atlas.overview()["asset_count"], 0)
        generated = atlas.generate(
            text="Use this archived mountain moment",
            top_k=1,
            asset_ids=[str(self.image["id"])],
        )
        self.assertEqual(generated["data"][0]["review"]["inbox_state"], "archived")
        self.assertTrue(generated["data"][0]["review"]["favorite"])

        replay, replayed = self._put(
            str(self.image["id"]),
            {"base_revision": 0, "inbox_state": "archived", "favorite": True},
            "archive-image",
        )
        self.assertTrue(replayed)
        self.assertEqual(replay, archived)
        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM asset_review_revisions WHERE asset_id=?",
                    (self.image["id"],),
                ).fetchone()[0],
                1,
            )

        restored, _ = self._put(
            str(self.image["id"]),
            {"base_revision": 1, "inbox_state": "kept"},
            "restore-image",
        )
        self.assertEqual(restored["revision"], 2)
        self.assertTrue(restored["favorite"])
        self.assertEqual(len(self.repository.mixed_candidates()[0]), 1)
        self.assertEqual(len(ImageIndexRepository(self.db_path).fetch_candidates()), 1)
        self.assertEqual(atlas.overview()["asset_count"], 1)

    def test_path_rebind_gets_new_content_identity_and_fresh_inbox_state(self) -> None:
        self._put(
            str(self.image["id"]),
            {"base_revision": 0, "inbox_state": "archived"},
            "archive-before-rebind",
        )
        replacement = register_asset(
            self.repository,
            self.library,
            "creator.jpg",
            b"new-content-at-same-path",
        )
        self.assertNotEqual(replacement["id"], self.image["id"])
        self.assertEqual(self.service.get_review(str(replacement["id"]))["revision"], 0)
        self.assertEqual(self.service.get_review(str(replacement["id"]))["inbox_state"], "inbox")
        self.assertEqual(self.service.get_review(str(self.image["id"]))["inbox_state"], "archived")

    def test_review_cas_and_response_freeze_share_one_transaction(self) -> None:
        payload = {"base_revision": 0, "inbox_state": "kept"}
        with patch.object(
            MediaRepository,
            "_idempotency_store_success",
            side_effect=RuntimeError("injected response freeze failure"),
        ), self.assertRaisesRegex(RuntimeError, "injected"):
            self._put(str(self.image["id"]), payload, "atomic-review")

        self.assertEqual(self.service.get_review(str(self.image["id"]))["revision"], 0)
        with self.repository._connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM idempotency_records").fetchone()[0], 0)
        saved, _ = self._put(str(self.image["id"]), payload, "atomic-review")
        self.assertEqual(saved["revision"], 1)
        with self.assertRaises(ReviewRevisionConflictError) as conflict:
            self._put(
                str(self.image["id"]),
                {"base_revision": 0, "favorite": True},
                "stale-review",
            )
        self.assertEqual(conflict.exception.current_review["revision"], 1)


class CreatorMemoryContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-creator-memory-")
        self.root = Path(self.temporary.name).resolve()
        self.repository, self.library, self.db_path = initialize_repository(self.root)
        self.service = CreatorMemoryService(self.repository)
        self.counter = 0

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _put(self, payload: dict[str, object]) -> tuple[dict[str, object], bool]:
        self.counter += 1
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return self.service.update_profile(
            payload,
            idempotency_scope="desktop:PUT:/v1/creator/profile",
            idempotency_key=f"profile-{self.counter}",
            request_sha256=request_hash,
        )

    def test_partial_full_form_clear_and_reset_are_canonical_revisions(self) -> None:
        first, _ = self._put(
            {
                "base_revision": 0,
                "profile": {
                    "platform": "",
                    "audience": "",
                    "duration_ms": None,
                    "aspect_ratio": "",
                    "tone": "warm",
                    "pace": "",
                    "narrative_arc": "",
                    "must_include": [],
                    "must_exclude": [],
                },
                "source": "user_edit",
            }
        )
        self.assertEqual(first["profile"], {"tone": "warm"})
        cleared, _ = self._put(
            {
                "base_revision": 1,
                "profile": {
                    "platform": "",
                    "audience": "",
                    "duration_ms": None,
                    "aspect_ratio": "",
                    "tone": "",
                    "pace": "",
                    "narrative_arc": "",
                    "must_include": [],
                    "must_exclude": [],
                },
                "source": "user_edit",
            }
        )
        self.assertEqual(cleared["profile"], {})
        reset, _ = self._put(
            {"base_revision": 2, "profile": {}, "source": "reset"}
        )
        self.assertEqual(reset["revision"], 3)
        self.assertEqual(reset["source"], "reset")
        self.assertNotEqual(first["content_sha256"], cleared["content_sha256"])
        self.assertEqual(cleared["content_sha256"], reset["content_sha256"])

    def test_suggestions_require_two_independent_projects_and_get_never_writes(self) -> None:
        one = self.repository.create_project(
            "One",
            {"tone": "warm", "aspect_ratio": "9:16", "platform": "Xiaohongshu"},
            {"created_by": "test"},
        )
        self.assertEqual(self.service.suggestions(), [])
        two = self.repository.create_project(
            "Two",
            {"tone": "warm", "aspect_ratio": "9:16", "platform": "Xiaohongshu"},
            {"created_by": "test"},
        )
        with self.repository._connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM creator_profile_revisions").fetchone()[0]
        suggestions = self.service.suggestions()
        with self.repository._connect() as connection:
            after = connection.execute("SELECT COUNT(*) FROM creator_profile_revisions").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual({item["field"] for item in suggestions}, {"tone", "aspect_ratio", "platform"})
        self.assertTrue(all(item["evidence_count"] == 2 for item in suggestions))

        tone = next(item for item in suggestions if item["field"] == "tone")
        confirmed, _ = self._put(
            {
                "base_revision": 0,
                "profile": {"tone": tone["value"]},
                "evidence": tone["evidence"],
                "source": "confirmed_suggestion",
            }
        )
        self.assertEqual(confirmed["evidence"], tone["evidence"])
        self.assertEqual(
            {item["project_id"] for item in confirmed["evidence"]},
            {one["id"], two["id"]},
        )
        with self.assertRaises(ProfileRevisionConflictError):
            self._put({"base_revision": 0, "profile": {"pace": "fast"}, "source": "user_edit"})

    def test_suggestions_ignore_values_inherited_from_creator_memory(self) -> None:
        inherited_brief = {
            "tone": "warm",
            "pace": "gentle",
            "applied_profile_fields": ["tone", "pace"],
        }
        self.repository.create_project(
            "Inherited one",
            inherited_brief,
            {"created_by": "test"},
        )
        self.repository.create_project(
            "Inherited two",
            inherited_brief,
            {"created_by": "test"},
        )

        self.assertEqual(self.service.suggestions(), [])

    def test_confirmed_suggestion_must_match_current_value_and_complete_evidence(self) -> None:
        first = self.repository.create_project(
            "First",
            {"tone": "warm", "platform": "Xiaohongshu"},
            {"created_by": "test"},
        )
        second = self.repository.create_project(
            "Second",
            {"tone": "warm", "platform": "Xiaohongshu"},
            {"created_by": "test"},
        )
        suggestion = next(item for item in self.service.suggestions() if item["field"] == "tone")

        with self.assertRaisesRegex(ValueError, "Only confirmed suggestions"):
            self._put(
                {
                    "base_revision": 0,
                    "profile": {"tone": "warm"},
                    "source": "user_edit",
                    "evidence": suggestion["evidence"],
                }
            )
        with self.assertRaisesRegex(ValueError, "not a current"):
            self._put(
                {
                    "base_revision": 0,
                    "profile": {"tone": "cold"},
                    "source": "confirmed_suggestion",
                    "evidence": suggestion["evidence"],
                }
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self._put(
                {
                    "base_revision": 0,
                    "profile": {"tone": "warm"},
                    "source": "confirmed_suggestion",
                    "evidence": suggestion["evidence"][:1],
                }
            )

        saved, _ = self._put(
            {
                "base_revision": 0,
                "profile": {"tone": "warm"},
                "source": "confirmed_suggestion",
                "evidence": list(reversed(suggestion["evidence"])),
            }
        )
        self.assertEqual(
            {item["project_id"] for item in saved["evidence"]},
            {first["id"], second["id"]},
        )

    def test_director_freezes_only_explicitly_applied_profile_fields(self) -> None:
        image = register_asset(self.repository, self.library, "mountain.jpg", b"mountain-image")
        add_legacy_image_record(self.db_path, image)
        profile, _ = self._put(
            {
                "base_revision": 0,
                "profile": {"tone": "warm", "aspect_ratio": "9:16", "pace": "gentle"},
                "source": "user_edit",
            }
        )
        reference = {
            "profile_id": profile["profile_id"],
            "revision": profile["revision"],
            "content_sha256": profile["content_sha256"],
        }
        director = CreativeDirector(
            self.repository,
            MixedRetrievalService(self.repository),
            self.service,
        )
        project, _ = director.create_brief(
            {
                "goal": "mountain",
                "candidate_refs": [str(image["id"])],
                "tone": "warm",
                "aspect_ratio": "9:16",
                "creator_profile_ref": reference,
                "applied_profile_fields": ["tone", "aspect_ratio"],
            }
        )
        self.assertEqual(project["brief"]["creator_profile_ref"], reference)
        self.assertEqual(project["brief"]["applied_profile_fields"], ["tone", "aspect_ratio"])
        self.assertEqual(project["brief"]["pace"], "balanced")
        with self.assertRaisesRegex(ValueError, "does not match"):
            director.create_brief(
                {
                    "goal": "mountain",
                    "candidate_refs": [str(image["id"])],
                    "tone": "cold",
                    "creator_profile_ref": reference,
                    "applied_profile_fields": ["tone"],
                }
            )


class CreatorMemoryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-creator-routes-")
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.token = "creator-memory-desktop-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(PROJECT_ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.root / "state" / "media.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.token,
                "MINIMAX_KEY": "",
                "OPENAI_API_KEY": "",
                "DASHSCOPE_API_KEY": "",
                "VERTEX_ACCESS_TOKEN": "",
                "GOOGLE_OAUTH_ACCESS_TOKEN": "",
            },
            clear=False,
        )
        self.environment.start()
        self.app = create_app(Settings.from_env())
        self.client = self.app.test_client()
        self.repository = self.app.extensions["media_repository"]
        self.asset = register_asset(self.repository, self.library, "route-image.jpg", b"route-image")

    def tearDown(self) -> None:
        shutdown_app(self.app)
        self.environment.stop()
        self.temporary.cleanup()

    def test_routes_enforce_binding_token_idempotency_and_return_path_safe_envelopes(self) -> None:
        inbox = self.client.get("/v1/inbox")
        self.assertEqual(inbox.status_code, 200)
        self.assertEqual(inbox.json["summary"], {"inbox": 1, "kept": 0, "archived": 0, "all": 1})
        self.assertEqual(inbox.json["data"][0]["review"]["revision"], 0)

        mismatch = self.client.get(
            "/v1/inbox",
            query_string={"db_path": str(self.root / "other.db")},
        )
        self.assertEqual(mismatch.status_code, 409)
        self.assertEqual(mismatch.json["code"], "database_binding_mismatch")

        path = f"/v1/inbox/assets/{self.asset['id']}"
        payload = {
            "db_path": str(self.repository.db_path),
            "base_revision": 0,
            "inbox_state": "kept",
        }
        unauthenticated = self.client.put(path, json=payload, headers={"Idempotency-Key": "route-review"})
        self.assertEqual(unauthenticated.status_code, 401)
        unauthenticated_mismatch = self.client.put(
            path,
            json={**payload, "db_path": str(self.root / "other.db")},
            headers={"Idempotency-Key": "route-review-mismatch"},
        )
        unauthenticated_missing = self.client.put(
            path,
            json={"base_revision": 0, "inbox_state": "kept"},
            headers={"Idempotency-Key": "route-review-missing"},
        )
        self.assertEqual(unauthenticated_mismatch.status_code, 401)
        self.assertEqual(unauthenticated_missing.status_code, 401)
        self.assertEqual(unauthenticated_mismatch.json["code"], "desktop_auth_required")
        self.assertEqual(unauthenticated_missing.json["code"], "desktop_auth_required")
        missing_binding = self.client.put(
            path,
            json={"base_revision": 0, "inbox_state": "kept"},
            headers={DESKTOP_TOKEN_HEADER: self.token, "Idempotency-Key": "missing-review-binding"},
        )
        self.assertEqual(missing_binding.status_code, 409)
        self.assertEqual(missing_binding.json["code"], "database_binding_required")
        no_key = self.client.put(path, json=payload, headers={DESKTOP_TOKEN_HEADER: self.token})
        self.assertEqual(no_key.status_code, 400)
        headers = {DESKTOP_TOKEN_HEADER: self.token, "Idempotency-Key": "route-review"}
        first = self.client.put(path, json=payload, headers=headers)
        replay = self.client.put(path, json=payload, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(first.json["review"]["revision"], 1)

        conflict = self.client.put(
            path,
            json={**payload, "favorite": True},
            headers=headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json["code"], "idempotency_conflict")
        stale = self.client.put(
            path,
            json={**payload, "favorite": True},
            headers={DESKTOP_TOKEN_HEADER: self.token, "Idempotency-Key": "stale-route-review"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["code"], "review_revision_conflict")

        profile_payload = {
            "db_path": str(self.repository.db_path),
            "base_revision": 0,
            "profile": {"tone": "warm", "duration_ms": None, "aspect_ratio": ""},
            "source": "user_edit",
        }
        profile_headers = {
            DESKTOP_TOKEN_HEADER: self.token,
            "Idempotency-Key": "route-profile",
        }
        missing_profile_binding = self.client.put(
            "/v1/creator/profile",
            json={key: value for key, value in profile_payload.items() if key != "db_path"},
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": "missing-profile-binding",
            },
        )
        self.assertEqual(missing_profile_binding.status_code, 409)
        self.assertEqual(missing_profile_binding.json["code"], "database_binding_required")
        profile = self.client.put("/v1/creator/profile", json=profile_payload, headers=profile_headers)
        profile_replay = self.client.put(
            "/v1/creator/profile",
            json=profile_payload,
            headers=profile_headers,
        )
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json, profile_replay.json)
        self.assertEqual(profile.json["profile"]["profile"], {"tone": "warm"})
        self.assertEqual(self.client.get("/v1/creator/profile").json, profile.json)
        suggestions = self.client.get("/v1/creator/profile/suggestions")
        self.assertEqual(suggestions.status_code, 200)
        self.assertEqual(suggestions.json["data"], [])

        serialized = json.dumps(
            {"inbox": inbox.json, "profile": profile.json, "suggestions": suggestions.json},
            sort_keys=True,
        )
        self.assertNotIn(str(self.root), serialized)
        self.assertNotIn(self.repository.database_uuid, serialized)

    def test_confirmed_suggestion_exact_replay_precedes_current_state_validation(self) -> None:
        self.repository.create_project(
            "Suggestion one",
            {"tone": "warm"},
            {"created_by": "route-test"},
        )
        self.repository.create_project(
            "Suggestion two",
            {"tone": "warm"},
            {"created_by": "route-test"},
        )
        suggestions = self.client.get("/v1/creator/profile/suggestions")
        self.assertEqual(suggestions.status_code, 200)
        tone = next(
            item for item in suggestions.json["suggestions"] if item["field"] == "tone"
        )
        payload = {
            "db_path": str(self.repository.db_path),
            "profile_id": "default",
            "base_revision": 0,
            "profile": {"tone": "warm"},
            "evidence": tone["evidence"],
            "source": "confirmed_suggestion",
        }
        headers = {
            DESKTOP_TOKEN_HEADER: self.token,
            "Idempotency-Key": "confirmed-suggestion-route",
        }

        forged = self.client.put(
            "/v1/creator/profile",
            json={**payload, "evidence": tone["evidence"][:1]},
            headers={
                DESKTOP_TOKEN_HEADER: self.token,
                "Idempotency-Key": "forged-confirmed-suggestion",
            },
        )
        self.assertEqual(forged.status_code, 400)
        self.assertEqual(forged.json["code"], "invalid_creator_profile")

        first = self.client.put(
            "/v1/creator/profile",
            json=payload,
            headers=headers,
        )
        replay = self.client.put(
            "/v1/creator/profile",
            json=payload,
            headers=headers,
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, first.status_code)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(first.json["profile"]["revision"], 1)
        self.assertEqual(first.json["profile"]["source"], "confirmed_suggestion")

        different_hash = self.client.put(
            "/v1/creator/profile",
            json={**payload, "profile": {"tone": "warm", "pace": "fast"}},
            headers=headers,
        )
        self.assertEqual(different_hash.status_code, 409)
        self.assertEqual(different_hash.json["code"], "idempotency_conflict")

        with self.repository._connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM creator_profile_revisions WHERE profile_id='default'"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
