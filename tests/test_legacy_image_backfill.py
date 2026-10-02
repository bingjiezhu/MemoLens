from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from backend.src.media.legacy_image_backfill import (
    LegacyImageBackfillError,
    LegacyImageBackfillService,
    _LegacyCandidate,
)
from core.config import Settings
from core.db import ImageIndexRepository
from core.image_analysis_contract import canonical_sha256
from core.media_db import MediaRepository


NOW = "2026-08-29T00:00:00Z"


class LegacyImageBackfillRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-legacy-image-backfill-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.desktop_token = "legacy-backfill-desktop-token"
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.db_path),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_NETWORK_PROFILE": "offline",
                "EMBEDDING_BACKEND": "semantic_hash",
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
        self.app.testing = True
        self.client = self.app.test_client()
        self.bundle = self.app.extensions["runtime_manager"].current_bundle
        self.repository = self.bundle.extension("media_repository")
        self.runner = self.bundle.extension("media_job_runner")
        self.root_id = str(self.repository.library_roots()[0]["id"])
        self.url = "/v1/image-analysis/legacy-backfill-batches"

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()

    @staticmethod
    def _image_bytes(color: tuple[int, int, int]) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (20, 12), color).save(buffer, format="PNG")
        return buffer.getvalue()

    def _seed_legacy(
        self,
        relative_path: str,
        *,
        color: tuple[int, int, int] = (30, 60, 90),
        stored_sha256: str | None = None,
        write_source: bool = True,
        legacy_id: str | None = None,
    ) -> tuple[str, str]:
        content = self._image_bytes(color)
        if write_source:
            path = self.library / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        row_sha256 = stored_sha256 or actual_sha256
        row_id = legacy_id or f"legacy_{row_sha256[:20]}"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO image_index(
                       id,sha256,filename,relative_path,mime_type,file_size,
                       width,height,taken_at,lat,lon,altitude,place_name,country,
                       description,tags_json,combined_text,text_embedding_model,
                       combined_text_embedding,embedding_backend,embedding,
                       aesthetic_score,aesthetic_model,technical_quality_score,
                       aesthetic_updated_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,
                          ?,?,?,NULL,NULL,?,?,NULL,NULL,NULL,NULL,?,?)""",
                (
                    row_id,
                    row_sha256,
                    Path(relative_path).name,
                    relative_path,
                    "image/png",
                    len(content),
                    20,
                    12,
                    "legacy description must not cross authority",
                    '["legacy-tag"]',
                    "legacy combined text",
                    "legacy-provider",
                    b"legacy-vector",
                    NOW,
                    NOW,
                ),
            )
        return row_id, actual_sha256

    def _headers(self, key: str = "legacy-backfill-once") -> dict[str, str]:
        return {
            DESKTOP_TOKEN_HEADER: self.desktop_token,
            "Idempotency-Key": key,
        }

    def _payload(self, **overrides: object) -> dict[str, object]:
        return {
            "library_root_id": self.root_id,
            "limit": 50,
            "dry_run": False,
            **overrides,
        }

    def _import_canonical(self, relative_path: str, *, key: str):
        return self.client.post(
            "/v1/assets/import",
            json={
                "db_path": str(self.db_path),
                "library_root_id": self.root_id,
                "relative_paths": [relative_path],
                "recursive": False,
                "dry_run": False,
                "kinds": ["image"],
            },
            headers=self._headers(key),
        )

    def _canonical_counts(self) -> dict[str, int]:
        tables = (
            "assets",
            "asset_sources",
            "analysis_runs",
            "media_jobs",
            "image_analysis_job_bindings",
            "image_analysis_results",
            "image_analysis_heads",
            "legacy_image_aliases",
            "image_projection_generations",
        )
        with closing(sqlite3.connect(self.db_path)) as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                )
                for table in tables
            }

    def _wait_terminal(
        self,
        job_id: str,
        *,
        timeout: float = 10.0,
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.repository.get_media_job(job_id)
            assert job is not None
            if job["status"] in {"cancelled", "failed", "succeeded"}:
                return job
            time.sleep(0.01)
        self.fail(f"legacy backfill job did not finish: {job_id}")

    def _assert_one_current_consumer_identity(
        self,
        *,
        asset_sha256: str,
        canonical_asset_id: str,
        legacy_id: str,
        expected_intended_revisions: tuple[int, ...],
    ) -> None:
        with self.repository.transaction() as connection:
            asset_rows = connection.execute(
                "SELECT id FROM assets WHERE sha256=? ORDER BY id",
                (asset_sha256,),
            ).fetchall()
            physical_rows = connection.execute(
                "SELECT id FROM image_index WHERE sha256=? ORDER BY id",
                (asset_sha256,),
            ).fetchall()
            active_projection_rows = connection.execute(
                """SELECT projection.asset_id
                     FROM image_projection_generations generation
                     JOIN image_projection_rows projection
                       ON projection.generation_id=generation.id
                    WHERE generation.is_active=1
                      AND generation.status='complete'
                      AND projection.asset_id=?""",
                (canonical_asset_id,),
            ).fetchall()
            bindings = connection.execute(
                """SELECT binding.job_id,binding.asset_id,
                          binding.intended_revision,binding.enqueue_scope
                     FROM image_analysis_job_bindings binding
                    WHERE binding.asset_id=?
                    ORDER BY binding.intended_revision,binding.job_id""",
                (canonical_asset_id,),
            ).fetchall()
            aliases = connection.execute(
                """SELECT canonical_asset_id FROM legacy_image_aliases
                    WHERE database_uuid=? AND legacy_id=?""",
                (self.repository.database_uuid, legacy_id),
            ).fetchall()

        self.assertEqual([str(row["id"]) for row in asset_rows], [canonical_asset_id])
        self.assertEqual([str(row["id"]) for row in physical_rows], [canonical_asset_id])
        self.assertEqual(
            [str(row["asset_id"]) for row in active_projection_rows],
            [canonical_asset_id],
        )
        self.assertEqual(
            tuple(int(row["intended_revision"]) for row in bindings),
            expected_intended_revisions,
        )
        self.assertTrue(
            all(str(row["asset_id"]) == canonical_asset_id for row in bindings)
        )
        self.assertEqual(
            [str(row["canonical_asset_id"]) for row in aliases],
            [canonical_asset_id],
        )
        resolved = self.repository.resolve_image_asset_reference(
            legacy_id,
            library_root_id=self.root_id,
        )
        assert resolved is not None
        self.assertEqual(resolved["reference_kind"], "legacy_alias")
        self.assertEqual(resolved["asset_id"], canonical_asset_id)
        self.assertEqual(resolved["projection"]["status"], "current")

    def test_backfill_rehashes_then_queues_canonical_job_without_reusing_legacy_analysis(
        self,
    ) -> None:
        legacy_id, actual_sha256 = self._seed_legacy("album/source.png")

        with patch.object(self.runner, "submit") as submit:
            response = self.client.post(
                self.url,
                json=self._payload(),
                headers=self._headers(),
            )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.headers["Idempotency-Replayed"], "false")
        self.assertEqual(response.json["status"], "queued")
        self.assertEqual(response.json["candidate_count"], 1)
        self.assertFalse(response.json["legacy_metadata_reused"])
        self.assertNotIn("album/source.png", response.get_data(as_text=True))
        self.assertNotIn("legacy description", response.get_data(as_text=True))
        candidate = response.json["candidates"][0]
        self.assertEqual(candidate["legacy_id"], legacy_id)
        self.assertEqual(candidate["asset_sha256"], actual_sha256)
        self.assertRegex(response.json["candidate_set_sha256"], r"^[0-9a-f]{64}$")
        submit.assert_called_once_with(response.json["job_id"])

        counts = self._canonical_counts()
        self.assertEqual(
            (
                counts["assets"],
                counts["asset_sources"],
                counts["analysis_runs"],
                counts["media_jobs"],
                counts["image_analysis_job_bindings"],
            ),
            (1, 1, 1, 1, 1),
        )
        self.assertEqual(
            (
                counts["image_analysis_results"],
                counts["image_analysis_heads"],
                counts["legacy_image_aliases"],
                counts["image_projection_generations"],
            ),
            (0, 0, 0, 0),
        )
        with self.repository.transaction() as connection:
            legacy = connection.execute(
                "SELECT description,tags_json,embedding_backend FROM image_index WHERE id=?",
                (legacy_id,),
            ).fetchone()
            binding = connection.execute(
                """SELECT enqueue_scope,library_root_id,relative_path,
                          input_asset_sha256
                     FROM image_analysis_job_bindings"""
            ).fetchone()
        self.assertIsNotNone(legacy, "legacy row must survive until projection succeeds")
        assert legacy is not None and binding is not None
        self.assertEqual(legacy["embedding_backend"], "legacy-provider")
        self.assertEqual(
            tuple(binding),
            (
                "legacy-image-backfill/image-analysis",
                self.root_id,
                "album/source.png",
                actual_sha256,
            ),
        )

    def test_replay_survives_source_removal_without_new_job_or_attempt(self) -> None:
        self._seed_legacy("replay.png")
        headers = self._headers("replay-frozen-response")
        with patch.object(self.runner, "submit"):
            first = self.client.post(self.url, json=self._payload(), headers=headers)
        before = self._canonical_counts()
        (self.library / "replay.png").unlink()

        with patch.object(self.runner, "submit") as replay_submit:
            replay = self.client.post(self.url, json=self._payload(), headers=headers)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, first.json)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(self._canonical_counts(), before)
        replay_submit.assert_called_once_with(first.json["job_id"])

    def test_successful_worker_projection_mints_exact_alias_then_removes_legacy_row(
        self,
    ) -> None:
        legacy_id, _ = self._seed_legacy(
            "projected.png",
            color=(120, 80, 40),
            legacy_id="historic-projectable-reference",
        )

        response = self.client.post(
            self.url,
            json=self._payload(),
            headers=self._headers("project-and-alias"),
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        job = self._wait_terminal(str(response.json["job_id"]))
        self.assertEqual(
            (job["status"], job["stage"]),
            ("succeeded", "completed"),
            job,
        )

        with self.repository.transaction() as connection:
            alias = connection.execute(
                """SELECT canonical_asset_id,library_root_id,source_id
                     FROM legacy_image_aliases WHERE legacy_id=?""",
                (legacy_id,),
            ).fetchone()
            legacy = connection.execute(
                "SELECT 1 FROM image_index WHERE id=?",
                (legacy_id,),
            ).fetchone()
            canonical = connection.execute(
                "SELECT id FROM image_index WHERE id=?",
                (response.json["candidates"][0]["asset_id"],),
            ).fetchone()
        self.assertIsNotNone(alias)
        assert alias is not None
        self.assertEqual(alias["library_root_id"], self.root_id)
        self.assertIsNone(legacy)
        self.assertIsNotNone(canonical)
        resolved = self.repository.resolve_image_asset_reference(
            legacy_id,
            library_root_id=self.root_id,
        )
        assert resolved is not None
        self.assertEqual(resolved["reference_kind"], "legacy_alias")
        self.assertEqual(
            resolved["asset_id"],
            response.json["candidates"][0]["asset_id"],
        )
        self.assertEqual(resolved["projection"]["status"], "current")

    def test_canonical_first_then_legacy_ingest_keeps_one_consumer_identity(
        self,
    ) -> None:
        relative_path = "ordering/canonical-first.png"
        content = self._image_bytes((110, 40, 170))
        source = self.library / relative_path
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
        asset_sha256 = hashlib.sha256(content).hexdigest()

        canonical = self._import_canonical(
            relative_path,
            key="canonical-first-import",
        )
        self.assertEqual(canonical.status_code, 202, canonical.get_data(as_text=True))
        canonical_job_id = str(canonical.json["job_id"])
        canonical_job = self._wait_terminal(canonical_job_id)
        self.assertEqual(
            (canonical_job["status"], canonical_job["stage"]),
            ("succeeded", "completed"),
            canonical_job,
        )
        canonical_asset_id = str(canonical.json["assets"][0]["id"])
        self.assertEqual(canonical_asset_id, f"asset_{asset_sha256[:24]}")

        # Controlled legacy-ingest fixture: the retired writer historically
        # replaced the same-path physical row.  Canonical ledger/generation
        # state remains intact so backfill must converge it, not mint identity.
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "DELETE FROM image_index WHERE id=? AND relative_path=?",
                (canonical_asset_id, relative_path),
            )
        legacy_id, observed_sha256 = self._seed_legacy(
            relative_path,
            color=(110, 40, 170),
            legacy_id="legacy-canonical-first",
        )
        self.assertEqual(observed_sha256, asset_sha256)
        headers = self._headers("canonical-first-backfill")
        backfill = self.client.post(self.url, json=self._payload(), headers=headers)
        self.assertEqual(backfill.status_code, 202, backfill.get_data(as_text=True))
        backfill_job_id = str(backfill.json["job_id"])
        self.assertNotEqual(backfill_job_id, canonical_job_id)
        backfill_job = self._wait_terminal(backfill_job_id)
        self.assertEqual(
            (backfill_job["status"], backfill_job["stage"]),
            ("succeeded", "completed"),
            backfill_job,
        )

        replay = self.client.post(self.url, json=self._payload(), headers=headers)
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replay.json, backfill.json)
        empty = self.client.post(
            self.url,
            json=self._payload(),
            headers=self._headers("canonical-first-after-alias"),
        )
        self.assertEqual(empty.status_code, 200)
        self.assertEqual((empty.json["status"], empty.json["candidate_count"]), ("empty", 0))

        self._assert_one_current_consumer_identity(
            asset_sha256=asset_sha256,
            canonical_asset_id=canonical_asset_id,
            legacy_id=legacy_id,
            expected_intended_revisions=(1, 2),
        )

    def test_legacy_first_then_canonical_import_keeps_one_consumer_identity(
        self,
    ) -> None:
        relative_path = "ordering/legacy-first.png"
        legacy_id, asset_sha256 = self._seed_legacy(
            relative_path,
            color=(25, 135, 85),
            legacy_id="legacy-first-canonical-second",
        )
        backfill_headers = self._headers("legacy-first-backfill")
        backfill = self.client.post(
            self.url,
            json=self._payload(),
            headers=backfill_headers,
        )
        self.assertEqual(backfill.status_code, 202, backfill.get_data(as_text=True))
        backfill_job_id = str(backfill.json["job_id"])
        backfill_job = self._wait_terminal(backfill_job_id)
        self.assertEqual(
            (backfill_job["status"], backfill_job["stage"]),
            ("succeeded", "completed"),
            backfill_job,
        )
        canonical_asset_id = str(backfill.json["candidates"][0]["asset_id"])
        self.assertEqual(canonical_asset_id, f"asset_{asset_sha256[:24]}")

        canonical = self._import_canonical(
            relative_path,
            key="legacy-first-canonical-import",
        )
        self.assertEqual(canonical.status_code, 202, canonical.get_data(as_text=True))
        canonical_job_id = str(canonical.json["job_id"])
        self.assertNotEqual(canonical_job_id, backfill_job_id)
        canonical_job = self._wait_terminal(canonical_job_id)
        self.assertEqual(
            (canonical_job["status"], canonical_job["stage"]),
            ("succeeded", "completed"),
            canonical_job,
        )
        replay = self._import_canonical(
            relative_path,
            key="legacy-first-canonical-import",
        )
        self.assertEqual(replay.status_code, 202)
        self.assertEqual(replay.json, canonical.json)

        self._assert_one_current_consumer_identity(
            asset_sha256=asset_sha256,
            canonical_asset_id=canonical_asset_id,
            legacy_id=legacy_id,
            expected_intended_revisions=(1, 2),
        )

    def test_concurrent_and_repeated_backfill_reuses_one_publication_intent(
        self,
    ) -> None:
        legacy_id, asset_sha256 = self._seed_legacy(
            "ordering/concurrent.png",
            color=(205, 95, 35),
            legacy_id="legacy-concurrent",
        )
        payload = self._payload()

        def post_once(key: str):
            with self.app.test_client() as client:
                return client.post(
                    self.url,
                    json=payload,
                    headers=self._headers(key),
                )

        with patch.object(self.runner, "submit") as submit:
            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(post_once, "concurrent-backfill-a")
                second_future = executor.submit(post_once, "concurrent-backfill-b")
                first = first_future.result(timeout=10)
                second = second_future.result(timeout=10)

            self.assertEqual(first.status_code, 202, first.get_data(as_text=True))
            self.assertEqual(second.status_code, 202, second.get_data(as_text=True))
            self.assertEqual(first.json["job_id"], second.json["job_id"])
            self.assertEqual(
                first.json["candidates"][0]["asset_id"],
                second.json["candidates"][0]["asset_id"],
            )
            frozen = first.json
            for _ in range(100):
                replay = self.client.post(
                    self.url,
                    json=payload,
                    headers=self._headers("concurrent-backfill-a"),
                )
                self.assertEqual(replay.status_code, 202)
                self.assertEqual(replay.headers["Idempotency-Replayed"], "true")
                self.assertEqual(replay.json, frozen)

        canonical_asset_id = str(first.json["candidates"][0]["asset_id"])
        self.assertEqual(canonical_asset_id, f"asset_{asset_sha256[:24]}")
        counts = self._canonical_counts()
        self.assertEqual(
            (
                counts["assets"],
                counts["asset_sources"],
                counts["analysis_runs"],
                counts["media_jobs"],
                counts["image_analysis_job_bindings"],
            ),
            (1, 1, 1, 1, 1),
        )
        with self.repository.transaction() as connection:
            binding = connection.execute(
                """SELECT asset_id,intended_revision,enqueue_scope
                     FROM image_analysis_job_bindings"""
            ).fetchone()
            asset_ids = connection.execute(
                "SELECT id FROM assets WHERE sha256=?",
                (asset_sha256,),
            ).fetchall()
            legacy = connection.execute(
                "SELECT id FROM image_index WHERE id=?",
                (legacy_id,),
            ).fetchone()
        assert binding is not None
        self.assertEqual(
            tuple(binding),
            (
                canonical_asset_id,
                1,
                "legacy-image-backfill/image-analysis",
            ),
        )
        self.assertEqual([str(row["id"]) for row in asset_ids], [canonical_asset_id])
        self.assertIsNotNone(legacy, "queued work must not consume legacy input early")
        self.assertGreaterEqual(submit.call_count, 102)

    def test_missing_and_sha_mismatch_block_before_any_canonical_write(self) -> None:
        cases = (
            ("missing.png", None, False, "legacy_backfill_missing_source"),
            ("mismatch.png", "f" * 64, True, "legacy_backfill_sha_mismatch"),
        )
        for index, (relative, stored_sha, write_source, expected_code) in enumerate(cases):
            with self.subTest(expected_code=expected_code):
                self._seed_legacy(
                    relative,
                    stored_sha256=stored_sha,
                    write_source=write_source,
                    legacy_id=f"legacy-blocked-{index}",
                )
                before = self._canonical_counts()
                with patch.object(self.runner, "submit") as submit:
                    response = self.client.post(
                        self.url,
                        json=self._payload(limit=1),
                        headers=self._headers(f"blocked-{index}"),
                    )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json["code"], expected_code)
                self.assertEqual(self._canonical_counts(), before)
                submit.assert_not_called()
                with self.repository.transaction() as connection:
                    self.assertEqual(
                        connection.execute(
                            "SELECT COUNT(*) FROM image_index WHERE id=?",
                            (f"legacy-blocked-{index}",),
                        ).fetchone()[0],
                        1,
                    )
                with self.repository.transaction(immediate=True) as connection:
                    connection.execute(
                        "DELETE FROM image_index WHERE id=?",
                        (f"legacy-blocked-{index}",),
                    )

    def test_replaced_approved_root_blocks_before_canonical_write_or_submit(
        self,
    ) -> None:
        self._seed_legacy("root-replaced.png")
        before = self._canonical_counts()
        original = self.root / "library-original"
        self.library.rename(original)
        self.library.mkdir()
        (self.library / "root-replaced.png").write_bytes(
            self._image_bytes((200, 10, 40))
        )

        with patch.object(self.runner, "submit") as submit:
            response = self.client.post(
                self.url,
                json=self._payload(),
                headers=self._headers("replaced-approved-root"),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["code"], "legacy_backfill_root_unavailable")
        self.assertEqual(self._canonical_counts(), before)
        submit.assert_not_called()
        root_record = self.repository.library_root(self.root_id)
        assert root_record is not None
        self.assertEqual(root_record["status"], "unavailable")

    def test_dry_run_and_opaque_cursor_are_path_free_and_write_no_domain_rows(self) -> None:
        first_id, _ = self._seed_legacy(
            "private/first.png",
            color=(10, 20, 30),
            legacy_id="legacy-a",
        )
        second_id, _ = self._seed_legacy(
            "private/second.png",
            color=(40, 50, 60),
            legacy_id="legacy-b",
        )
        before = self._canonical_counts()

        first = self.client.post(
            self.url,
            json=self._payload(limit=1, dry_run=True),
            headers=self._headers("dry-page-one"),
        )
        cursor = first.json["next_cursor"]
        second = self.client.post(
            self.url,
            json=self._payload(limit=1, dry_run=True, cursor=cursor),
            headers=self._headers("dry-page-two"),
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json["candidates"][0]["legacy_id"], first_id)
        self.assertEqual(second.json["candidates"][0]["legacy_id"], second_id)
        self.assertTrue(first.json["has_more"])
        self.assertFalse(second.json["has_more"])
        self.assertIsInstance(cursor, str)
        self.assertNotIn("private", cursor)
        self.assertNotIn("private/first.png", first.get_data(as_text=True))
        self.assertEqual(self._canonical_counts(), before)

        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        invalid = self.client.post(
            self.url,
            json=self._payload(limit=1, dry_run=True, cursor=tampered),
            headers=self._headers("dry-invalid-cursor"),
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json["code"], "invalid_legacy_backfill_cursor")

    def test_kind_alias_and_ambiguous_conflicts_are_typed_and_write_nothing(
        self,
    ) -> None:
        kind_id, _ = self._seed_legacy(
            "kind-conflict.png",
            legacy_id="legacy-kind-conflict",
        )
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE image_index SET mime_type='video/mp4' WHERE id=?",
                (kind_id,),
            )
        before_kind = self._canonical_counts()
        kind = self.client.post(
            self.url,
            json=self._payload(limit=1),
            headers=self._headers("kind-conflict"),
        )
        self.assertEqual(kind.status_code, 409)
        self.assertEqual(kind.json["code"], "legacy_backfill_kind_conflict")
        self.assertEqual(self._canonical_counts(), before_kind)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM image_index WHERE id=?", (kind_id,))

        alias_id, alias_sha256 = self._seed_legacy(
            "alias-conflict.png",
            color=(75, 25, 125),
            legacy_id="legacy-alias-conflict",
        )
        observed = (self.library / "alias-conflict.png").stat()
        canonical = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path="alias-conflict.png",
            filename="alias-conflict.png",
            kind="image",
            sha256=alias_sha256,
            mime_type="image/png",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        identity = {
            "database_uuid": self.repository.database_uuid,
            "legacy_id": alias_id,
            "canonical_asset_id": str(canonical["id"]),
            "library_root_id": self.root_id,
            "source_id": str(canonical["asset_source_id"]),
            "alias_scope": "legacy-image-index/v1",
        }
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO legacy_image_aliases(
                       database_uuid,legacy_id,canonical_asset_id,library_root_id,
                       source_id,alias_scope,alias_sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    identity["database_uuid"],
                    identity["legacy_id"],
                    identity["canonical_asset_id"],
                    identity["library_root_id"],
                    identity["source_id"],
                    identity["alias_scope"],
                    canonical_sha256(identity),
                    NOW,
                ),
            )
        before_alias = self._canonical_counts()
        alias = self.client.post(
            self.url,
            json=self._payload(limit=1),
            headers=self._headers("alias-conflict"),
        )
        self.assertEqual(alias.status_code, 409)
        self.assertEqual(alias.json["code"], "legacy_alias_conflict")
        self.assertEqual(self._canonical_counts(), before_alias)

        ambiguous_id, ambiguous_sha256 = self._seed_legacy(
            "ambiguous.png",
            color=(15, 45, 75),
            legacy_id="legacy-ambiguous",
        )
        candidate = _LegacyCandidate(
            legacy_id=ambiguous_id,
            asset_sha256=ambiguous_sha256,
            relative_path="ambiguous.png",
            mime_type="image/png",
            source_binding_sha256=hashlib.sha256(
                f"{self.root_id}\0ambiguous.png\0{ambiguous_sha256}".encode()
            ).hexdigest(),
        )
        service = LegacyImageBackfillService(
            self.repository,
            self.runner,
            runtime_generation=self.bundle.generation_id,
            database_file_identity=self.repository.bind_image_database_identity(),
        )
        with self.assertRaisesRegex(
            LegacyImageBackfillError,
            "More than one legacy row claims",
        ) as ambiguous:
            service._validate_candidate_rows_before_filesystem(
                (candidate, candidate),
                library_root_id=self.root_id,
            )
        self.assertEqual(
            ambiguous.exception.code,
            "legacy_backfill_ambiguous_source",
        )
        after_ambiguous_seed = self._canonical_counts()
        self.assertEqual(after_ambiguous_seed, before_alias)

    def test_byte_identical_different_paths_fail_before_competing_publication_intents(
        self,
    ) -> None:
        legacy_id, asset_sha256 = self._seed_legacy(
            "first-location.png",
            legacy_id="legacy-byte-identical-a",
        )
        first = _LegacyCandidate(
            legacy_id=legacy_id,
            asset_sha256=asset_sha256,
            relative_path="first-location.png",
            mime_type="image/png",
            source_binding_sha256=hashlib.sha256(
                f"{self.root_id}\0first-location.png\0{asset_sha256}".encode()
            ).hexdigest(),
        )
        second = _LegacyCandidate(
            legacy_id="legacy-byte-identical-b",
            asset_sha256=asset_sha256,
            relative_path="second-location.png",
            mime_type="image/png",
            source_binding_sha256=hashlib.sha256(
                f"{self.root_id}\0second-location.png\0{asset_sha256}".encode()
            ).hexdigest(),
        )
        service = LegacyImageBackfillService(
            self.repository,
            self.runner,
            runtime_generation=self.bundle.generation_id,
            database_file_identity=self.repository.bind_image_database_identity(),
        )
        before = self._canonical_counts()

        with self.assertRaisesRegex(
            LegacyImageBackfillError,
            "competing publication intents",
        ) as blocked:
            service._validate_candidate_rows_before_filesystem(
                (first, second),
                library_root_id=self.root_id,
            )

        self.assertEqual(blocked.exception.code, "legacy_backfill_ambiguous_source")
        self.assertEqual(self._canonical_counts(), before)
        self.assertEqual(before["analysis_runs"], 0)
        self.assertEqual(before["media_jobs"], 0)
        self.assertEqual(before["image_analysis_job_bindings"], 0)

    def test_closed_body_idempotency_and_desktop_authority_fail_before_dispatch(
        self,
    ) -> None:
        self._seed_legacy("authority.png")
        with patch(
            "backend.src.api.routes.LegacyImageBackfillService.run",
            side_effect=AssertionError("unauthorized request reached backfill service"),
        ) as run:
            unauthorized = self.client.post(
                self.url,
                data=b"{not-json",
                content_type="application/json",
            )
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.json["code"], "desktop_auth_required")
        run.assert_not_called()

        no_key = self.client.post(
            self.url,
            json=self._payload(),
            headers={DESKTOP_TOKEN_HEADER: self.desktop_token},
        )
        unknown = self.client.post(
            self.url,
            json=self._payload(relative_path="authority.png"),
            headers=self._headers("closed-body"),
        )
        self.assertEqual(no_key.status_code, 400)
        self.assertEqual(no_key.json["code"], "invalid_idempotency_key")
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json["code"], "invalid_legacy_backfill_request")


class LegacyImageStartupAuthorityTests(unittest.TestCase):
    def test_v14_reopen_does_not_promote_legacy_rows_into_canonical_facts(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="memolens-no-startup-legacy-promotion-"
        ) as temporary:
            root = Path(temporary).resolve()
            library = root / "library"
            library.mkdir()
            content = LegacyImageBackfillRouteTests._image_bytes((90, 70, 50))
            (library / "historic.png").write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            db_path = root / "state" / "media.db"
            db_path.parent.mkdir()
            ImageIndexRepository(db_path).ensure_schema()
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    """INSERT INTO image_index(
                           id,sha256,filename,relative_path,mime_type,file_size,
                           width,height,taken_at,lat,lon,altitude,place_name,country,
                           description,tags_json,combined_text,text_embedding_model,
                           combined_text_embedding,embedding_backend,embedding,
                           aesthetic_score,aesthetic_model,technical_quality_score,
                           aesthetic_updated_at,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,
                              ?,?,?,NULL,NULL,?,?,NULL,NULL,NULL,NULL,?,?)""",
                    (
                        "legacy-startup-row",
                        digest,
                        "historic.png",
                        "historic.png",
                        "image/png",
                        len(content),
                        20,
                        12,
                        "untrusted legacy description",
                        "[]",
                        "untrusted legacy combined text",
                        "legacy-provider",
                        b"legacy-vector",
                        NOW,
                        NOW,
                    ),
                )
                connection.commit()

            repository = MediaRepository(db_path)
            try:
                repository.ensure_schema(library)
                with repository.transaction() as connection:
                    counts = tuple(
                        connection.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        for table in ("image_index", "assets", "asset_sources")
                    )
            finally:
                repository.close()
            self.assertEqual(counts, (1, 0, 0))


if __name__ == "__main__":
    unittest.main()
