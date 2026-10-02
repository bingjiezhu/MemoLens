from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from backend.src.media.blueprint import BlueprintService
from backend.src.media.canonical_export import (
    CANONICAL_EXPORT_WORKER_ACTIONS,
    CanonicalExportJobRunner,
    CanonicalExportService,
    _canonical_export_job_identity,
)
from backend.src.media.coverage import CoverageService
from backend.src.media.render import RENDER_JOB_IDENTITY_FIELDS, RenderJobRunner
from backend.src.media.render_plan import RENDER_PROFILE_REGISTRY
from backend.src.media.timeline import TimelineService
from backend.src.media.timeline_lowering import TimelineLoweringService
from backend.src.media.worker_admission import job_identity
from core.db import ImageIndexRepository
from core.media_db import CanonicalExportIntegrityError, MediaRepository
from tests import test_timeline_lowering_integration as timeline_integration


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INVENTORY_PATH = PROJECT_ROOT / "core" / "production_surface_inventory.v1.json"


def _tree(root: Path) -> tuple[tuple[str, str], ...]:
    if not root.exists():
        return ()
    values: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            values.append((relative, "directory"))
        elif path.is_file():
            values.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        else:
            values.append((relative, "non-regular"))
    return tuple(values)


def _inventory_action(action_id: str) -> dict[str, object]:
    inventory = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    actions = {
        str(action["id"]): action
        for action in inventory["actions"]
    }
    return actions[action_id]


