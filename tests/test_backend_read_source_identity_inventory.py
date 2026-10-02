from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from backend.src import DESKTOP_TOKEN_HEADER, create_app, shutdown_runtime_extensions
from backend.src.media.timeline import TimelineService
from core.config import Settings


ROOT = Path(__file__).resolve().parents[1]
DESKTOP_TOKEN = "read-source-inventory-desktop-token"
AUDITED_ACTION_IDS = frozenset(
    {
        "flask_http.GET:/v1/assets/<asset_id>/media",
        "flask_http.GET:/v1/assets/<asset_id>/thumbnail",
        "flask_http.GET:/v1/keyframes/<keyframe_id>",
        "flask_http.GET:/v1/library/files/<path:relative_path>",
        "flask_http.GET:/v1/library/previews/<path:relative_path>",
        "flask_http.GET:/v1/renders/<job_id>/download",
        "flask_http.GET:/v1/video-segments/<segment_id>/thumbnail",
    }
)


def _jpeg_bytes(color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 18), color).save(buffer, "JPEG", quality=95)
    return buffer.getvalue()


def _assert_blue_jpeg(test_case: unittest.TestCase, payload: bytes) -> None:
    with Image.open(BytesIO(payload)) as image:
        red, _green, blue = image.convert("RGB").getpixel((16, 9))
    test_case.assertGreater(blue, red)


def _register_source(repository, root_id: str, root: Path, relative_path: str, content: bytes, *, kind: str):
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    metadata = path.stat()
    return repository.upsert_asset_source(
        root_id=root_id,
        relative_path=relative_path,
        filename=path.name,
        kind=kind,
        sha256=hashlib.sha256(content).hexdigest(),
        mime_type="image/jpeg" if kind == "image" else "video/mp4",
        file_size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        source_file_id=str(metadata.st_ino),
    )


class BackendReadSourceIdentityInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-read-source-inventory-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.original_library_bytes = _jpeg_bytes("blue")
        self.replacement_bytes = _jpeg_bytes("red")
        self.outside_bytes = _jpeg_bytes("green")
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(ROOT / "config.yaml"),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library),
                "SQLITE_DB_PATH": str(self.root / "state" / "inventory.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": DESKTOP_TOKEN,
                "MEMOLENS_MAIN_AUTHORITY_TOKEN": "read-source-inventory-main-token",
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
        self.repository = self.app.extensions["media_repository"]
        self.root_id = str(self.repository.library_roots()[0]["id"])

        self.photo_path = self.library / "photo.jpg"
        photo = _register_source(
            self.repository,
            self.root_id,
            self.library,
            "photo.jpg",
            self.original_library_bytes,
            kind="image",
        )
        self.repository.update_image_probe(str(photo["id"]), width=32, height=18)
        self.photo_asset_id = str(photo["id"])
        self.photo_source_id = str(photo["asset_source_id"])

        self.video_path = self.library / "video.mp4"
        self.original_video_bytes = b"bounded synthetic video source"
        video = _register_source(
            self.repository,
            self.root_id,
            self.library,
            "video.mp4",
            self.original_video_bytes,
            kind="video",
        )
        self.video_asset_id = str(video["id"])
        self.repository.update_asset_probe(
            str(video["id"]),
            {
                "duration_ms": 2_000,
                "width": 320,
                "height": 180,
                "rotation_degrees": 0,
                "captured_at": None,
                "codec": {"video_codec": "fixture", "audio_streams": []},
            },
        )
        job = self.repository.create_analysis_job(asset_id=str(video["id"]))
        self.segment_id = "read-source-segment"
        self.keyframe_id = "read-source-keyframe"
        self.keyframe_path = (
            self.root / "state" / "media-cache" / "keyframes" / "read-source.jpg"
        )
        self.keyframe_path.parent.mkdir(parents=True, exist_ok=True)
        self.keyframe_path.write_bytes(self.original_library_bytes)
        self.repository.commit_video_analysis(
            job_id=str(job["id"]),
            segments=[
                {
                    "id": self.segment_id,
                    "ordinal": 0,
                    "start_ms": 0,
                    "end_ms": 2_000,
                    "boundary_reason": "source_identity_oracle",
                    "summary": "source identity fixture",
                    "semantic": {"tags": ["fixture"]},
                    "combined_text": "source identity fixture",
                    "visual_status": "local_fallback",
                    "transcript_status": "unavailable",
                    "confidence": None,
                }
            ],
            keyframes=[
                {
                    "id": self.keyframe_id,
                    "segment_id": self.segment_id,
                    "timestamp_ms": 1_000,
                    "cache_key": "keyframes/read-source.jpg",
                    "sha256": hashlib.sha256(self.original_library_bytes).hexdigest(),
                    "width": 32,
                    "height": 18,
                    "selection_reason": "source_identity_oracle",
                    "is_representative": True,
                }
            ],
            transcripts=[],
        )

        project = self.repository.create_project(
            "Read source identity fixture",
            {
                "schema_version": "1",
                "goal": "Read source identity fixture",
                "duration_ms": 1_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": self.photo_asset_id,
                        "asset_id": self.photo_asset_id,
                        "asset_source_id": self.photo_source_id,
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "source_identity_oracle", "external_model": False},
        )
        timeline = TimelineService(self.repository).create_from_project(
            str(project["id"])
        )
        with self.repository.transaction() as connection:
            preview_root = connection.execute(
                """SELECT id,canonical_path FROM output_roots
                   WHERE kind='app_preview' AND status='active'"""
            ).fetchone()
        assert preview_root is not None
        self.render_bytes = b"verified bounded render artifact"
        self.render_path = Path(str(preview_root["canonical_path"])) / "oracle.mp4"
        self.render_path.write_bytes(self.render_bytes)
        render_job = self.repository.create_render_job(
            timeline_id=str(timeline["timeline"]["id"]),
            timeline_revision=int(timeline["timeline"]["revision"]),
            profile="preview-low",
            output_root_id=str(preview_root["id"]),
            output_relative_path=self.render_path.name,
            timeline_content_sha256=str(timeline["content_sha256"]),
        )
        self.render_job_id = str(render_job["id"])
        self.repository.update_render_job(
            self.render_job_id,
            status="running",
            stage="render",
        )
        self.assertTrue(
            self.repository.complete_render_job_success(
                self.render_job_id,
                ffmpeg_version="oracle-fixture",
                output_sha256=hashlib.sha256(self.render_bytes).hexdigest(),
                size_bytes=len(self.render_bytes),
                duration_ms=1_000,
            )
        )

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary.cleanup()

    def test_all_backend_read_source_identity_actions_hold_verified_fds_and_fail_closed(self) -> None:
        action_urls = {
            "flask_http.GET:/v1/assets/<asset_id>/media": (
                f"/v1/assets/{self.video_asset_id}/media",
                {DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            ),
            "flask_http.GET:/v1/assets/<asset_id>/thumbnail": (
                f"/v1/assets/{self.photo_asset_id}/thumbnail",
                {},
            ),
            "flask_http.GET:/v1/keyframes/<keyframe_id>": (
                f"/v1/keyframes/{self.keyframe_id}",
                {},
            ),
            "flask_http.GET:/v1/library/files/<path:relative_path>": (
                "/v1/library/files/photo.jpg",
                {DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            ),
            "flask_http.GET:/v1/library/previews/<path:relative_path>": (
                "/v1/library/previews/photo.jpg",
                {},
            ),
            "flask_http.GET:/v1/renders/<job_id>/download": (
                f"/v1/renders/{self.render_job_id}/download",
                {DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            ),
            "flask_http.GET:/v1/video-segments/<segment_id>/thumbnail": (
                f"/v1/video-segments/{self.segment_id}/thumbnail",
                {},
            ),
        }
        self.assertEqual(frozenset(action_urls), AUDITED_ACTION_IDS)

        # Every audited production route resolves and returns only the verified
        # fixture before any adversarial replacement is introduced.
        for action_id, (url, headers) in action_urls.items():
            with self.subTest(action_id=action_id, phase="baseline"):
                response = self.client.get(url, headers=headers)
                self.assertEqual(response.status_code, 200)
                self.assertNotEqual(response.data, b"")

        # Every media/cache/artifact read is bound to the active database
        # before its repository opener can select a source object.
        database_bound_urls = {
            action_id: (url, headers)
            for action_id, (url, headers) in action_urls.items()
            if "/v1/library/" not in url
        }
        self.assertEqual(len(database_bound_urls), 5)
        with (
            patch.object(
                self.repository,
                "open_asset_file",
                side_effect=AssertionError("database mismatch reached asset open"),
            ),
            patch.object(
                self.repository,
                "open_render_artifact",
                side_effect=AssertionError("database mismatch reached artifact open"),
            ),
            patch(
                "backend.src.api.routes._open_keyframe_file",
                side_effect=AssertionError("database mismatch reached keyframe open"),
            ),
        ):
            for action_id, (url, headers) in database_bound_urls.items():
                separator = "&" if "?" in url else "?"
                with self.subTest(action_id=action_id, phase="database-mismatch"):
                    denied = self.client.get(
                        f"{url}{separator}db_path={self.root / 'other.db'}",
                        headers=headers,
                    )
                    self.assertEqual(denied.status_code, 409)
                    self.assertEqual(denied.json["code"], "database_binding_mismatch")

        with patch(
            "backend.src.api.routes._open_keyframe_file",
            side_effect=AssertionError("unknown video segment reached keyframe open"),
        ) as keyframe_open:
            unknown_segment = self.client.get(
                "/v1/video-segments/not-the-admitted-segment/thumbnail"
            )
        self.assertEqual(unknown_segment.status_code, 404)
        keyframe_open.assert_not_called()

        # A file pathname changed after verification cannot redirect either
        # streaming route: both responses keep reading the already-held inode.
        import backend.src.api.routes as routes

        original_open_library = routes._open_library_file

        def replace_library_file_after_open(relative_path: str, *, root_path_override: str | None):
            opened = original_open_library(
                relative_path,
                root_path_override=root_path_override,
            )
            held = self.library / "held-photo.jpg"
            self.photo_path.rename(held)
            self.photo_path.write_bytes(self.replacement_bytes)
            return opened

        with patch(
            "backend.src.api.routes._open_library_file",
            side_effect=replace_library_file_after_open,
        ):
            streamed = self.client.get(
                "/v1/library/files/photo.jpg",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
        self.assertEqual(streamed.status_code, 200)
        self.assertEqual(streamed.data, self.original_library_bytes)
        self.photo_path.unlink()
        (self.library / "held-photo.jpg").rename(self.photo_path)

        original_open_asset = self.repository.open_asset_file

        def replace_video_after_open(asset_id: str):
            opened = original_open_asset(asset_id)
            held = self.library / "held-video.mp4"
            self.video_path.rename(held)
            self.video_path.write_bytes(b"unverified replacement video source")
            return opened

        with patch.object(
            self.repository,
            "open_asset_file",
            side_effect=replace_video_after_open,
        ):
            streamed_video = self.client.get(
                f"/v1/assets/{self.video_asset_id}/media",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
        self.assertEqual(streamed_video.status_code, 200)
        self.assertEqual(streamed_video.data, self.original_video_bytes)
        self.video_path.unlink()
        (self.library / "held-video.mp4").rename(self.video_path)

        with patch(
            "backend.src.api.routes._open_library_file",
            side_effect=replace_library_file_after_open,
        ):
            preview = self.client.get("/v1/library/previews/photo.jpg")
        self.assertEqual(preview.status_code, 200)
        _assert_blue_jpeg(self, preview.data)
        self.photo_path.unlink()
        (self.library / "held-photo.jpg").rename(self.photo_path)

        def replace_asset_after_open(asset_id: str):
            opened = original_open_asset(asset_id)
            held = self.library / "held-photo.jpg"
            self.photo_path.rename(held)
            self.photo_path.write_bytes(self.replacement_bytes)
            return opened

        with patch.object(
            self.repository,
            "open_asset_file",
            side_effect=replace_asset_after_open,
        ):
            thumbnail = self.client.get(
                f"/v1/assets/{self.photo_asset_id}/thumbnail"
            )
        self.assertEqual(thumbnail.status_code, 200)
        _assert_blue_jpeg(self, thumbnail.data)
        self.photo_path.unlink()
        (self.library / "held-photo.jpg").rename(self.photo_path)

        original_open_render = self.repository.open_render_artifact

        def replace_render_after_open(job_id: str):
            opened = original_open_render(job_id)
            held = self.render_path.with_name("held-oracle.mp4")
            self.render_path.rename(held)
            self.render_path.write_bytes(b"unverified replacement render")
            return opened

        with patch.object(
            self.repository,
            "open_render_artifact",
            side_effect=replace_render_after_open,
        ):
            streamed_render = self.client.get(
                f"/v1/renders/{self.render_job_id}/download",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
        self.assertEqual(streamed_render.status_code, 200)
        self.assertEqual(streamed_render.data, self.render_bytes)
        self.render_path.unlink()
        self.render_path.with_name("held-oracle.mp4").rename(self.render_path)

        original_open_keyframe = routes._open_keyframe_file

        def replace_keyframe_after_open(keyframe_id: str):
            opened = original_open_keyframe(keyframe_id)
            held = self.keyframe_path.with_name("held-read-source.jpg")
            self.keyframe_path.rename(held)
            self.keyframe_path.write_bytes(self.replacement_bytes)
            return opened

        for url in (
            f"/v1/keyframes/{self.keyframe_id}",
            f"/v1/video-segments/{self.segment_id}/thumbnail",
        ):
            with self.subTest(url=url, phase="post-verify-file-replacement"), patch(
                "backend.src.api.routes._open_keyframe_file",
                side_effect=replace_keyframe_after_open,
            ):
                streamed = self.client.get(url)
                self.assertEqual(streamed.status_code, 200)
                self.assertEqual(streamed.data, self.original_library_bytes)
            self.keyframe_path.unlink()
            self.keyframe_path.with_name("held-read-source.jpg").rename(
                self.keyframe_path
            )

        # Replacing the registered runtime root at the same pathname changes
        # dev/ino.  Both legacy library reads reject it before streaming or
        # image decoding can observe replacement bytes.
        held_library = self.root / "held-library"
        self.library.rename(held_library)
        self.library.mkdir()
        (self.library / "photo.jpg").write_bytes(self.outside_bytes)
        try:
            with (
                patch(
                    "backend.src.api.routes._stream_verified_handle",
                    side_effect=AssertionError("replacement bytes reached streaming"),
                ),
                patch(
                    "backend.src.api.routes.Image.open",
                    side_effect=AssertionError("replacement bytes reached image decode"),
                ),
            ):
                denied_file = self.client.get(
                    "/v1/library/files/photo.jpg",
                    headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
                )
                denied_preview = self.client.get(
                    "/v1/library/previews/photo.jpg"
                )
            self.assertEqual(denied_file.status_code, 404)
            self.assertEqual(denied_preview.status_code, 404)
            self.assertNotIn(self.outside_bytes, denied_file.data)
            self.assertNotIn(self.outside_bytes, denied_preview.data)
        finally:
            (self.library / "photo.jpg").unlink()
            self.library.rmdir()
            held_library.rename(self.library)

        # A nested symlink cannot turn a runtime-library relative path into an
        # external source, even when the external file is a valid image.
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.jpg").write_bytes(self.outside_bytes)
        (self.library / "nested").symlink_to(outside, target_is_directory=True)
        with (
            patch(
                "backend.src.api.routes._stream_verified_handle",
                side_effect=AssertionError("external file reached streaming"),
            ),
            patch(
                "backend.src.api.routes.Image.open",
                side_effect=AssertionError("external file reached image decode"),
            ),
        ):
            denied_file = self.client.get(
                "/v1/library/files/nested/secret.jpg",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
            denied_preview = self.client.get(
                "/v1/library/previews/nested/secret.jpg"
            )
        self.assertEqual(denied_file.status_code, 404)
        self.assertEqual(denied_preview.status_code, 404)

        # An artifact with changed bytes cannot be downloaded even when its
        # path and size remain inside the app-managed output root.
        replacement_render = b"x" * len(self.render_bytes)
        self.render_path.write_bytes(replacement_render)
        with patch(
            "backend.src.api.routes._stream_verified_handle",
            side_effect=AssertionError("changed render reached streaming"),
        ):
            denied_render = self.client.get(
                f"/v1/renders/{self.render_job_id}/download",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
        self.assertEqual(denied_render.status_code, 404)
        self.assertNotIn(replacement_render, denied_render.data)
        self.render_path.write_bytes(self.render_bytes)

        # The render job may name only one root-relative basename; a corrupt
        # traversal binding is rejected before any external artifact is read.
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE render_jobs SET output_relative_path='../outside.mp4' WHERE id=?",
                (self.render_job_id,),
            )
        try:
            with patch(
                "backend.src.api.routes._stream_verified_handle",
                side_effect=AssertionError("out-of-job artifact reached streaming"),
            ):
                denied_render = self.client.get(
                    f"/v1/renders/{self.render_job_id}/download",
                    headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
                )
            self.assertEqual(denied_render.status_code, 404)
        finally:
            with self.repository.transaction(immediate=True) as connection:
                connection.execute(
                    "UPDATE render_jobs SET output_relative_path=? WHERE id=?",
                    (self.render_path.name, self.render_job_id),
                )

        # Managed-cache artifacts are content-bound and opened with O_NOFOLLOW.
        # Neither changed bytes nor an external same-content symlink may reach
        # a keyframe or segment-thumbnail response.
        self.keyframe_path.write_bytes(self.replacement_bytes)
        with patch(
            "backend.src.api.routes._stream_verified_handle",
            side_effect=AssertionError("changed keyframe reached streaming"),
        ):
            for url in (
                f"/v1/keyframes/{self.keyframe_id}",
                f"/v1/video-segments/{self.segment_id}/thumbnail",
            ):
                with self.subTest(url=url, phase="content-replacement"):
                    denied = self.client.get(url)
                    self.assertEqual(denied.status_code, 404)
                    self.assertNotIn(self.replacement_bytes, denied.data)

        self.keyframe_path.unlink()
        external_keyframe = outside / "external-keyframe.jpg"
        external_keyframe.write_bytes(self.original_library_bytes)
        self.keyframe_path.symlink_to(external_keyframe)
        with patch(
            "backend.src.api.routes._stream_verified_handle",
            side_effect=AssertionError("symlink keyframe reached streaming"),
        ):
            for url in (
                f"/v1/keyframes/{self.keyframe_id}",
                f"/v1/video-segments/{self.segment_id}/thumbnail",
            ):
                with self.subTest(url=url, phase="symlink-replacement"):
                    denied = self.client.get(url)
                    self.assertEqual(denied.status_code, 404)

        # The image-thumbnail route already shares MediaRepository's held-file
        # source opening.  An external same-content symlink is rejected before
        # Pillow can decode it.
        self.photo_path.unlink()
        external_photo = outside / "external-photo.jpg"
        external_photo.write_bytes(self.original_library_bytes)
        self.photo_path.symlink_to(external_photo)
        with patch(
            "backend.src.api.routes.Image.open",
            side_effect=AssertionError("symlink asset reached image decode"),
        ):
            denied_asset = self.client.get(
                f"/v1/assets/{self.photo_asset_id}/thumbnail"
            )
        self.assertEqual(denied_asset.status_code, 404)

        self.video_path.unlink()
        external_video = outside / "external-video.mp4"
        external_video.write_bytes(self.original_video_bytes)
        self.video_path.symlink_to(external_video)
        with patch(
            "backend.src.api.routes._stream_verified_handle",
            side_effect=AssertionError("symlink asset reached streaming"),
        ):
            denied_media = self.client.get(
                f"/v1/assets/{self.video_asset_id}/media",
                headers={DESKTOP_TOKEN_HEADER: DESKTOP_TOKEN},
            )
        self.assertEqual(denied_media.status_code, 404)


if __name__ == "__main__":
    unittest.main()
