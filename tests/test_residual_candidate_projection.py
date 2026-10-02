from __future__ import annotations

from copy import deepcopy
import unittest

from backend.src.media.mixed_ranking import (
    RankedCandidate,
    rank_candidates,
    select_non_overlapping,
)
from backend.src.media.mixed_search_contract import MixedSearchRequest
from backend.src.media.usage_projection import (
    UsageQueryPolicy,
    UsageSelection,
    materialize_residual_candidates,
)
from core.residual_identity_contract import (
    require_valid_residual_binding,
    source_binding_sha256,
)


ASSET_SHA = "a" * 64
ASSET_ID = f"asset_{ASSET_SHA[:24]}"
SOURCE_ID = f"src_{'b' * 24}"
RUN_ID = f"arun_{'c' * 32}"
PARENT_ID = f"seg_{ASSET_SHA[:24]}_1_0"
USAGE_REVISION = "d" * 64


def _source_binding_sha256() -> str:
    return source_binding_sha256(
        {
            "asset_source_id": SOURCE_ID,
            "asset_id": ASSET_ID,
            "asset_sha256": ASSET_SHA,
            "library_root_id": "root_alpha",
            "observed_size": 123_456,
            "observed_mtime_ns": 987_654_321,
            "source_file_id": "file-alpha",
        }
    )


def _candidate(
    *,
    start_ms: int = 0,
    end_ms: int = 60_000,
    used_intervals: list[list[int]] | None = None,
    residual_intervals: list[list[int]] | None = None,
) -> dict[str, object]:
    used = [[10_000, 14_000]] if used_intervals is None else used_intervals
    residual = (
        [[0, 10_000], [14_000, 60_000]]
        if residual_intervals is None
        else residual_intervals
    )
    return {
        "id": PARENT_ID,
        "result_type": "video_segment",
        "asset_id": ASSET_ID,
        "asset_sha256": ASSET_SHA,
        "asset_source_id": SOURCE_ID,
        "source_binding_sha256": _source_binding_sha256(),
        "analysis_run_id": RUN_ID,
        "analysis_revision": 1,
        "input_asset_sha256": ASSET_SHA,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": 60_000,
        "filename": "sunset.mp4",
        "summary": "sunset over the sea",
        "combined_text": "sunset",
        "tags": ["sunset"],
        "canonical_usage": {
            "asset_id": ASSET_ID,
            "media_kind": "video",
            "occurrence_count": 1 if used else 0,
            "used": bool(used),
            "used_in": (
                [{"project_id": "project_alpha", "export_revision": 1}]
                if used
                else []
            ),
            "source_domain": [0, 60_000],
            "candidate_domain": [start_ms, end_ms],
            "used_intervals": used,
            "residual_intervals": residual,
            "fully_used": bool(used) and not residual,
            "has_residual": bool(residual),
        },
    }


def _policy(**overrides: object) -> UsageQueryPolicy:
    values: dict[str, object] = {
        "requested": True,
        "unused_only": False,
        "prefer_unused": False,
        "allow_reuse": True,
        "used_in": None,
        "residual_of": None,
    }
    values.update(overrides)
    return UsageQueryPolicy(**values)  # type: ignore[arg-type]


def _request(policy: UsageQueryPolicy) -> MixedSearchRequest:
    return MixedSearchRequest(
        query="sunset",
        top_k=3,
        result_types=frozenset({"video_segment"}),
        filters={},
        excluded_terms=(),
        duration_min_ms=None,
        duration_max_ms=None,
        orientation=None,
        usage=policy,
    )


