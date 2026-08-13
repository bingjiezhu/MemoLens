from __future__ import annotations

import unittest

from backend.src.media.retrieval import MixedRetrievalService


def _archived_candidate() -> dict[str, object]:
    return {
        "id": "segment-archived",
        "asset_id": "asset-archived",
        "asset_source_id": "source-archived",
        "result_type": "video_segment",
        "filename": "old-trip.mp4",
        "start_ms": 1_000,
        "end_ms": 4_000,
        "summary": "A quiet shoreline",
        "combined_text": "quiet shoreline",
        "analysis_run_id": "analysis-one",
        "analysis_revision": 1,
        "source_availability": "available",
        "review": {
            "revision": 2,
            "inbox_state": "archived",
            "favorite": True,
            "project_ready": False,
            "note": "private note that must not enter search results",
        },
    }


class _Repository:
    def __init__(self) -> None:
        self.include_archived_calls: list[bool] = []

    def mixed_candidates(
        self,
        *,
        include_archived: bool = False,
    ) -> tuple[list[dict[str, object]], dict[str, str]]:
        self.include_archived_calls.append(include_archived)
        return ([_archived_candidate()] if include_archived else []), {}


class CreatorReviewRetrievalTests(unittest.TestCase):
    def test_default_search_excludes_archived_but_explicit_reference_resolves(self) -> None:
        repository = _Repository()
        service = MixedRetrievalService(repository)  # type: ignore[arg-type]

        search = service.search({"query": "shoreline"})
        explicit = service.resolve_matches(["segment-archived"])

        self.assertEqual(search["results"], [])
        self.assertEqual(repository.include_archived_calls, [False, True])
        self.assertEqual(len(explicit), 1)
        self.assertEqual(
            explicit[0]["review"],
            {
                "revision": 2,
                "inbox_state": "archived",
                "favorite": True,
                "project_ready": False,
            },
        )
        self.assertNotIn("note", explicit[0]["review"])


if __name__ == "__main__":
    unittest.main()
