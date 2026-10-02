from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import tempfile
import unittest
from pathlib import Path

from backend.src.media.director import CreativeBriefError, CreativeDirector
from backend.src.media.usage_projection import (
    UsageQueryPolicy,
    annotate_usage_candidates,
    materialize_residual_candidates,
)
from core.db import ImageIndexRepository
from core.media_db import canonical_json
from tests.test_usage_brief_admission import (
    _AdmissionRepository,
    _Retrieval,
    _video_candidate,
    _video_occurrence,
)


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _ResidualAdmissionRepository(_AdmissionRepository):
    def __init__(self, db_path: Path):
        super().__init__(db_path)
        self.derivative_facts: list[dict[str, object]] = []

    def canonical_material_search_facts_in_transaction(
        self,
        connection,
        asset_ids,
    ) -> dict[str, object]:
        base = super().canonical_material_search_facts_in_transaction(
            connection,
            asset_ids,
        )
        base["derivative_facts"] = deepcopy(self.derivative_facts)
        base["derivative_revision"] = _digest(self.derivative_facts)
        return base


def _selection(
    candidate: dict[str, object],
    occurrences: list[dict[str, object]],
) -> tuple[str, dict[str, object]]:
    usage_revision = _digest(occurrences)
    policy = UsageQueryPolicy(
        requested=True,
        unused_only=True,
        allow_reuse=False,
    )
    annotated = annotate_usage_candidates([candidate], occurrences, policy)
    residuals = materialize_residual_candidates(
        annotated,
        usage_revision=usage_revision,
        policy=policy,
    )
    selected = residuals[0]
    selection = {
        "policy": "unused_only",
        "usage_revision": usage_revision,
        "derivative_revision": _digest([]),
        "candidates": [
            {
                "id": selected["id"],
                "asset_id": selected["asset_id"],
                "asset_source_id": selected["asset_source_id"],
                "result_type": selected["result_type"],
                "analysis_run_id": selected["analysis_run_id"],
                "analysis_revision": selected["analysis_revision"],
                "start_ms": selected["start_ms"],
                "end_ms": selected["end_ms"],
                "usage": selected["canonical_usage"],
                "residual_binding": selected["residual_binding"],
            }
        ],
    }
    return str(selected["id"]), selection


class ResidualBriefAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="memolens-residual-brief-")
        root = Path(self.temporary.name)
        db_path = root / "state" / "media.db"
        db_path.parent.mkdir()
        library = root / "library"
        library.mkdir()
        ImageIndexRepository(db_path).ensure_schema()
        self.repository = _ResidualAdmissionRepository(db_path)
        self.repository.ensure_schema(library)
        self.director = CreativeDirector(
            self.repository,
            _Retrieval(self.repository),  # type: ignore[arg-type]
        )

    def tearDown(self) -> None:
        self.repository.close()
        self.temporary.cleanup()

    def test_exact_residual_is_recomputed_and_frozen_without_synthetic_segment_row(self) -> None:
        candidate = _video_candidate()
        occurrences = [_video_occurrence()]
        self.repository.current_candidates = [candidate]
        self.repository.current_occurrences = occurrences
        residual_id, selection = _selection(candidate, occurrences)
        payload = {
            "goal": "memory",
            "duration_ms": 3_000,
            "candidate_refs": [residual_id],
            "usage_selection": selection,
        }

        project, _search = self.director.create_brief(payload)

        frozen = project["brief"]["usage_selection"]["candidates"][0]
        self.assertEqual(frozen["id"], residual_id)
        self.assertEqual(frozen["residual_binding"], selection["candidates"][0]["residual_binding"])
        self.assertEqual(project["candidates"][0]["parent_segment_id"], candidate["id"])
        self.assertNotEqual(project["candidates"][0]["id"], candidate["id"])
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM video_segments WHERE id=?",
                    (residual_id,),
                ).fetchone()[0],
                0,
            )

    def test_new_successful_output_revision_rejects_old_residual_before_any_write(self) -> None:
        candidate = _video_candidate()
        occurrences = [_video_occurrence()]
        self.repository.current_candidates = [candidate]
        self.repository.current_occurrences = occurrences
        residual_id, selection = _selection(candidate, occurrences)
        self.repository.derivative_facts = [
            {
                "project_id": "another-project",
                "export_revision": 1,
                "export_revision_sha256": "5" * 64,
                "job_id": "export-job",
                "operation_id": "export-operation",
                "output_role": "canonical_rendered_video",
                "video_sha256": "6" * 64,
            }
        ]

        with self.assertRaises(CreativeBriefError) as raised:
            self.director.create_brief(
                {
                    "goal": "memory",
                    "duration_ms": 3_000,
                    "candidate_refs": [residual_id],
                    "usage_selection": selection,
                }
            )

        self.assertEqual(raised.exception.code, "derivative_revision_conflict")
        self.assertEqual(raised.exception.status, 409)
        with closing(self.repository._connect()) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_projects").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM creative_briefs").fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main()
