from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.src.media.residual_parent import ResidualParentResolutionError
from backend.src.media.timeline import TimelineService, TimelineValidationError
from core.db import ImageIndexRepository
from core.media_db import (
    CanonicalExportIntegrityError,
    MediaRepository,
    content_sha256,
    utc_now_iso,
)
from core.residual_identity_contract import (
    build_residual_binding,
    canonical_usage_projection_sha256,
    source_binding_sha256,
)


class ResidualLegacyTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-residual-timeline-")
        root = Path(self.temporary.name)
        self.library = root / "library"
        self.library.mkdir()
        self.db_path = root / "state" / "media.db"
        self.db_path.parent.mkdir()
        ImageIndexRepository(self.db_path).ensure_schema()
        self.repository = MediaRepository(self.db_path)
        self.repository.ensure_schema(self.library)
        self.candidate, self.parent_id, self.exact_source_id = self._seed_residual()
        self._facts_patch = patch.object(
            self.repository,
            "canonical_material_search_facts_in_transaction",
            side_effect=lambda _connection, _asset_ids: deepcopy(self.canonical_facts),
        )
        self._facts_patch.start()

    def tearDown(self) -> None:
        self._facts_patch.stop()
        self.repository.close()
        self.temporary.cleanup()

    def _seed_residual(self) -> tuple[dict[str, object], str, str]:
        content = b"residual-source-bytes"
        digest = hashlib.sha256(content).hexdigest()
        root_id = str(self.repository.register_library_root(self.library)["id"])
        source_ids: list[str] = []
        for name in ("preferred.mp4", "renamed-copy.mp4"):
            path = self.library / name
            path.write_bytes(content)
            observed = path.stat()
            registered = self.repository.upsert_asset_source(
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
            source_ids.append(str(registered["asset_source_id"]))
        asset_id = f"asset_{digest[:24]}"
        self.repository.update_asset_probe(
            asset_id,
            {
                "duration_ms": 60_000,
                "width": 1920,
                "height": 1080,
                "rotation_degrees": 0,
                "codec": {"video": "fixture"},
                "captured_at": None,
            },
        )
        preferred_source_id, exact_source_id = source_ids
        now = utc_now_iso()
        run_id = f"arun_{'c' * 32}"
        parent_id = f"seg_{digest[:24]}_1_0"
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE asset_sources SET is_preferred=CASE WHEN id=? THEN 1 ELSE 0 END WHERE asset_id=?",
                (preferred_source_id, asset_id),
            )
            connection.execute(
                """INSERT INTO analysis_runs(
                     id,asset_id,revision,run_kind,analysis_profile_id,
                     analysis_profile_json,input_asset_sha256,status,
                     transcript_status,visual_status,created_at)
                   VALUES(?,?,1,'initial','residual-timeline-test','{}',?,
                          'succeeded','available','ready',?)""",
                (run_id, asset_id, digest, now),
            )
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                     boundary_reason,semantic_json,combined_text,visual_status,
                     transcript_status,created_at)
                   VALUES(?,?,?,0,0,60000,'residual-timeline-test','{}','memory',
                          'ready','available',?)""",
                (parent_id, asset_id, run_id, now),
            )
            connection.execute(
                """INSERT INTO asset_analysis_heads(
                     asset_id,analysis_run_id,updated_at) VALUES(?,?,?)""",
                (asset_id, run_id, now),
            )
            source = connection.execute(
                """SELECT id,asset_id,library_root_id,observed_size,
                          observed_mtime_ns,source_file_id
                     FROM asset_sources WHERE id=?""",
                (exact_source_id,),
            ).fetchone()
        assert source is not None
        source_digest = source_binding_sha256(
            {
                "asset_source_id": str(source["id"]),
                "asset_id": str(source["asset_id"]),
                "asset_sha256": digest,
                "library_root_id": str(source["library_root_id"]),
                "observed_size": source["observed_size"],
                "observed_mtime_ns": source["observed_mtime_ns"],
                "source_file_id": source["source_file_id"],
            }
        )
        usage = {
            "asset_id": asset_id,
            "media_kind": "video",
            "occurrence_count": 1,
            "used": True,
            "used_in": [{"project_id": "project_prior", "export_revision": 1}],
            "source_domain": [0, 60_000],
            "candidate_domain": [0, 60_000],
            "used_intervals": [[10_000, 14_000]],
            "residual_intervals": [[0, 10_000], [14_000, 60_000]],
            "fully_used": False,
            "has_residual": True,
        }
        usage_occurrences = [
            {
                "project_id": "project_prior",
                "export_revision": 1,
                "ordinal": 0,
                "clip_id": "clip_prior",
                "asset_id": asset_id,
                "asset_sha256": digest,
                "asset_source_id": exact_source_id,
                "media_kind": "video",
                "timeline_start_ms": 0,
                "timeline_end_ms": 4_000,
                "source_start_ms": 10_000,
                "source_end_ms": 14_000,
                "source_binding_sha256": source_digest,
                "occurrence_sha256": "e" * 64,
                "created_at": now,
            }
        ]
        self.canonical_facts = {
            "usage_occurrences": usage_occurrences,
            "derivative_facts": [],
        }
        binding = build_residual_binding(
            parent_segment_id=parent_id,
            asset_id=asset_id,
            asset_sha256=digest,
            asset_source_id=exact_source_id,
            source_binding_sha256=source_digest,
            analysis_run_id=run_id,
            analysis_revision=1,
            input_asset_sha256=digest,
            parent_start_ms=0,
            parent_end_ms=60_000,
            source_in_ms=0,
            source_out_ms=10_000,
            usage_revision=content_sha256(usage_occurrences),
            parent_usage_projection_sha256=canonical_usage_projection_sha256(usage),
        )
        return (
            {
                "object": "creative_asset_match",
                "result_type": "video_segment",
                "id": binding["residual_id"],
                "parent_segment_id": parent_id,
                "asset_id": asset_id,
                "asset_sha256": digest,
                "asset_source_id": exact_source_id,
                "filename": "renamed-copy.mp4",
                "start_ms": 0,
                "end_ms": 10_000,
                "residual_binding": binding,
            },
            parent_id,
            exact_source_id,
        )

    def _create_project(self) -> str:
        project = self.repository.create_project(
            "Residual edit",
            {
                "goal": "Use only the remaining interval",
                "duration_ms": 10_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [self.candidate],
            },
            {"source": "residual-test"},
        )
        return str(project["id"])

    def test_initial_timeline_uses_parent_row_exact_source_and_exact_interval(self) -> None:
        project_id = self._create_project()

        created = TimelineService(self.repository).create_from_project(project_id)

        self.assertTrue(created["validation"]["valid"])
        clip = created["timeline"]["tracks"][0]["clips"][0]
        self.assertEqual(clip["segment_id"], self.parent_id)
        self.assertEqual(clip["asset_source_id"], self.exact_source_id)
        self.assertEqual((clip["source_in_ms"], clip["source_out_ms"]), (0, 10_000))
        self.assertEqual(clip["provenance"]["match_id"], self.candidate["id"])
        self.assertEqual(
            clip["provenance"]["residual_binding"],
            self.candidate["residual_binding"],
        )
        with self.repository.transaction() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM video_segments WHERE id=?",
                    (self.candidate["id"],),
                ).fetchone()
            )

    def test_trim_stays_inside_residual_and_cannot_expand_into_used_parent_range(self) -> None:
        project_id = self._create_project()
        service = TimelineService(self.repository)
        created = service.create_from_project(project_id)
        timeline = created["timeline"]
        clip = timeline["tracks"][0]["clips"][0]
        base = {
            "op": "trim_clip",
            "clip_id": clip["id"],
            "preconditions": {"timeline_revision": 1},
        }

        inside = service.preview_revision(
            str(timeline["id"]),
            base_revision=1,
            operations=[{**base, "source_in_ms": 1_000, "source_out_ms": 10_000}],
        )
        self.assertTrue(inside["validation"]["valid"])
        with self.assertRaises(TimelineValidationError) as raised:
            service.preview_revision(
                str(timeline["id"]),
                base_revision=1,
                operations=[{**base, "source_in_ms": 0, "source_out_ms": 11_000}],
            )
        self.assertIn(
            "residual_candidate_mismatch",
            {str(item["code"]) for item in raised.exception.errors},
        )

    def test_exact_nonpreferred_source_is_required_and_unavailability_is_conflict(self) -> None:
        project_id = self._create_project()
        self.repository.mark_source_availability(self.exact_source_id, "missing")

        with self.assertRaises(ResidualParentResolutionError) as raised:
            TimelineService(self.repository).create_from_project(project_id)

        self.assertEqual(raised.exception.code, "residual_parent_stale")

    def test_usage_revision_change_rejects_legacy_lowering_before_write(self) -> None:
        project_id = self._create_project()
        changed = deepcopy(self.canonical_facts["usage_occurrences"])
        changed.append(
            {
                **changed[0],
                "export_revision": 2,
                "ordinal": 1,
                "clip_id": "clip_concurrent",
                "timeline_start_ms": 4_000,
                "timeline_end_ms": 14_000,
                "source_start_ms": 0,
                "source_end_ms": 10_000,
                "occurrence_sha256": "f" * 64,
            }
        )
        self.canonical_facts["usage_occurrences"] = changed

        with self.assertRaises(ResidualParentResolutionError) as raised:
            TimelineService(self.repository).create_from_project(project_id)

        self.assertEqual(raised.exception.code, "residual_usage_stale")
        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM timelines WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )

    def test_derivative_fact_rejects_legacy_lowering_before_write(self) -> None:
        project_id = self._create_project()
        self.canonical_facts["derivative_facts"] = [
            {
                "project_id": "project_export",
                "export_revision": 1,
                "video_sha256": self.candidate["asset_sha256"],
            }
        ]

        with self.assertRaises(ResidualParentResolutionError) as raised:
            TimelineService(self.repository).create_from_project(project_id)

        self.assertEqual(raised.exception.code, "residual_export_derivative")
        with self.repository.transaction() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM timelines WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                0,
            )

    def test_persisted_rseg_namespace_is_never_executable_as_analyzed_media(self) -> None:
        forged_id = f"rseg_{'9' * 64}"
        now = utc_now_iso()
        binding = self.candidate["residual_binding"]
        with self.repository.transaction(immediate=True) as connection:
            connection.execute(
                """INSERT INTO video_segments(
                     id,asset_id,analysis_run_id,ordinal,start_ms,end_ms,
                     boundary_reason,semantic_json,combined_text,visual_status,
                     transcript_status,created_at)
                   VALUES(?,?,?,1,20000,30000,'forged-residual-row','{}','forged',
                          'ready','available',?)""",
                (
                    forged_id,
                    binding["asset_id"],
                    binding["analysis_run_id"],
                    now,
                ),
            )

        with self.assertRaises(CanonicalExportIntegrityError) as search_error:
            self.repository.mixed_candidates()
        self.assertEqual(
            str(search_error.exception),
            "synthetic_residual_segment_persisted",
        )
        self.assertIsNone(self.repository.get_segment(forged_id))

        forged_match = {
            "id": forged_id,
            "asset_id": binding["asset_id"],
            "asset_source_id": binding["asset_source_id"],
            "result_type": "video_segment",
            "start_ms": 20_000,
            "end_ms": 30_000,
        }
        project = self.repository.create_project(
            "Forged residual must fail closed",
            {
                "goal": "Never execute a persisted synthetic identity",
                "duration_ms": 10_000,
                "aspect_ratio": "16:9",
                "candidate_refs": [forged_match],
            },
            {"source": "residual-forgery-test"},
        )
        with self.assertRaises(ResidualParentResolutionError) as timeline_error:
            TimelineService(self.repository).create_from_project(str(project["id"]))
        self.assertEqual(timeline_error.exception.code, "residual_binding_required")


if __name__ == "__main__":
    unittest.main()
