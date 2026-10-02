from __future__ import annotations

import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from backend.src import (
    DESKTOP_TOKEN_HEADER,
    create_app,
    shutdown_runtime_extensions,
)
from backend.src.api.routes import (
    LEGACY_HIGH_IMPACT_SCOPE_ENDPOINTS,
    MEDIA_PRIVILEGED_ENDPOINTS,
)
from backend.src.media.importing import MediaImportService
from backend.src.process_lifecycle import (
    require_literal_loopback_bind_host,
    run_managed_backend,
)
from core.config import Settings
from core.schemas import VisionMetadata
from indexing.files import MAX_LOCAL_IMAGE_BYTES


class _NeverRunApp:
    def __init__(self) -> None:
        self.extensions: dict[str, object] = {}
        self.run_calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> None:
        self.run_calls.append(kwargs)


class LegacyRouteAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="memolens-legacy-authority-"
        )
        self.root = Path(self.temporary_directory.name)
        self.library_root = self.root / "library"
        self.library_root.mkdir()
        self.desktop_token = "legacy-authority-desktop-token"
        self.desktop_headers = {DESKTOP_TOKEN_HEADER: self.desktop_token}
        self.environment = patch.dict(
            os.environ,
            {
                "APP_CONFIG_PATH": str(
                    Path(__file__).resolve().parents[1] / "config.yaml"
                ),
                "MEMOLENS_APP_STATE_DIR": str(self.root / "state"),
                "IMAGE_LIBRARY_DIR": str(self.library_root),
                "SQLITE_DB_PATH": str(self.root / "state" / "photo-index.db"),
                "MEMOLENS_DESKTOP_SESSION_TOKEN": self.desktop_token,
                "MEMOLENS_NETWORK_PROFILE": "online",
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

    def tearDown(self) -> None:
        shutdown_runtime_extensions(self.app.extensions)
        self.environment.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _image_bytes(color: tuple[int, int, int]) -> bytes:
        buffer = BytesIO()
        Image.new("RGB", (16, 12), color).save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()

    @staticmethod
    def _root_identity(path: Path) -> dict[str, str]:
        metadata = path.stat()
        return {
            "device": str(metadata.st_dev),
            "inode": str(metadata.st_ino),
        }

    def test_legacy_mutations_reject_before_json_or_service_dispatch(self) -> None:
        requests = (
            ("put", "/v1/settings"),
            ("post", "/v1/indexing/jobs"),
            ("post", "/v1/atlas/rebuild"),
            ("post", "/v1/atlas/feedback"),
            ("post", "/v1/atlas/basket"),
            ("post", "/v1/atlas/stack/action"),
            ("post", "/v1/creative/briefs"),
            ("post", "/v1/creative/projects/project-1/timeline/reconcile"),
            ("post", "/v1/creative/projects/project-1/timeline/edit"),
            (
                "post",
                "/v1/creative/projects/project-1/timeline/structural-edit",
            ),
            ("post", "/v1/creative/projects/project-1/timeline/restore"),
            ("post", "/v1/image-analysis/legacy-backfill-batches"),
        )
        with patch(
            "flask.wrappers.Request.get_json",
            side_effect=AssertionError("JSON parsed before authority admission"),
        ) as get_json:
            for method, path in requests:
                with self.subTest(path=path):
                    response = getattr(self.client, method)(
                        path,
                        data=b"{not-json",
                        content_type="application/json",
                    )
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json["code"], "desktop_auth_required")
        get_json.assert_not_called()

    def test_indexing_root_and_database_are_bound_before_any_write(self) -> None:
        outside_root = self.root / "outside-library"
        outside_root.mkdir()
        outside_file = outside_root / "outside.jpg"
        outside_file.write_bytes(b"not-an-image")
        inside_file = self.library_root / "inside.jpg"
        inside_file.write_bytes(b"not-an-image")
        inside_alias = self.library_root / "inside-alias.jpg"
        inside_alias.symlink_to(inside_file)
        outside_db = self.root / "outside-state" / "forbidden.db"
        root_identity = self._root_identity(self.library_root)
        indexing_service = self.app.extensions["runtime_manager"].current_bundle.extension(
            "indexing_service"
        )

        with patch.object(
            indexing_service,
            "run",
            side_effect=AssertionError("indexing dispatched before binding admission"),
        ) as run:
            wrong_root = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(outside_root),
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )
            wrong_db = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "db_path": str(outside_db),
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )
            wrong_file = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": [str(outside_file)],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )
            symlink_file = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": [str(inside_alias)],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        for response in (wrong_root, wrong_db, wrong_file, symlink_file):
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json["code"], "indexing_authority_mismatch")
        run.assert_not_called()
        self.assertFalse(outside_db.exists())
        self.assertFalse(outside_db.parent.exists())

    def test_backend_rejects_replaced_or_forged_root_identity_before_scan_provider_and_write(
        self,
    ) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        indexing_service = bundle.extension("indexing_service")
        repository = bundle.extension("image_index_repository")
        vision_client = bundle.extension("vision_client")
        source = self.library_root / "source.jpg"
        source.write_bytes(self._image_bytes((255, 0, 0)))
        approved_identity = self._root_identity(self.library_root)
        forged_identity = {
            **approved_identity,
            "inode": str(int(approved_identity["inode"]) + 1),
        }

        with (
            patch.object(
                indexing_service,
                "run",
                side_effect=AssertionError("root mismatch reached indexing scan"),
            ) as scan,
            patch.object(
                vision_client,
                "describe_image",
                side_effect=AssertionError("root mismatch reached provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("root mismatch reached persistence"),
            ) as upsert,
        ):
            forged = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["source.jpg"],
                    "library_root_identity": forged_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

            replaced_root = self.root / "library-original"
            self.library_root.rename(replaced_root)
            self.library_root.mkdir()
            (self.library_root / "source.jpg").write_bytes(
                self._image_bytes((0, 0, 255))
            )
            replaced = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["source.jpg"],
                    "library_root_identity": approved_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        for response in (forged, replaced):
            self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
            self.assertEqual(response.json["code"], "indexing_authority_mismatch")
        scan.assert_not_called()
        provider.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(repository.summarize_index_health()["total_records"], 0)

    def test_indexing_root_identity_shape_is_closed_before_service_dispatch(
        self,
    ) -> None:
        indexing_service = self.app.extensions[
            "runtime_manager"
        ].current_bundle.extension("indexing_service")
        malformed_identities = (
            {"device": 1, "inode": "2"},
            {"device": "01", "inode": "2"},
            {"device": "1", "inode": "2", "path": str(self.library_root)},
            {"device": "1"},
        )
        with patch.object(
            indexing_service,
            "run",
            side_effect=AssertionError("malformed root identity reached service"),
        ) as run:
            for identity in malformed_identities:
                with self.subTest(identity=identity):
                    response = self.client.post(
                        "/v1/indexing/jobs",
                        json={
                            "image_dir": str(self.library_root),
                            "library_root_identity": identity,
                            "persist_to_server": True,
                        },
                        headers=self.desktop_headers,
                    )
                    self.assertEqual(
                        response.status_code,
                        400,
                        response.get_data(as_text=True),
                    )
        run.assert_not_called()
        missing = self.client.post(
            "/v1/indexing/jobs",
            json={
                "image_dir": str(self.library_root),
                "persist_to_server": True,
            },
            headers=self.desktop_headers,
        )
        self.assertEqual(missing.status_code, 409, missing.get_data(as_text=True))
        self.assertEqual(missing.json["code"], "indexing_authority_mismatch")

    def test_indexing_nested_component_symlink_and_post_admission_swap_are_denied(
        self,
    ) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        indexing_service = bundle.extension("indexing_service")
        repository = bundle.extension("image_index_repository")
        vision_client = bundle.extension("vision_client")
        root_identity = self._root_identity(self.library_root)
        outside_directory = self.root / "outside-nested"
        outside_directory.mkdir()
        (outside_directory / "source.jpg").write_bytes(
            self._image_bytes((0, 0, 255))
        )

        alias = self.library_root / "alias"
        alias.symlink_to(outside_directory, target_is_directory=True)
        with (
            patch.object(
                indexing_service,
                "run",
                side_effect=AssertionError("nested symlink reached indexing service"),
            ) as scan,
            patch.object(
                vision_client,
                "describe_image",
                side_effect=AssertionError("nested symlink reached provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("nested symlink reached persistence"),
            ) as upsert,
        ):
            symlinked = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["alias/source.jpg"],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )
        self.assertEqual(symlinked.status_code, 409, symlinked.get_data(as_text=True))
        self.assertEqual(symlinked.json["code"], "indexing_authority_mismatch")
        scan.assert_not_called()
        provider.assert_not_called()
        upsert.assert_not_called()

        nested = self.library_root / "nested"
        nested.mkdir()
        (nested / "source.jpg").write_bytes(self._image_bytes((255, 0, 0)))
        original_prepare = MediaImportService.prepare_import

        def swap_after_admission(service, **kwargs):
            plan = original_prepare(service, **kwargs)
            nested.rename(self.library_root / "nested-original")
            nested.symlink_to(outside_directory, target_is_directory=True)
            return plan

        with (
            patch.object(
                MediaImportService,
                "prepare_import",
                autospec=True,
                side_effect=swap_after_admission,
            ),
            patch.object(
                vision_client,
                "describe_image",
                side_effect=AssertionError("nested swap reached provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("nested swap reached persistence"),
            ) as upsert,
        ):
            swapped = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["nested/source.jpg"],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(swapped.status_code, 400, swapped.get_data(as_text=True))
        self.assertEqual(swapped.json["code"], "invalid_import")
        provider.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(repository.summarize_index_health()["total_records"], 0)

    def test_unauthorized_indexing_path_payload_has_zero_write_effect(self) -> None:
        outside_root = self.root / "untrusted-library"
        outside_db = self.root / "untrusted-state" / "untrusted.db"
        response = self.client.post(
            "/v1/indexing/jobs",
            json={
                "image_dir": str(outside_root),
                "db_path": str(outside_db),
                "persist_to_server": True,
            },
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(outside_root.exists())
        self.assertFalse(outside_db.exists())
        self.assertFalse(outside_db.parent.exists())

    def test_backend_filesystem_write_scope_denied_before_service_dispatch(
        self,
    ) -> None:
        media_cases = {
            "cancel_media_index_job": ("post", "/v1/index/jobs/job-1/cancel"),
            "cancel_timeline_render": ("post", "/v1/renders/render-1/cancel"),
            "commit_blueprint_proposal": (
                "post",
                "/v1/creative/projects/project-1/blueprint/commit",
            ),
            "create_creative_brief": ("post", "/v1/creative/briefs"),
            "create_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timelines",
            ),
            "download_timeline_render": (
                "get",
                "/v1/renders/render-1/download",
            ),
            "get_media_capabilities": ("get", "/v1/media/capabilities"),
            "edit_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timeline/edit",
            ),
            "structurally_edit_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timeline/structural-edit",
            ),
            "import_media_assets": ("post", "/v1/assets/import"),
            "create_legacy_image_backfill_batch": (
                "post",
                "/v1/image-analysis/legacy-backfill-batches",
            ),
            "materialize_coverage_baseline": (
                "post",
                "/v1/creative/projects/project-1/coverage/materialize",
            ),
            "materialize_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timeline/materialize",
            ),
            "reconcile_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timeline/reconcile",
            ),
            "restore_blueprint_revision": (
                "post",
                "/v1/creative/projects/project-1/blueprint/restore",
            ),
            "restore_project_timeline": (
                "post",
                "/v1/creative/projects/project-1/timeline/restore",
            ),
            "resume_media_index_job": ("post", "/v1/index/jobs/job-1/resume"),
            "revise_timeline": ("post", "/v1/timelines/timeline-1/revise"),
            "start_timeline_render": ("post", "/v1/renders"),
            "stream_media_asset": ("get", "/v1/assets/asset-1/media"),
            "update_creator_profile": ("put", "/v1/creator/profile"),
            "update_media_inbox_asset": (
                "put",
                "/v1/inbox/assets/asset-1",
            ),
            "validate_timeline": (
                "post",
                "/v1/timelines/timeline-1/validate",
            ),
        }
        legacy_cases = {
            "create_atlas_feedback": ("post", "/v1/atlas/feedback"),
            "create_atlas_stack_action": ("post", "/v1/atlas/stack/action"),
            "create_indexing_job": ("post", "/v1/indexing/jobs"),
            "rebuild_atlas": ("post", "/v1/atlas/rebuild"),
            "save_atlas_basket": ("post", "/v1/atlas/basket"),
            "update_settings": ("put", "/v1/settings"),
        }
        self.assertEqual(set(media_cases), set(MEDIA_PRIVILEGED_ENDPOINTS))
        self.assertEqual(
            set(legacy_cases),
            set(LEGACY_HIGH_IMPACT_SCOPE_ENDPOINTS),
        )

        all_cases = {**media_cases, **legacy_cases}
        dispatches: dict[str, Mock] = {}
        original_views: dict[str, object] = {}
        for endpoint in all_cases:
            full_endpoint = next(
                name
                for name in self.app.view_functions
                if name.rsplit(".", 1)[-1] == endpoint
            )
            original_views[full_endpoint] = self.app.view_functions[full_endpoint]
            dispatch = Mock(
                side_effect=AssertionError(
                    f"{endpoint} dispatched after a mismatched filesystem scope"
                )
            )
            dispatches[endpoint] = dispatch
            self.app.view_functions[full_endpoint] = dispatch

        outside_db = self.root / "outside-runtime" / "forbidden.db"
        try:
            with patch(
                "flask.wrappers.Request.get_json",
                side_effect=AssertionError(
                    "JSON parsed before filesystem scope admission"
                ),
            ) as get_json:
                for endpoint, (method, path) in all_cases.items():
                    with self.subTest(endpoint=endpoint, scope="db_path"):
                        kwargs: dict[str, object] = {
                            "headers": self.desktop_headers,
                            "query_string": {"db_path": str(outside_db)},
                        }
                        if method != "get":
                            kwargs.update(
                                data=b"{not-json",
                                content_type="application/json",
                            )
                        response = getattr(self.client, method)(path, **kwargs)
                        self.assertEqual(response.status_code, 409)
                        expected_code = (
                            "database_binding_mismatch"
                            if endpoint in MEDIA_PRIVILEGED_ENDPOINTS
                            else "filesystem_scope_mismatch"
                        )
                        self.assertEqual(response.json["code"], expected_code)

                outside_root = self.root / "outside-library"
                outside_source = outside_root / "outside.jpg"
                for scope_field, scope_path in (
                    ("image_dir", outside_root),
                    ("source_path", outside_source),
                ):
                    with self.subTest(endpoint="create_indexing_job", scope=scope_field):
                        response = self.client.post(
                            "/v1/indexing/jobs",
                            data=b"{not-json",
                            content_type="application/json",
                            headers=self.desktop_headers,
                            query_string={scope_field: str(scope_path)},
                        )
                        self.assertEqual(response.status_code, 409)
                        self.assertEqual(
                            response.json["code"],
                            "filesystem_scope_mismatch",
                        )
            get_json.assert_not_called()
        finally:
            for full_endpoint, view in original_views.items():
                self.app.view_functions[full_endpoint] = view

        for dispatch in dispatches.values():
            dispatch.assert_not_called()
        self.assertFalse(outside_db.exists())
        self.assertFalse(outside_db.parent.exists())
        self.assertFalse((self.root / "outside-library").exists())

    def test_indexing_scan_and_replacement_race_never_escape_pinned_source(
        self,
    ) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        indexing_service = bundle.extension("indexing_service")
        repository = bundle.extension("image_index_repository")
        vision_client = bundle.extension("vision_client")
        outside = self.root / "outside.jpg"
        outside.write_bytes(self._image_bytes((0, 0, 255)))
        root_identity = self._root_identity(self.library_root)

        scan_alias = self.library_root / "scan-alias.jpg"
        scan_alias.symlink_to(outside)
        with (
            patch.object(
                vision_client,
                "describe_image",
                side_effect=AssertionError("symlink bytes reached the vision provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("symlink source reached persistence"),
            ) as upsert,
        ):
            scanned = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(scanned.status_code, 200)
        self.assertEqual(scanned.json["status"], "empty")
        self.assertEqual(scanned.json["errors"], [])
        provider.assert_not_called()
        upsert.assert_not_called()

        scan_alias.unlink()

        original = self.library_root / "original.jpg"
        original.write_bytes(self._image_bytes((255, 0, 0)))

        original_prepare = MediaImportService.prepare_import

        def replace_after_pinned_read(service, **kwargs):
            plan = original_prepare(service, **kwargs)
            original.unlink()
            original.symlink_to(outside)
            return plan

        with (
            patch.object(
                MediaImportService,
                "prepare_import",
                autospec=True,
                side_effect=replace_after_pinned_read,
            ),
            patch.object(
                vision_client,
                "describe_image",
                side_effect=AssertionError("legacy request reached provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("replaced source reached persistence"),
            ) as upsert,
        ):
            raced = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["original.jpg"],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(raced.status_code, 400)
        self.assertEqual(raced.json["code"], "invalid_import")
        provider.assert_not_called()
        upsert.assert_not_called()
        self.assertEqual(repository.summarize_index_health()["total_records"], 0)

        original.unlink()
        oversized = self.library_root / "oversized.jpg"
        with oversized.open("wb") as handle:
            handle.truncate(MAX_LOCAL_IMAGE_BYTES + 1)
        with (
            patch.object(
                indexing_service.vision_client,
                "describe_image",
                side_effect=AssertionError("oversized bytes reached provider"),
            ) as provider,
            patch.object(
                repository,
                "upsert",
                side_effect=AssertionError("oversized source reached persistence"),
            ) as upsert,
        ):
            oversized_response = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["oversized.jpg"],
                    "library_root_identity": root_identity,
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(oversized_response.status_code, 200)
        self.assertEqual(oversized_response.json["status"], "failed")
        provider.assert_not_called()
        upsert.assert_not_called()

    def test_pinned_indexing_success_persists_the_verified_source_identity(self) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        repository = bundle.extension("image_index_repository")
        media_repository = bundle.extension("media_repository")
        vision_client = bundle.extension("vision_client")
        source_path = self.library_root / "verified.jpg"
        source_path.write_bytes(self._image_bytes((0, 255, 0)))
        observed = source_path.stat()

        with patch.object(
            vision_client,
            "describe_image",
            return_value=VisionMetadata(tags=["green"], description="Verified green source"),
        ) as provider:
            response = self.client.post(
                "/v1/indexing/jobs",
                json={
                    "image_dir": str(self.library_root),
                    "files": ["verified.jpg"],
                    "library_root_identity": self._root_identity(self.library_root),
                    "persist_to_server": True,
                },
                headers=self.desktop_headers,
            )

        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(response.json["status"], "queued")
        self.assertEqual(len(response.json["queued"]), 1)
        self.assertEqual(response.json["data"], [])
        provider.assert_not_called()
        rows = repository.fetch_candidates()
        self.assertEqual(rows, [])
        asset = media_repository.get_asset(str(response.json["queued"][0]["id"]))
        self.assertIsNotNone(asset)
        assert asset is not None
        source = media_repository.get_asset_source(str(asset["asset_source_id"]))
        self.assertIsNotNone(source)
        assert source is not None
        self.assertEqual(source["source_file_id"], str(observed.st_ino))
        self.assertEqual(source["observed_size"], observed.st_size)

    def test_provider_egress_requires_desktop_authority_before_service_dispatch(
        self,
    ) -> None:
        bundle = self.app.extensions["runtime_manager"].current_bundle
        planner = bundle.extension("query_planner")
        copywriter = bundle.extension("retrieval_copywriter")
        atlas = bundle.extension("photo_atlas_service")
        image = {
            "id": "image-1",
            "filename": "one.jpg",
            "relative_path": "one.jpg",
            "description": "A quiet mountain lake.",
            "tags": ["mountain", "lake"],
            "score": 0.9,
        }

        with (
            patch.object(planner.settings, "query_api_key", "configured-provider-key"),
            patch.object(planner.settings, "query_provider", "openai"),
            patch.object(
                planner.settings,
                "query_base_url",
                "https://query-provider.invalid/v1",
            ),
            patch.object(planner.settings, "vision_api_key", "configured-provider-key"),
            patch.object(planner.settings, "vision_provider", "openai"),
            patch.object(
                planner.settings,
                "vision_base_url",
                "https://vision-provider.invalid/v1",
            ),
            patch.object(planner.settings, "embedding_backend", "transformers"),
            patch.object(
                planner.settings,
                "text_embedding_model_id",
                "external/model-that-must-not-load",
            ),
            patch.object(
                bundle.extension("text_embedding_service").__class__,
                "_ensure_loaded",
                side_effect=AssertionError("external embedding model load dispatched"),
            ) as embedding_load,
            patch.object(
                planner,
                "_request_planning_content",
                side_effect=AssertionError("remote planning provider dispatched"),
            ) as remote_plan,
            patch.object(
                planner,
                "_request_inspiration_content",
                side_effect=AssertionError("remote inspiration provider dispatched"),
            ) as remote_inspiration,
            patch.object(
                copywriter,
                "generate",
                side_effect=AssertionError("remote copy provider dispatched"),
            ) as remote_copy,
            patch.object(
                atlas,
                "workbench",
                return_value={
                    "library_summary": {},
                    "memories": [],
                },
            ),
            patch.object(
                atlas,
                "generate",
                return_value={
                    "object": "atlas.generate",
                    "status": "completed",
                    "candidate_count": 1,
                    "data": [image],
                },
            ),
        ):
            responses = []
            for headers in ({}, self.desktop_headers):
                responses.extend(
                    (
                        self.client.post(
                            "/v1/retrieval/query",
                            json={
                                "text": "last winter mountain lake",
                                "include_copy": True,
                            },
                            headers=headers,
                        ),
                        self.client.post(
                            "/v1/retrieval/copy",
                            json={"query_text": "mountain lake", "images": [image]},
                            headers=headers,
                        ),
                        self.client.post(
                            "/v1/inspiration/generate",
                            json={"count": 5},
                            headers=headers,
                        ),
                        self.client.post(
                            "/v1/atlas/generate",
                            json={
                                "text": "mountain lake",
                                "include_copy": True,
                            },
                            headers=headers,
                        ),
                    )
                )

        for response in responses:
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(responses[1].json["object"], "generated_copy")
        self.assertEqual(responses[2].json["source"], "query_profile")
        self.assertIsNotNone(responses[3].json["generated_copy"])
        self.assertEqual(responses[5].json["object"], "generated_copy")
        self.assertEqual(responses[6].json["source"], "query_profile")
        self.assertIsNotNone(responses[7].json["generated_copy"])
        remote_plan.assert_not_called()
        remote_inspiration.assert_not_called()
        remote_copy.assert_not_called()
        embedding_load.assert_not_called()

    def test_settings_ollama_probe_ignores_proxy_environment(self) -> None:
        response = Mock(ok=True)
        session = Mock()
        session.get.return_value = response
        with (
            patch.dict(
                os.environ,
                {
                    "HTTP_PROXY": "http://proxy.invalid:8080",
                    "HTTPS_PROXY": "http://proxy.invalid:8080",
                },
            ),
            patch("core.local_model_runtime.requests.Session", return_value=session),
            patch(
                "core.local_model_runtime.requests.get",
                side_effect=AssertionError("proxy-aware requests.get was used"),
            ) as proxy_aware_get,
        ):
            settings = self.client.get("/v1/settings")

        self.assertEqual(settings.status_code, 200)
        self.assertIs(session.trust_env, False)
        session.get.assert_called_once_with(
            "http://127.0.0.1:11434/api/tags",
            timeout=0.5,
        )
        session.close.assert_called_once_with()
        proxy_aware_get.assert_not_called()

    def test_backend_bind_and_high_impact_surfaces_reject_non_loopback_authority(
        self,
    ) -> None:
        self.assertEqual(require_literal_loopback_bind_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(require_literal_loopback_bind_host("127.42.3.9"), "127.42.3.9")
        self.assertEqual(require_literal_loopback_bind_host("::1"), "::1")

        for host in (
            "",
            "localhost",
            "0.0.0.0",
            "192.168.1.10",
            "2130706433",
            "::",
            "::ffff:127.0.0.1",
        ):
            with self.subTest(host=host):
                app = _NeverRunApp()
                with self.assertRaisesRegex(ValueError, "literal loopback"):
                    run_managed_backend(app, host=host, port=5519, debug=False)
                self.assertEqual(app.run_calls, [])

        high_impact_requests = (
            ("get", "/v1/settings", None),
            ("put", "/v1/settings", {}),
            ("post", "/v1/indexing/jobs", {}),
            ("post", "/v1/retrieval/query", {"text": "mountain"}),
            ("post", "/v1/atlas/feedback", {}),
            ("post", "/v1/creative/briefs", {}),
        )
        for method, path, payload in high_impact_requests:
            with self.subTest(path=path):
                kwargs: dict[str, object] = {
                    "environ_overrides": {"REMOTE_ADDR": "192.168.1.10"},
                    "headers": self.desktop_headers,
                }
                if payload is not None:
                    kwargs["json"] = payload
                response = getattr(self.client, method)(path, **kwargs)
                self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
