from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = ROOT / ".agents" / "plugins" / "plugins" / "memolens" / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from backend.src.media.blueprint import BlueprintService  # noqa: E402
from backend.src.media.coverage import CoverageService  # noqa: E402
from backend.src.media.library_scan import LibraryScanRunner  # noqa: E402
from backend.src.media.timeline_lowering import TimelineLoweringService  # noqa: E402
from core.db import ImageIndexRepository  # noqa: E402
from core.media_db import MediaRepository  # noqa: E402
from memolens_canonical_editor import PairedCanonicalEditorBackend  # noqa: E402
from memolens_editor_server import (  # noqa: E402
    EditorServerError,
    EditorServerManager,
)


def _open_directory(path: Path) -> int:
    return os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )


class _RealWorkspaceClient:
    def __init__(self, repository: MediaRepository) -> None:
        self.blueprints = BlueprintService(repository)
        self.coverage = CoverageService(repository)
        self.timelines = TimelineLoweringService(repository)

    def project_workspace(self, project_id: str) -> dict[str, object]:
        return self.blueprints.project_workspace(project_id).response

    def coverage_workspace(self, project_id: str) -> dict[str, object]:
        return self.coverage.read(project_id)

    def timeline_workspace(self, project_id: str) -> dict[str, object]:
        return self.timelines.read(project_id)


class LibraryBootstrapEditorGateTests(unittest.TestCase):
    """A completed empty scan must not manufacture canonical edit authority."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="memolens-bootstrap-editor-gate-"
        )
        self.root = Path(self.temporary.name).resolve()
        self.library = self.root / "library"
        self.library.mkdir()
        self.db_path = self.root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(None)
        self.manager = EditorServerManager(
            session_ttl_seconds=60,
            boot_ttl_seconds=30,
        )

    def tearDown(self) -> None:
        self.manager.close()
        self.repository.close()
        self.temporary.cleanup()

    def _commit_bootstrap(self) -> tuple[str, str, str]:
        descriptor = _open_directory(self.library)
        try:
            identity = os.fstat(descriptor)
            binding = self.repository.library_bootstrap_candidate_binding_sha256(
                canonical_root=self.library,
                expected_library_root_device=int(identity.st_dev),
                expected_library_root_inode=int(identity.st_ino),
            )
            now_ms = int(time.time() * 1000)
            committed = self.repository.commit_library_bootstrap(
                request_id=f"lb_{'e' * 64}",
                intent_created_at_ms=now_ms - 1_000,
                intent_expires_at_ms=now_ms + 299_000,
                native_confirmed_at_ms=now_ms,
                candidate_binding_sha256=binding,
                library_root_path=self.library,
                library_root_fd=descriptor,
                expected_library_root_device=int(identity.st_dev),
                expected_library_root_inode=int(identity.st_ino),
            )
        finally:
            os.close(descriptor)
        self.assertEqual(committed["response"]["blueprint_authority"], "unverified")
        self.assertFalse(committed["response"]["timeline_edit_granted"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT project_id,scan_job_id,library_root_id "
                "FROM library_bootstrap_receipts"
            ).fetchone()
        assert row is not None
        return str(row[0]), str(row[1]), str(row[2])

    def test_completed_empty_scan_still_denies_canonical_editor_handoff(self) -> None:
        project_id, scan_job_id, root_id = self._commit_bootstrap()
        submitted: list[str] = []
        runner = LibraryScanRunner(
            self.repository,
            submit_child=submitted.append,
        )
        runner.run(
            scan_job_id,
            runtime_generation_id=f"runtime_generation_{'7' * 64}",
            active_library_root_id=root_id,
            should_stop=lambda: False,
        )

        scan = self.repository.get_library_scan_context(scan_job_id)
        self.assertEqual((scan["status"], scan["stage"]), ("succeeded", "no_supported_media"))
        self.assertEqual(scan["checkpoint"]["processed_count"], 0)
        self.assertEqual(submitted, [])

        client = _RealWorkspaceClient(self.repository)
        project = client.project_workspace(project_id)["project"]
        coverage = client.coverage_workspace(project_id)
        timeline = client.timeline_workspace(project_id)
        blueprint = project["current_blueprint"]
        semantic = blueprint["blueprint"]["semantic"]

        self.assertEqual(blueprint["authority"], {"state": "unverified", "verified": False})
        self.assertEqual(blueprint["blueprint"]["evidence_manifest"], [])
        self.assertEqual(semantic["bindings"], {"creator_context": None, "wiki_generation": None})
        self.assertEqual(
            [gap["gap_id"] for gap in semantic["missing_evidence"]],
            ["library_scan_pending"],
        )
        self.assertEqual(project["coverage"]["state"], "missing")
        self.assertIsNone(project["coverage"]["head"])
        self.assertEqual(coverage["freshness"]["state"], "missing")
        self.assertIsNone(coverage["head"])
        self.assertEqual(project["timeline"]["state"], "missing")
        self.assertIsNone(project["timeline"]["head"])
        self.assertEqual(timeline["freshness"]["state"], "missing")
        self.assertIsNone(timeline["head"])
        self.assertEqual(timeline["lifecycle"]["state"], "not_materialized")
        self.assertFalse(project["capabilities"]["timeline_mutation"])

        backend = PairedCanonicalEditorBackend(
            project_id=project_id,
            credential={
                "database_uuid": self.repository.database_uuid,
                "actions": ["timeline.apply_edit"],
            },
            client=client,  # type: ignore[arg-type]
        )
        with self.assertRaises(EditorServerError) as raised:
            self.manager.create_canonical_handoff(backend=backend)
        self.assertEqual(raised.exception.code, "canonical_editor_not_editable")
        self.assertIsNone(self.manager.origin)


if __name__ == "__main__":
    unittest.main()
