from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest

from core.image_read_cutover_schema import (
    V17_CHECKSUM,
    V17_SCHEMA_OBJECTS,
)
from core.media_db import (
    SCHEMA_VERSION,
    V18_CHECKSUM,
    V19_CHECKSUM,
    V20_CHECKSUM,
    _V18_SCHEMA_OBJECTS,
    _V19_SCHEMA_OBJECTS,
    _V20_SCHEMA_OBJECTS,
    MediaRepository,
    canonical_json,
)
from core.provider_egress_schema import (
    V16_CHECKSUM,
    V16_SCHEMA_OBJECTS,
)
from scripts.collect_b2b4_real_host_evidence import (
    EvidenceCollectionError,
    collect_evidence,
)
from scripts.prepare_b2b4_real_host_fixture import (
    DIRECTIONS,
    FIXTURE_DATABASE_SCHEMA_VERSION,
    IMAGE_ANALYSIS_PROFILE,
    IMAGE_RUNTIME_GENERATION,
    MANIFEST_NAME,
    default_fixture_root,
    prepare_fixture,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class B2B4RealHostFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp_root = PROJECT_ROOT / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="b2b4-real-host-tests-",
            dir=tmp_root,
        )
        self.test_root = Path(self.temporary.name).resolve()
        self.fixture_root = self.test_root / "b2b4-real-host-20350102T030405Z"
        self.created_at = datetime(2035, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        self.assertEqual(SCHEMA_VERSION, FIXTURE_DATABASE_SCHEMA_VERSION)
        prepared = prepare_fixture(
            self.fixture_root,
            created_at=self.created_at,
        )
        self.manifest = json.loads(canonical_json(prepared))
        self._assert_exact_v20_authority_fixture()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_exact_v20_authority_fixture(self) -> None:
        database = self.fixture_root / "authority" / "media.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            self.assertEqual(
                MediaRepository._schema_preflight(connection),
                FIXTURE_DATABASE_SCHEMA_VERSION,
            )
            MediaRepository._verify_v16_physical_schema(connection)
            MediaRepository._verify_v17_physical_schema(connection)
            MediaRepository._verify_v18_physical_schema(connection)
            MediaRepository._verify_v19_physical_schema(connection)
            MediaRepository._verify_v20_physical_schema(connection)
            self.assertEqual(
                [
                    tuple(row)
                    for row in connection.execute(
                        "SELECT version,name,checksum FROM schema_migrations "
                        "WHERE version IN (16,17,18,19,20) ORDER BY version"
                    ).fetchall()
                ],
                [
                    (16, "provider_egress_one_shot_authority", V16_CHECKSUM),
                    (17, "canonical_image_read_cutover", V17_CHECKSUM),
                    (18, "canonical_timeline_structural_edit", V18_CHECKSUM),
                    (19, "canonical_export_output_root_anchor", V19_CHECKSUM),
                    (20, "plugin_first_library_bootstrap", V20_CHECKSUM),
                ],
            )
            expected_objects = set(
                (
                    *V16_SCHEMA_OBJECTS,
                    *V17_SCHEMA_OBJECTS,
                    *_V18_SCHEMA_OBJECTS,
                    *_V19_SCHEMA_OBJECTS,
                    *_V20_SCHEMA_OBJECTS,
                )
            )
            object_names = tuple(name for _object_type, name in expected_objects)
            actual_objects = {
                (str(row["type"]), str(row["name"]))
                for row in connection.execute(
                    f"SELECT type,name FROM sqlite_schema WHERE name IN "
                    f"({','.join('?' for _name in object_names)})",
                    object_names,
                ).fetchall()
            }
            self.assertEqual(actual_objects, expected_objects)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provider_egress_grants").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provider_egress_manifests").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM library_bootstrap_receipts").fetchone()[0],
                0,
            )

            generations = connection.execute(
                """SELECT generation.id,
                          generation.canonical_high_water_position,
                          generation.status,generation.is_active,
                          read_manifest.previous_generation_id,
                          read_manifest.eligible_count,
                          read_manifest.projected_count,
                          read_manifest.alias_count,
                          read_manifest.missing_count,
                          read_manifest.unexpected_count,
                          read_manifest.mismatched_count,
                          read_manifest.blocked_count,
                          read_manifest.manifest_sha256=canonical_manifest.manifest_sha256
                            AS digest_equal,
                          read_manifest.manifest_json=canonical_manifest.manifest_json
                            AS manifest_equal
                     FROM image_projection_generations generation
                     JOIN image_projection_read_manifests read_manifest
                       ON read_manifest.generation_id=generation.id
                     JOIN image_projection_manifests canonical_manifest
                       ON canonical_manifest.generation_id=generation.id
                    ORDER BY generation.canonical_high_water_position"""
            ).fetchall()
            self.assertEqual(len(generations), 4)
            self.assertEqual(
                [int(row["canonical_high_water_position"]) for row in generations],
                [1, 2, 3, 4],
            )
            self.assertEqual(
                [(str(row["status"]), int(row["is_active"])) for row in generations],
                [("complete", 0), ("complete", 0), ("complete", 0), ("complete", 1)],
            )
            self.assertIsNone(generations[0]["previous_generation_id"])
            for previous, current in zip(
                generations[:-1],
                generations[1:],
                strict=True,
            ):
                self.assertEqual(current["previous_generation_id"], previous["id"])
            self.assertEqual(
                [
                    (
                        int(row["eligible_count"]),
                        int(row["projected_count"]),
                        int(row["alias_count"]),
                        int(row["missing_count"]),
                        int(row["unexpected_count"]),
                        int(row["mismatched_count"]),
                        int(row["blocked_count"]),
                        int(row["digest_equal"]),
                        int(row["manifest_equal"]),
                    )
                    for row in generations
                ],
                [
                    (1, 1, 0, 0, 0, 0, 0, 1, 1),
                    (2, 2, 0, 0, 0, 0, 0, 1, 1),
                    (3, 3, 0, 0, 0, 0, 0, 1, 1),
                    (4, 4, 0, 0, 0, 0, 0, 1, 1),
                ],
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_prepare_creates_private_current_v20_wal_canonical_projects(self) -> None:
        self.assertEqual(stat.S_IMODE(self.fixture_root.stat().st_mode), 0o700)
        self.assertFalse(self.manifest["acceptance_claimed"])
        self.assertEqual(
            self.manifest["evidence_scope"],
            "fixture_only_not_real_host_acceptance",
        )
        self.assertEqual(self.manifest["created_at"], "2035-01-02T03:04:05Z")
        self.assertNotIn(str(self.fixture_root), canonical_json(self.manifest))

        manifest_on_disk = json.loads((self.fixture_root / MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest_on_disk, self.manifest)
        self.assertEqual(
            stat.S_IMODE((self.fixture_root / MANIFEST_NAME).stat().st_mode),
            0o600,
        )
        for host in ("codex", "deepseek"):
            state_dir = self.fixture_root / "hosts" / host
            self.assertTrue(state_dir.is_dir())
            self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)

        database = self.fixture_root / "authority" / "media.db"
        with closing(sqlite3.connect(database)) as connection:
            self.assertEqual(
                str(connection.execute("PRAGMA journal_mode").fetchone()[0]).casefold(),
                "wal",
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT schema_version FROM database_meta WHERE singleton=1").fetchone(),
                (SCHEMA_VERSION,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone(),
                (2,),
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM image_index").fetchone(),
                (4,),
            )
            self.assertEqual(
                connection.execute("SELECT DISTINCT embedding_backend FROM image_index").fetchall(),
                [("text_derived:semantic_hash",)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT runtime_generation FROM image_analysis_attempt_authorities"
                ).fetchall(),
                [(IMAGE_RUNTIME_GENERATION,)],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT DISTINCT analysis_profile_id,analysis_profile_version,"
                    "analysis_profile_sha256 FROM image_analysis_job_bindings"
                ).fetchall(),
                [
                    (
                        IMAGE_ANALYSIS_PROFILE["profile_id"],
                        IMAGE_ANALYSIS_PROFILE["profile_version"],
                        IMAGE_ANALYSIS_PROFILE["profile_sha256"],
                    )
                ],
            )

        repository = MediaRepository(database)
        try:
            projects = self.manifest["projects"]
            self.assertEqual(
                [project["direction"] for project in projects],
                list(DIRECTIONS),
            )
            for project in projects:
                project_id = project["project_id"]
                self.assertEqual(project["initial_heads"]["blueprint"]["revision"], 1)
                self.assertEqual(project["initial_heads"]["coverage"]["revision"], 1)
                self.assertEqual(project["initial_heads"]["timeline"]["revision"], 1)
                self.assertEqual(len(project["editable_image_clips"]), 2)
                self.assertEqual(repository.get_blueprint_head(project_id)["revision"], 1)
                self.assertEqual(repository.get_coverage_head(project_id)["revision"], 1)
                timeline = repository.get_canonical_timeline_head(project_id)
                self.assertEqual(timeline["revision"], 1)
                self.assertEqual(
                    [clip["media_kind"] for clip in timeline["timeline"]["tracks"][0]["clips"]],
                    ["image", "image"],
                )
                for clip in project["editable_image_clips"]:
                    current = repository.get_current_image_analysis(clip["asset_id"])
                    self.assertIsNotNone(current)
                    assert current is not None
                    self.assertEqual(
                        (current["projection"]["status"], current["projection"]["outcome"]),
                        ("current", "applied"),
                    )
                    self.assertEqual(
                        current["result"]["analysis_profile"],
                        {
                            "id": IMAGE_ANALYSIS_PROFILE["profile_id"],
                            "version": IMAGE_ANALYSIS_PROFILE["profile_version"],
                            "content_sha256": IMAGE_ANALYSIS_PROFILE["profile_sha256"],
                        },
                    )
                    self.assertEqual(
                        current["result"]["stages"]["embedding"]["output"]["vectors"][0]["model_id"],
                        "semantic_hash",
                    )
        finally:
            repository.close()

        png_files = sorted((self.fixture_root / "library").glob("*.png"))
        self.assertEqual(len(png_files), 4)
        for image in png_files:
            self.assertEqual(image.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_collector_cold_audits_and_writes_path_free_summary(self) -> None:
        output = self.test_root / "committable-evidence.json"
        summary = collect_evidence(
            self.fixture_root,
            collected_at="2035-01-02T04:05:06Z",
            output=output,
        )
        encoded = canonical_json(summary)
        self.assertNotIn(str(self.fixture_root), encoded)
        self.assertNotIn(str(self.test_root), encoded)
        self.assertEqual(summary["collected_at"], "2035-01-02T04:05:06Z")
        self.assertEqual(
            summary["database_schema_version"],
            FIXTURE_DATABASE_SCHEMA_VERSION,
        )
        self.assertEqual(summary["journal_mode"], "wal")
        self.assertEqual(summary["host_state_modes"], {"codex": "0700", "deepseek": "0700"})
        self.assertEqual(
            summary["foreign_key_check"],
            {"ok": True, "violation_count": 0},
        )
        self.assertFalse(summary["manual_acceptance"]["claimed"])
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), summary)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o644)

        for project in summary["projects"]:
            self.assertEqual(project["ledger_progress"], "baseline_ready")
            self.assertEqual(project["blueprint"]["head"]["revision"], 1)
            self.assertEqual(len(project["blueprint"]["operations"]), 1)
            self.assertEqual(len(project["blueprint"]["desktop_receipts"]), 1)
            self.assertEqual(project["coverage"]["head"]["revision"], 1)
            self.assertEqual(len(project["coverage"]["operations"]), 1)
            self.assertEqual(len(project["coverage"]["desktop_receipts"]), 1)
            self.assertEqual(project["timeline"]["current_head"]["revision"], 1)
            self.assertEqual(len(project["timeline"]["operations"]), 1)
            self.assertEqual(len(project["timeline"]["desktop_receipts"]), 1)
            self.assertEqual(project["timeline"]["paired_receipts"], [])
            self.assertEqual(len(project["timeline"]["current_image_clips"]), 2)

    def test_collector_rejects_weakened_host_state_permissions(self) -> None:
        state_dir = self.fixture_root / "hosts" / "codex"
        state_dir.chmod(0o755)
        with self.assertRaisesRegex(
            EvidenceCollectionError,
            "codex state directory permissions changed",
        ):
            collect_evidence(self.fixture_root)

    def test_collector_fails_closed_on_canonical_receipt_tamper(self) -> None:
        database = self.fixture_root / "authority" / "media.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("DROP TRIGGER trg_canonical_timeline_receipts_no_update")
            connection.execute(
                "UPDATE canonical_timeline_receipts SET response_sha256=?",
                ("0" * 64,),
            )
            connection.commit()
        with self.assertRaisesRegex(
            EvidenceCollectionError,
            "canonical production audit failed",
        ):
            collect_evidence(self.fixture_root)

    def test_prepare_is_non_overwriting_and_default_name_is_utc(self) -> None:
        with self.assertRaises(FileExistsError):
            prepare_fixture(self.fixture_root)
        expected = PROJECT_ROOT / "tmp" / "b2b4-real-host-20350102T030405Z"
        self.assertEqual(default_fixture_root(now=self.created_at), expected.resolve())


if __name__ == "__main__":
    unittest.main()
