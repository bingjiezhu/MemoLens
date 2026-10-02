from __future__ import annotations

import random
import unittest

from backend.src.media.usage_projection import (
    UsageQueryPolicy,
    UsageSelection,
    annotate_usage_candidates,
    project_asset_usage,
    project_candidate_usage,
    residual_half_open_intervals,
    union_half_open_intervals,
)
from backend.src.media.retrieval import MixedRetrievalService
from core.media_db import CanonicalExportIntegrityError, content_sha256
from tests.test_usage_brief_admission import _image_observation


def _occurrence(
    *,
    project_id: str,
    export_revision: int,
    asset_id: str = "asset_video",
    start_ms: int = 0,
    end_ms: int = 1_000,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "export_revision": export_revision,
        "asset_id": asset_id,
        "media_kind": "video",
        "source_start_ms": start_ms,
        "source_end_ms": end_ms,
    }


class CanonicalUsageProjectionTests(unittest.TestCase):
    def test_derivative_sha_exclusion_has_no_executable_search_bypass(self) -> None:
        class Repository:
            derivative_facts = [
                {
                    "project_id": "project_one",
                    "export_revision": 1,
                    "export_revision_sha256": "c" * 64,
                    "job_id": "job_one",
                    "operation_id": "operation_one",
                    "output_role": "canonical_rendered_video",
                    "video_sha256": "a" * 64,
                }
            ]

            @staticmethod
            def mixed_candidates(include_archived: bool = False):
                del include_archived
                common = {
                    "result_type": "video_segment",
                    "asset_source_id": "source_video",
                    "filename": "keyword.mp4",
                    "summary": "keyword",
                    "combined_text": "keyword",
                    "tags": [],
                    "analysis_run_id": "run_one",
                    "analysis_revision": 1,
                    "source_availability": "available",
                    "width": 1920,
                    "height": 1080,
                    "duration_ms": 60_000,
                    "start_ms": 0,
                    "end_ms": 10_000,
                    "captured_at": None,
                }
                return [
                    {
                        **common,
                        "id": "derivative",
                        "asset_id": "asset_derivative",
                        "asset_sha256": "a" * 64,
                    },
                    {
                        **common,
                        "id": "retained",
                        "asset_id": "asset_retained",
                        "asset_sha256": "b" * 64,
                    },
                ], {}

            @classmethod
            def canonical_material_search_facts(cls, _asset_ids):
                return {
                    "usage_occurrences": [],
                    "derivative_facts": list(cls.derivative_facts),
                    "derivative_revision": content_sha256(cls.derivative_facts),
                }

        retrieval = MixedRetrievalService(Repository())
        policies = (
            {},
            {"unused_only": True},
            {"prefer_unused": True, "allow_reuse": True},
            {"allow_reuse": True},
            {"residual_of": "asset_retained"},
        )
        first_search_revision = None
        first_derivative_revision = None
        for filters in policies:
            with self.subTest(filters=filters):
                response = retrieval.search(
                    {
                        "query": "keyword",
                        "types": ["video"],
                        "filters": filters,
                    }
                )
                self.assertEqual(
                    [item["id"] for item in response["results"]],
                    ["retained"],
                )
                self.assertEqual(response["results"][0]["asset_sha256"], "b" * 64)
                self.assertRegex(response["derivative_revision"], r"^[0-9a-f]{64}$")
                if filters == {}:
                    first_search_revision = response["search_revision"]
                    first_derivative_revision = response["derivative_revision"]
        self.assertEqual(
            [item["id"] for item in retrieval.resolve_matches(["derivative", "retained"])],
            ["retained"],
        )
        self.assertEqual(
            retrieval.constraint_conflicts(
                ["derivative"],
                required_terms=["keyword"],
                excluded_terms=[],
            ),
            [
                {
                    "candidate_ref": None,
                    "missing_required_terms": ["keyword"],
                    "matched_excluded_terms": [],
                }
            ],
        )

        Repository.derivative_facts.append(
            {
                **Repository.derivative_facts[0],
                "project_id": "project_two",
                "job_id": "job_two",
                "operation_id": "operation_two",
            }
        )
        second = retrieval.search({"query": "keyword", "types": ["video"]})
        self.assertNotEqual(second["derivative_revision"], first_derivative_revision)
        self.assertNotEqual(second["search_revision"], first_search_revision)
        reversed_facts = list(reversed(Repository.derivative_facts))
        with self.assertRaisesRegex(
            CanonicalExportIntegrityError,
            "canonical_export_derivative_projection_invalid",
        ):
            retrieval._validated_material_search_facts(
                {
                    "usage_occurrences": [],
                    "derivative_facts": reversed_facts,
                    "derivative_revision": content_sha256(reversed_facts),
                }
            )

    def test_union_and_residual_are_half_open_and_merge_adjacency(self) -> None:
        used = union_half_open_intervals(
            [(10, 20), (0, 5), (5, 10), (18, 30), (40, 50)],
            domain=(0, 45),
        )

        self.assertEqual(used, [(0, 30), (40, 45)])
        self.assertEqual(
            residual_half_open_intervals((0, 45), used),
            [(30, 40)],
        )

    def test_file_level_used_video_preserves_remainder_and_candidate_explanation(self) -> None:
        projection = project_asset_usage(
            asset_id="asset_video",
            media_kind="video",
            duration_ms=60_000,
            occurrences=[
                _occurrence(
                    project_id="project_one",
                    export_revision=1,
                    start_ms=10_000,
                    end_ms=14_000,
                ),
                _occurrence(
                    project_id="project_two",
                    export_revision=3,
                    start_ms=12_000,
                    end_ms=16_000,
                ),
            ],
        )

        self.assertTrue(projection["used"])
        self.assertEqual(projection["used_intervals"], [[10_000, 16_000]])
        self.assertEqual(
            projection["residual_intervals"],
            [[0, 10_000], [16_000, 60_000]],
        )
        candidate = project_candidate_usage(
            {
                "result_type": "video_segment",
                "asset_id": "asset_video",
                "start_ms": 8_000,
                "end_ms": 20_000,
            },
            projection,
        )
        self.assertEqual(candidate["used_intervals"], [[10_000, 16_000]])
        self.assertEqual(
            candidate["residual_intervals"],
            [[8_000, 10_000], [16_000, 20_000]],
        )
        self.assertTrue(candidate["has_residual"])
        self.assertFalse(candidate["fully_used"])

    def test_used_in_scope_is_derived_without_mutating_global_projection(self) -> None:
        occurrences = [
            _occurrence(project_id="project_one", export_revision=1, start_ms=0, end_ms=10),
            _occurrence(project_id="project_one", export_revision=2, start_ms=20, end_ms=30),
            _occurrence(project_id="project_two", export_revision=1, start_ms=40, end_ms=50),
        ]

        scoped = project_asset_usage(
            asset_id="asset_video",
            media_kind="video",
            duration_ms=100,
            occurrences=occurrences,
            selection=UsageSelection("project_one", 2),
        )
        global_projection = project_asset_usage(
            asset_id="asset_video",
            media_kind="video",
            duration_ms=100,
            occurrences=occurrences,
        )

        self.assertEqual(scoped["used_intervals"], [[20, 30]])
        self.assertEqual(
            scoped["used_in"],
            [{"project_id": "project_one", "export_revision": 2}],
        )
        self.assertEqual(global_projection["used_intervals"], [[0, 10], [20, 30], [40, 50]])

    def test_image_projection_has_occurrence_semantics_without_fake_time(self) -> None:
        occurrence = {
            "project_id": "project_image",
            "export_revision": 1,
            "asset_id": "asset_image",
            "media_kind": "image",
            "source_start_ms": None,
            "source_end_ms": None,
        }
        projection = project_asset_usage(
            asset_id="asset_image",
            media_kind="image",
            duration_ms=None,
            occurrences=[occurrence],
        )
        candidate = project_candidate_usage(
            {"result_type": "image_asset", "asset_id": "asset_image"},
            projection,
        )

        self.assertEqual(projection["used_intervals"], [])
        self.assertEqual(projection["residual_intervals"], [])
        self.assertIsNone(projection["source_domain"])
        self.assertTrue(candidate["fully_used"])
        self.assertFalse(candidate["has_residual"])

    def test_750_adversarial_interval_fixtures_match_point_oracle(self) -> None:
        randomizer = random.Random(0x4D454D4F)
        for fixture in range(750):
            duration = randomizer.randint(1, 400)
            raw: list[tuple[int, int]] = []
            for _ in range(randomizer.randint(0, 80)):
                left = randomizer.randint(0, duration - 1)
                right = randomizer.randint(left + 1, duration)
                raw.append((left, right))
                if randomizer.random() < 0.2:
                    raw.append((left, right))
            union = union_half_open_intervals(raw, domain=(0, duration))
            residual = residual_half_open_intervals((0, duration), raw)
            used_points = {
                point
                for start, end in raw
                for point in range(start, end)
            }
            projected_used = {
                point
                for start, end in union
                for point in range(start, end)
            }
            projected_residual = {
                point
                for start, end in residual
                for point in range(start, end)
            }
            with self.subTest(fixture=fixture):
                self.assertEqual(projected_used, used_points)
                self.assertEqual(projected_residual, set(range(duration)) - used_points)
                self.assertFalse(projected_used & projected_residual)

    def test_malformed_occurrence_never_becomes_false_unused(self) -> None:
        with self.assertRaisesRegex(ValueError, "half-open"):
            project_asset_usage(
                asset_id="asset_video",
                media_kind="video",
                duration_ms=100,
                occurrences=[
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        start_ms=50,
                        end_ms=50,
                    )
                ],
            )

    def test_source_domain_rejects_out_of_bounds_occurrences_and_candidates(self) -> None:
        with self.assertRaisesRegex(ValueError, "source domain"):
            project_asset_usage(
                asset_id="asset_video",
                media_kind="video",
                duration_ms=100,
                occurrences=[
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        start_ms=90,
                        end_ms=101,
                    )
                ],
            )

        projection = project_asset_usage(
            asset_id="asset_video",
            media_kind="video",
            duration_ms=100,
            occurrences=[],
        )
        with self.assertRaisesRegex(ValueError, "source domain"):
            project_candidate_usage(
                {
                    "result_type": "video_segment",
                    "asset_id": "asset_video",
                    "start_ms": 90,
                    "end_ms": 101,
                },
                projection,
            )

    def test_same_asset_candidates_require_one_consistent_source_duration(self) -> None:
        candidates = [
            {
                "result_type": "video_segment",
                "id": "segment_one",
                "asset_id": "asset_video",
                "duration_ms": 100,
                "start_ms": 0,
                "end_ms": 10,
            },
            {
                "result_type": "video_segment",
                "id": "segment_two",
                "asset_id": "asset_video",
                "duration_ms": 101,
                "start_ms": 10,
                "end_ms": 20,
            },
        ]
        with self.assertRaisesRegex(ValueError, "duration is inconsistent"):
            annotate_usage_candidates(
                candidates,
                [],
                UsageQueryPolicy(requested=True),
            )

    def test_search_filters_explain_residual_and_never_drop_video_remainder(self) -> None:
        partial_sha256 = "1" * 64
        partial_asset_id = f"asset_{partial_sha256[:24]}"
        partial_parent_id = f"seg_{partial_sha256[:24]}_1_0"
        full_sha256 = "2" * 64
        full_asset_id = f"asset_{full_sha256[:24]}"

        class Repository:
            @staticmethod
            def mixed_candidates():
                common = {
                    "filename": "keyword.mp4",
                    "summary": "keyword",
                    "combined_text": "keyword",
                    "tags": [],
                    "analysis_revision": 1,
                    "source_availability": "available",
                    "width": 1920,
                    "height": 1080,
                    "duration_ms": 60_000,
                    "captured_at": None,
                }
                return [
                    {
                        **common,
                        "id": partial_parent_id,
                        "asset_id": partial_asset_id,
                        "asset_sha256": partial_sha256,
                        "asset_source_id": f"src_{'3' * 24}",
                        "source_binding_sha256": "4" * 64,
                        "analysis_run_id": f"arun_{'5' * 32}",
                        "input_asset_sha256": partial_sha256,
                        "result_type": "video_segment",
                        "start_ms": 8_000,
                        "end_ms": 20_000,
                    },
                    {
                        **common,
                        "id": f"seg_{full_sha256[:24]}_1_0",
                        "asset_id": full_asset_id,
                        "asset_sha256": full_sha256,
                        "asset_source_id": f"src_{'6' * 24}",
                        "source_binding_sha256": "7" * 64,
                        "analysis_run_id": f"arun_{'8' * 32}",
                        "input_asset_sha256": full_sha256,
                        "result_type": "video_segment",
                        "start_ms": 0,
                        "end_ms": 10_000,
                    },
                ], {}

            @staticmethod
            def canonical_usage_occurrences_for_search(_asset_ids):
                return [
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        asset_id=partial_asset_id,
                        start_ms=10_000,
                        end_ms=14_000,
                    ),
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        asset_id=full_asset_id,
                        start_ms=0,
                        end_ms=10_000,
                    ),
                ]

        response = MixedRetrievalService(Repository()).search(
            {
                "query": "keyword",
                "types": ["video"],
                "filters": {"unused_only": True},
            }
        )
        results = response["results"]

        self.assertRegex(response["usage_revision"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            sorted((result["start_ms"], result["end_ms"]) for result in results),
            [(8_000, 10_000), (14_000, 20_000)],
        )
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertRegex(result["id"], r"^rseg_[0-9a-f]{64}$")
            self.assertEqual(result["parent_segment_id"], partial_parent_id)
            self.assertEqual(
                result["thumbnail_url"],
                f"/v1/video-segments/{partial_parent_id}/thumbnail",
            )
            self.assertEqual(result["usage"]["used_intervals"], [])
            self.assertEqual(
                result["usage"]["residual_intervals"],
                [[result["start_ms"], result["end_ms"]]],
            )
            self.assertEqual(result["residual_binding"]["residual_id"], result["id"])
        residual_results = MixedRetrievalService(Repository()).search(
            {
                "query": "keyword",
                "types": ["video"],
                "filters": {"residual_of": partial_asset_id},
            }
        )["results"]
        self.assertEqual(
            {result["id"] for result in residual_results},
            {result["id"] for result in results},
        )

    def test_search_used_in_residual_of_and_prefer_unused_have_closed_semantics(self) -> None:
        class Repository:
            @staticmethod
            def mixed_candidates():
                base = {
                    "result_type": "image_asset",
                    "asset_source_id": "source_image",
                    "filename": "keyword.jpg",
                    "summary": "keyword",
                    "combined_text": "keyword",
                    "tags": [],
                    "analysis_run_id": None,
                    "analysis_revision": None,
                    "source_availability": "available",
                    "width": 100,
                    "height": 100,
                    "duration_ms": None,
                    "start_ms": None,
                    "end_ms": None,
                    "captured_at": None,
                }
                def current_image(identifier: str, asset_id: str) -> dict[str, object]:
                    analysis_run_id = f"run_{identifier}"
                    analysis_revision = 1
                    return {
                        **base,
                        "id": identifier,
                        "asset_id": asset_id,
                        "analysis_run_id": analysis_run_id,
                        "analysis_revision": analysis_revision,
                        "analysis_status": "current",
                        "canonical_image_observation": _image_observation(
                            asset_id,
                            analysis_run_id=analysis_run_id,
                            analysis_revision=analysis_revision,
                        ),
                    }

                return [
                    current_image("used", "asset_used"),
                    current_image("unused", "asset_unused"),
                ], {}

            @staticmethod
            def canonical_usage_occurrences_for_search(_asset_ids):
                return [
                    {
                        "project_id": "project_one",
                        "export_revision": 2,
                        "asset_id": "asset_used",
                        "media_kind": "image",
                        "source_start_ms": None,
                        "source_end_ms": None,
                    }
                ]

        retrieval = MixedRetrievalService(Repository())
        preferred = retrieval.search(
            {
                "query": "keyword",
                "types": ["image"],
                "filters": {"prefer_unused": True, "allow_reuse": True},
            }
        )["results"]
        used = retrieval.search(
            {
                "query": "keyword",
                "types": ["image"],
                "filters": {
                    "used_in": {"project_id": "project_one", "export_revision": 2}
                },
            }
        )["results"]

        self.assertEqual([item["id"] for item in preferred], ["unused", "used"])
        self.assertEqual([item["id"] for item in used], ["used"])
        for filters in (
            {"unused_only": True, "allow_reuse": True},
            {"prefer_unused": True, "allow_reuse": False},
            {"used_in": "project_one", "unused_only": True},
            {"used_in": "project_one", "prefer_unused": True},
            {"used_in": "project_one", "residual_of": "asset_used"},
            {"used_in": {"project_id": "project_one", "extra": 1}},
        ):
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                retrieval.search(
                    {"query": "keyword", "types": ["image"], "filters": filters}
                )

    def test_prefer_unused_ranks_an_uncovered_window_of_a_used_video_as_fresh(self) -> None:
        class Repository:
            @staticmethod
            def mixed_candidates():
                common = {
                    "result_type": "video_segment",
                    "asset_source_id": "source_video",
                    "filename": "keyword.mp4",
                    "summary": "keyword",
                    "combined_text": "keyword",
                    "tags": [],
                    "analysis_run_id": "run_one",
                    "analysis_revision": 1,
                    "source_availability": "available",
                    "width": 1920,
                    "height": 1080,
                    "duration_ms": 60_000,
                    "captured_at": None,
                }
                return [
                    {
                        **common,
                        "id": "segment_fully_used",
                        "asset_id": "asset_other",
                        "start_ms": 0,
                        "end_ms": 10_000,
                    },
                    {
                        **common,
                        "id": "segment_fresh_window",
                        "asset_id": "asset_shared",
                        "start_ms": 20_000,
                        "end_ms": 30_000,
                    },
                ], {}

            @staticmethod
            def canonical_usage_occurrences_for_search(_asset_ids):
                return [
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        asset_id="asset_other",
                        start_ms=0,
                        end_ms=10_000,
                    ),
                    _occurrence(
                        project_id="project_one",
                        export_revision=1,
                        asset_id="asset_shared",
                        start_ms=0,
                        end_ms=10_000,
                    ),
                ]

        results = MixedRetrievalService(Repository()).search(
            {
                "query": "keyword",
                "types": ["video"],
                "filters": {"prefer_unused": True, "allow_reuse": True},
            }
        )["results"]

        self.assertEqual(
            [item["id"] for item in results],
            ["segment_fresh_window", "segment_fully_used"],
        )
        self.assertEqual(results[0]["usage"]["used_intervals"], [])


if __name__ == "__main__":
    unittest.main()