class ResidualCandidateProjectionTests(unittest.TestCase):
    def test_unused_only_replaces_partial_parent_with_exact_identity_children(self) -> None:
        parent = _candidate()
        original = deepcopy(parent)

        projected = materialize_residual_candidates(
            [parent],
            usage_revision=USAGE_REVISION,
            policy=_policy(unused_only=True, allow_reuse=False),
        )

        self.assertEqual(parent, original)
        self.assertEqual(len(projected), 2)
        self.assertEqual(
            [(item["start_ms"], item["end_ms"]) for item in projected],
            [(0, 10_000), (14_000, 60_000)],
        )
        self.assertNotIn(PARENT_ID, {item["id"] for item in projected})
        for item in projected:
            self.assertRegex(str(item["id"]), r"^rseg_[0-9a-f]{64}$")
            self.assertEqual(item["parent_segment_id"], PARENT_ID)
            self.assertEqual(item["canonical_usage"]["used_intervals"], [])
            binding = require_valid_residual_binding(item["residual_binding"])
            self.assertEqual(binding["residual_id"], item["id"])
            self.assertEqual(binding["source_in_ms"], item["start_ms"])
            self.assertEqual(binding["source_out_ms"], item["end_ms"])

    def test_wholly_unused_parent_keeps_persisted_segment_identity(self) -> None:
        parent = _candidate(
            used_intervals=[],
            residual_intervals=[[0, 60_000]],
        )

        projected = materialize_residual_candidates(
            [parent],
            usage_revision=USAGE_REVISION,
            policy=_policy(unused_only=True, allow_reuse=False),
        )

        self.assertEqual(projected, [parent])
        self.assertNotIn("residual_binding", projected[0])

    def test_prefer_unused_keeps_parent_but_ranks_children_first(self) -> None:
        policy = _policy(prefer_unused=True)
        projected = materialize_residual_candidates(
            [_candidate()],
            usage_revision=USAGE_REVISION,
            policy=policy,
        )

        self.assertEqual(len(projected), 3)
        self.assertIn(PARENT_ID, {item["id"] for item in projected})
        ranked = rank_candidates(projected, _request(policy))
        self.assertEqual(ranked[-1].item["id"], PARENT_ID)
        self.assertTrue(
            all(item.item["id"].startswith("rseg_") for item in ranked[:2])
        )

    def test_allow_reuse_does_not_invent_residual_identity(self) -> None:
        parent = _candidate()

        projected = materialize_residual_candidates(
            [parent],
            usage_revision=USAGE_REVISION,
            policy=_policy(),
        )

        self.assertEqual(projected, [parent])

    def test_allow_reuse_false_alone_replaces_partial_parent_with_residuals(self) -> None:
        projected = materialize_residual_candidates(
            [_candidate()],
            usage_revision=USAGE_REVISION,
            policy=_policy(allow_reuse=False),
        )

        self.assertEqual(len(projected), 2)
        self.assertEqual(
            [(item["start_ms"], item["end_ms"]) for item in projected],
            [(0, 10_000), (14_000, 60_000)],
        )
        self.assertTrue(all(str(item["id"]).startswith("rseg_") for item in projected))
        self.assertNotIn(PARENT_ID, {item["id"] for item in projected})

    def test_usage_revision_is_an_identity_dimension(self) -> None:
        first = materialize_residual_candidates(
            [_candidate()],
            usage_revision=USAGE_REVISION,
            policy=_policy(unused_only=True, allow_reuse=False),
        )
        second = materialize_residual_candidates(
            [_candidate()],
            usage_revision="e" * 64,
            policy=_policy(unused_only=True, allow_reuse=False),
        )

        self.assertNotEqual(
            {item["id"] for item in first},
            {item["id"] for item in second},
        )

    def test_missing_source_fact_fails_closed(self) -> None:
        parent = _candidate()
        parent.pop("source_binding_sha256")

        with self.assertRaisesRegex(ValueError, "source binding"):
            materialize_residual_candidates(
                [parent],
                usage_revision=USAGE_REVISION,
                policy=_policy(unused_only=True, allow_reuse=False),
            )

    def test_scoped_usage_cannot_mint_a_global_residual_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "Scoped Usage"):
            materialize_residual_candidates(
                [_candidate()],
                usage_revision=USAGE_REVISION,
                policy=_policy(
                    prefer_unused=True,
                    used_in=UsageSelection(project_id="project_alpha"),
                ),
            )

    def test_adjacent_residual_siblings_are_not_swallowed_by_padding(self) -> None:
        children = materialize_residual_candidates(
            [
                _candidate(
                    start_ms=0,
                    end_ms=300,
                    used_intervals=[[100, 200]],
                    residual_intervals=[[0, 100], [200, 300]],
                )
            ],
            usage_revision=USAGE_REVISION,
            policy=_policy(unused_only=True, allow_reuse=False),
        )
        ranked = [
            RankedCandidate(
                score=1.0,
                item=item,
                matched_terms=("sunset",),
                lexical_score=1.0,
            )
            for item in children
        ]

        selected = select_non_overlapping(ranked, top_k=2)

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            [(item.item["start_ms"], item.item["end_ms"]) for item in selected],
            [(0, 100), (200, 300)],
        )


if __name__ == "__main__":
    unittest.main()