class CanonicalExportWorkerInventoryTests(unittest.TestCase):
    _build_project = timeline_integration.TimelineLoweringIntegrationTests._build_project
    _materialize = timeline_integration.TimelineLoweringIntegrationTests._materialize

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-canonical-worker-inventory-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.blueprints = BlueprintService(self.repository)
        self.coverage = CoverageService(self.repository)
        self.timelines = TimelineLoweringService(self.repository)
        self.cache = self.root / "state" / "media-cache"
        self.runner = CanonicalExportJobRunner(self.repository, self.cache)
        self.service = CanonicalExportService(self.repository, self.runner)
        self._ordinal = 0

    def tearDown(self) -> None:
        self.runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def _register_image(self, name: str, content: bytes) -> dict[str, object]:
        path = self.library / name
        encoded = BytesIO()
        Image.new(
            "RGB",
            (96, 64),
            color=tuple(hashlib.sha256(content).digest()[:3]),
        ).save(encoded, format="JPEG", quality=90)
        exact = encoded.getvalue()
        if not path.exists() or path.read_bytes() != exact:
            path.write_bytes(exact)
        return timeline_integration.TimelineLoweringIntegrationTests._register_image(
            self,
            name,
            exact,
        )

    def _create_job(self, label: str) -> tuple[dict[str, object], Path]:
        self._ordinal += 1
        project_id, _blueprint, _coverage, timeline_payload = self._build_project()
        self._materialize(
            project_id,
            timeline_payload,
            key=f"timeline-worker-{self._ordinal}",
        )
        output = self.root / f"export-{self._ordinal}"
        output.mkdir()
        selected = output.stat()
        presentation = self.service.presentation(project_id)
        with patch.object(self.runner, "submit"):
            result = self.service.start(
                project_id,
                {
                    "presentation": presentation["presentation"],
                    "presentation_sha256": presentation["presentation_sha256"],
                    "native_gesture_nonce": (
                        f"native-worker-export-{self._ordinal:04d}"
                    ),
                    "destination": {
                        "canonical_path": str(output),
                        "package_basename": label,
                        "selection_identity": {
                            "device": str(selected.st_dev),
                            "inode": str(selected.st_ino),
                        },
                    },
                },
                runtime_epoch="runtime-worker-inventory",
                idempotency_key=f"canonical-worker-{self._ordinal}",
            )
        return dict(result.response["job"]), output

    def _assert_canonical_action_contract(self) -> None:
        self.assertEqual(CANONICAL_EXPORT_WORKER_ACTIONS, frozenset({"package"}))
        action = _inventory_action("python_worker.canonical_export.package")
        self.assertEqual(
            action["authority"],
            ["export-grant", "source-identity", "worker-internal"],
        )
        self.assertEqual(
            action["scope"],
            ["canonical-export-job", "canonical-source-set", "native-export-root"],
        )

    def test_canonical_export_package_rejects_worker_admission_before_effects(
        self,
    ) -> None:
        self._assert_canonical_action_contract()
        first, first_output = self._create_job("admission-one")
        second, second_output = self._create_job("admission-two")
        output_before = (_tree(first_output), _tree(second_output))
        cache_before = _tree(self.cache)

        with (
            patch.object(
                self.runner,
                "_bound_inputs",
                side_effect=AssertionError("scope preflight must not run"),
            ) as bound_inputs,
            patch.object(
                self.repository,
                "update_canonical_export_job",
                side_effect=AssertionError("worker admission denial must not write"),
            ) as update_job,
            patch(
                "backend.src.media.canonical_export.resolve_binary",
                side_effect=AssertionError("worker admission denial must not probe"),
            ) as resolve,
            patch(
                "backend.src.media.canonical_export.render_canonical_master",
                side_effect=AssertionError("worker admission denial must not read media"),
            ) as render,
            patch(
                "backend.src.media.canonical_export.build_lightweight_package",
                side_effect=AssertionError("worker admission denial must not write"),
            ) as package,
        ):
            first_id = str(first["id"])
            second_id = str(second["id"])
            self.runner._run(first_id)
            self.runner._run(first_id, admission_ticket="forged")

            cross_job = self.runner._admission_authority.issue(
                first_id,
                _canonical_export_job_identity(first),
            )
            self.runner._run(second_id, admission_ticket=cross_job)

            rebound = self.runner._admission_authority.issue(
                first_id,
                _canonical_export_job_identity(first),
            )
            rebound_job = copy.deepcopy(first)
            rebound_job["output_binding"]["package_basename"] = "rebound"
            with patch.object(
                self.repository,
                "get_canonical_export_job",
                return_value=rebound_job,
            ):
                self.runner._run(first_id, admission_ticket=rebound)

            revoked = self.runner._admission_authority.issue(
                first_id,
                _canonical_export_job_identity(first),
            )
            self.runner._admission_authority.revoke(revoked)
            self.runner._run(first_id, admission_ticket=revoked)

            replay = self.runner._admission_authority.issue(
                first_id,
                _canonical_export_job_identity(first),
            )
            terminal = copy.deepcopy(first)
            terminal.update({"status": "cancelled", "stage": "cancelled"})
            with patch.object(
                self.repository,
                "get_canonical_export_job",
                return_value=terminal,
            ):
                self.runner._run(first_id, admission_ticket=replay)
            self.runner._run(first_id, admission_ticket=replay)

        bound_inputs.assert_not_called()
        update_job.assert_not_called()
        resolve.assert_not_called()
        render.assert_not_called()
        package.assert_not_called()
        self.assertEqual((_tree(first_output), _tree(second_output)), output_before)
        self.assertEqual(_tree(self.cache), cache_before)

    def test_canonical_export_package_rejects_missing_or_revoked_grant_and_wrong_scopes_before_effects(
        self,
    ) -> None:
        self._assert_canonical_action_contract()
        missing_grant, missing_output = self._create_job("missing-grant")
        revoked_grant, revoked_output = self._create_job("revoked-grant")
        wrong_job, wrong_job_output = self._create_job("wrong-job")
        wrong_sources, wrong_sources_output = self._create_job("wrong-sources")
        revoked_root, revoked_root_output = self._create_job("revoked-root")
        jobs = (
            missing_grant,
            revoked_grant,
            wrong_job,
            wrong_sources,
            revoked_root,
        )
        tickets = {
            str(job["id"]): self.runner._admission_authority.issue(
                str(job["id"]),
                _canonical_export_job_identity(job),
            )
            for job in jobs
        }

        self.repository.request_canonical_export_cancel(str(revoked_grant["id"]))
        with closing(sqlite3.connect(self.db_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "DROP TRIGGER trg_canonical_export_operations_no_delete"
            )
            connection.execute(
                "DROP TRIGGER trg_canonical_export_jobs_identity_immutable"
            )
            connection.execute(
                "DELETE FROM canonical_export_operations WHERE id=?",
                (missing_grant["operation_id"],),
            )
            connection.execute(
                "UPDATE canonical_export_jobs SET operation_id=? WHERE id=?",
                ("exportop_wrong_worker_scope", wrong_job["id"]),
            )
            connection.execute(
                """UPDATE canonical_export_jobs
                      SET timeline_source_bindings_sha256=? WHERE id=?""",
                ("f" * 64, wrong_sources["id"]),
            )
            root_id = revoked_root["output_binding"]["output_root_id"]
            connection.execute(
                "UPDATE output_roots SET status='revoked' WHERE id=?",
                (root_id,),
            )

        outputs = (
            missing_output,
            revoked_output,
            wrong_job_output,
            wrong_sources_output,
            revoked_root_output,
        )
        output_before = tuple(_tree(output) for output in outputs)
        cache_before = _tree(self.cache)
        with (
            patch.object(
                self.repository,
                "update_canonical_export_job",
                side_effect=AssertionError("scope denial must precede job mutation"),
            ) as update_job,
            patch(
                "backend.src.media.canonical_export.resolve_binary",
                side_effect=AssertionError("scope denial must precede process probe"),
            ) as resolve,
            patch(
                "backend.src.media.canonical_export.render_canonical_master",
                side_effect=AssertionError("scope denial must precede media read"),
            ) as render,
            patch(
                "backend.src.media.canonical_export.build_lightweight_package",
                side_effect=AssertionError("scope denial must precede output write"),
            ) as package,
        ):
            for corrupted in (missing_grant, wrong_job, wrong_sources):
                with self.subTest(corrupted=corrupted["id"]), self.assertRaises(
                    CanonicalExportIntegrityError
                ):
                    self.runner._run(
                        str(corrupted["id"]),
                        admission_ticket=tickets[str(corrupted["id"])],
                    )
            self.runner._run(
                str(revoked_grant["id"]),
                admission_ticket=tickets[str(revoked_grant["id"])],
            )
            self.runner._run(
                str(revoked_root["id"]),
                admission_ticket=tickets[str(revoked_root["id"])],
            )

        update_job.assert_not_called()
        resolve.assert_not_called()
        render.assert_not_called()
        package.assert_not_called()
        self.assertEqual(tuple(_tree(output) for output in outputs), output_before)
        self.assertEqual(_tree(self.cache), cache_before)


class RenderProfileScopeInventoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-render-profile-inventory-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.root_id = str(self.repository.library_roots()[0]["id"])
        source = self.library / "timeline-source.jpg"
        Image.new("RGB", (64, 36), color="navy").save(source, format="JPEG")
        observed = source.stat()
        asset = self.repository.upsert_asset_source(
            root_id=self.root_id,
            relative_path=source.name,
            filename=source.name,
            kind="image",
            sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            mime_type="image/jpeg",
            file_size=observed.st_size,
            mtime_ns=observed.st_mtime_ns,
            source_file_id=str(observed.st_ino),
        )
        self.repository.update_image_probe(str(asset["id"]), width=64, height=36)
        project = self.repository.create_project(
            "Render profile authority",
            {
                "schema_version": "1",
                "goal": "Render profile authority",
                "duration_ms": 1_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [
                    {
                        "id": asset["id"],
                        "asset_id": asset["id"],
                        "asset_source_id": asset["asset_source_id"],
                        "result_type": "image_asset",
                    }
                ],
            },
            {"created_by": "inventory-test", "external_model": False},
        )
        self.project_id = str(project["id"])
        timeline = TimelineService(self.repository).create_from_project(
            self.project_id
        )
        self.timeline_id = str(timeline["timeline"]["id"])
        self.timeline_sha = str(timeline["content_sha256"])
        self.preview_path = self.root / "preview"
        self.preview_root = self.repository.register_preview_root(self.preview_path)
        self.export_path = self.root / "native-export"
        self.export_path.mkdir(parents=True)
        export_root = self.repository.register_user_export_root(self.export_path)
        self.export_root_id = str(export_root["id"])
        with self.repository.transaction(immediate=True) as connection:
            self.repository._materialize_user_export_root_in_transaction(
                connection,
                root_id=self.export_root_id,
                permission_fingerprint=str(export_root["permission_fingerprint"]),
            )
        self.cache = self.root / "state" / "render-cache"
        self.runner = RenderJobRunner(
            self.repository,
            self.cache,
            reconcile_on_start=False,
        )
        self._ordinal = 0

    def tearDown(self) -> None:
        self.runner.shutdown()
        self.repository.close()
        self.temporary.cleanup()

    def _grant(
        self,
        *,
        root_id: str,
        filename: str,
        status: str = "active",
        project_id: str | None = None,
    ) -> str:
        self._ordinal += 1
        grant_id = f"grant-render-inventory-{self._ordinal}"
        now = datetime.now(timezone.utc)
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO export_grants(
                     id,output_root_id,project_id,filename,token_sha256,
                     allow_overwrite,single_use,status,issued_at,expires_at,
                     consumed_at)
                   VALUES(?,?,?,?,?,0,1,?,?,?,NULL)""",
                (
                    grant_id,
                    root_id,
                    project_id or self.project_id,
                    filename,
                    hashlib.sha256(grant_id.encode()).hexdigest(),
                    status,
                    now.isoformat(),
                    (now + timedelta(minutes=10)).isoformat(),
                ),
            )
        return grant_id

    def _job(
        self,
        *,
        profile: str,
        root_id: str,
        filename: str,
        grant_id: str | None = None,
    ) -> dict[str, object]:
        return self.repository.create_render_job(
            timeline_id=self.timeline_id,
            timeline_revision=1,
            profile=profile,
            output_root_id=root_id,
            output_relative_path=filename,
            timeline_content_sha256=self.timeline_sha,
            export_grant_id=grant_id,
        )

    def _ticket(self, job: dict[str, object]) -> str:
        return self.runner._admission_authority.issue(
            str(job["id"]),
            job_identity(job, fields=RENDER_JOB_IDENTITY_FIELDS),
        )

    def test_render_profile_dispatcher_rejects_timeline_grant_and_output_root_scope_before_media_effects(
        self,
    ) -> None:
        self.assertEqual(
            RENDER_PROFILE_REGISTRY,
            frozenset({"export-1080p", "preview-low"}),
        )
        preview_action = _inventory_action("render_profile.preview-low")
        export_action = _inventory_action("render_profile.export-1080p")
        self.assertEqual(
            preview_action["scope"],
            [
                "managed-preview-root",
                "render-job",
                "timeline-revision",
                "verified-render-source-set",
            ],
        )
        self.assertEqual(
            export_action["scope"],
            [
                "native-export-root",
                "render-job",
                "timeline-revision",
                "verified-render-source-set",
            ],
        )
        self.assertIn("export-grant", export_action["authority"])

        preview_timeline = self._job(
            profile="preview-low",
            root_id=str(self.preview_root["id"]),
            filename="preview-timeline.mp4",
        )
        preview_wrong_root = self._job(
            profile="preview-low",
            root_id=str(self.preview_root["id"]),
            filename="preview-wrong-root.mp4",
        )
        missing_grant_id = self._grant(
            root_id=self.export_root_id,
            filename="missing-grant.mp4",
        )
        export_missing_grant = self._job(
            profile="export-1080p",
            root_id=self.export_root_id,
            filename="missing-grant.mp4",
            grant_id=missing_grant_id,
        )
        revoked_grant_id = self._grant(
            root_id=self.export_root_id,
            filename="revoked-grant.mp4",
            status="revoked",
        )
        export_revoked_grant = self._job(
            profile="export-1080p",
            root_id=self.export_root_id,
            filename="revoked-grant.mp4",
            grant_id=revoked_grant_id,
        )
        timeline_grant_id = self._grant(
            root_id=self.export_root_id,
            filename="export-timeline.mp4",
        )
        export_timeline = self._job(
            profile="export-1080p",
            root_id=self.export_root_id,
            filename="export-timeline.mp4",
            grant_id=timeline_grant_id,
        )
        wrong_scope_grant_id = self._grant(
            root_id=self.export_root_id,
            filename="different-name.mp4",
        )
        export_wrong_grant_scope = self._job(
            profile="export-1080p",
            root_id=self.export_root_id,
            filename="wrong-grant-scope.mp4",
            grant_id=wrong_scope_grant_id,
        )
        preview_native_grant_id = self._grant(
            root_id=str(self.preview_root["id"]),
            filename="wrong-native-root.mp4",
        )
        export_wrong_root = self._job(
            profile="export-1080p",
            root_id=str(self.preview_root["id"]),
            filename="wrong-native-root.mp4",
            grant_id=preview_native_grant_id,
        )

        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE render_jobs SET timeline_content_sha256=? WHERE id IN (?,?)",
                ("e" * 64, preview_timeline["id"], export_timeline["id"]),
            )
            connection.execute(
                "UPDATE render_jobs SET output_root_id=? WHERE id=?",
                (self.export_root_id, preview_wrong_root["id"]),
            )
            connection.execute(
                "UPDATE render_jobs SET export_grant_id=NULL WHERE id=?",
                (export_missing_grant["id"],),
            )

        denied_jobs = [
            self.repository.get_render_job(str(job["id"]))
            for job in (
                preview_timeline,
                preview_wrong_root,
                export_missing_grant,
                export_revoked_grant,
                export_timeline,
                export_wrong_grant_scope,
                export_wrong_root,
            )
        ]
        self.assertTrue(all(job is not None for job in denied_jobs))
        jobs = [job for job in denied_jobs if job is not None]
        tickets = {str(job["id"]): self._ticket(job) for job in jobs}
        source_before = _tree(self.library)
        preview_before = _tree(self.preview_path)
        export_before = _tree(self.export_path)
        cache_before = _tree(self.cache)

        with (
            patch.object(
                self.repository,
                "update_render_job",
                side_effect=AssertionError("scope denial must precede job mutation"),
            ) as update_job,
            patch(
                "backend.src.media.render.SourceVerifier.verify",
                side_effect=AssertionError("scope denial must precede media read"),
            ) as source_verify,
            patch(
                "backend.src.media.render.ffmpeg_encode_capability",
                side_effect=AssertionError("scope denial must precede process probe"),
            ) as capability,
            patch(
                "backend.src.media.render.resolve_binary",
                side_effect=AssertionError("scope denial must precede binary dispatch"),
            ) as resolve,
            patch(
                "backend.src.media.render.subprocess.Popen",
                side_effect=AssertionError("scope denial must precede process start"),
            ) as process,
            patch(
                "backend.src.media.render.tempfile.mkdtemp",
                side_effect=AssertionError("scope denial must precede workspace write"),
            ) as workspace,
            patch(
                "backend.src.media.render.publish_render",
                side_effect=AssertionError("scope denial must precede output write"),
            ) as publish,
        ):
            for job in jobs:
                with self.subTest(job=job["id"], profile=job["profile"]):
                    self.runner._run(
                        str(job["id"]),
                        admission_ticket=tickets[str(job["id"])],
                    )

        update_job.assert_not_called()
        source_verify.assert_not_called()
        capability.assert_not_called()
        resolve.assert_not_called()
        process.assert_not_called()
        workspace.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(_tree(self.library), source_before)
        self.assertEqual(_tree(self.preview_path), preview_before)
        self.assertEqual(_tree(self.export_path), export_before)
        self.assertEqual(_tree(self.cache), cache_before)


if __name__ == "__main__":
    unittest.main()
