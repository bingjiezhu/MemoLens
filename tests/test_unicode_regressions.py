from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.src.retrieval.planner import (
    SEARCH_INSPIRATION_PROMPT,
    OpenAICompatibleQueryPlanner,
)
from core.photo_atlas import (
    common_label,
    english_terms,
    english_text,
    extract_concepts,
    humanize_concept,
    normalize_concept,
    slugify,
)


class PhotoAtlasUnicodeRegressionTests(unittest.TestCase):
    def test_unicode_metadata_is_trimmed_without_being_replaced(self) -> None:
        self.assertEqual(english_text("  北京   故宫  ", "Memory"), "北京 故宫")
        self.assertEqual(english_text("  ", "Memory"), "Memory")
        self.assertEqual(common_label(["北京", " 北京 ", "München"]), "北京")
        self.assertEqual(humanize_concept("夜景"), "夜景")
        self.assertEqual(slugify("München 東京"), "münchen-東京")

    def test_unicode_tags_and_short_han_concepts_are_preserved_and_deduplicated(self) -> None:
        self.assertEqual(
            english_terms([" 夜景 ", "夜景", " café ", "café"]),
            ["夜景", "café"],
        )
        self.assertEqual(normalize_concept("星空"), "星空")
        self.assertEqual(extract_concepts(["雪", "星空", "café"], ""), ["雪", "星空", "café"])


class QueryPlannerUnicodeRegressionTests(unittest.TestCase):
    def test_local_required_terms_accept_unicode_words(self) -> None:
        terms = OpenAICompatibleQueryPlanner._extract_required_terms(
            text="帮我 北京 故宫 雪 café 夜景 照片",
            excluded_terms=[],
        )

        self.assertEqual(terms, ["北京", "故宫", "雪", "café", "夜景"])

    def test_fallback_suggestions_keep_unicode_context(self) -> None:
        planner = object.__new__(OpenAICompatibleQueryPlanner)
        suggestions = planner._fallback_search_suggestions(
            library_summary={
                "top_concepts": ["夜景", "夜景", "café"],
                "places": ["北京"],
            },
            memories=[{"label": "故宫之夜", "top_concepts": ["红墙", "灯笼"]}],
            context_assets=[{"tags": ["星空", "星空", "München"]}],
            count=8,
        )

        self.assertTrue(any("星空" in suggestion for suggestion in suggestions))
        self.assertTrue(any("夜景" in suggestion for suggestion in suggestions))
        self.assertTrue(any("故宫之夜" in suggestion for suggestion in suggestions))
        self.assertTrue(any("北京" in suggestion for suggestion in suggestions))

    def test_exclusion_parser_keeps_unicode_terms(self) -> None:
        self.assertEqual(
            OpenAICompatibleQueryPlanner._extract_excluded_terms("without café"),
            ["café"],
        )

    def test_suggestion_prompt_does_not_require_english(self) -> None:
        self.assertNotIn("English-only", SEARCH_INSPIRATION_PROMPT)
        self.assertNotIn("English words", SEARCH_INSPIRATION_PROMPT)

    def test_model_suggestions_do_not_apply_an_english_length_floor(self) -> None:
        settings = SimpleNamespace(query_provider="vertex", query_api_key=None)
        planner = OpenAICompatibleQueryPlanner(settings)
        with patch.object(
            planner,
            "_request_inspiration_content",
            return_value='{"suggestions": ["雪", "夜景", "café"]}',
        ):
            suggestions = planner.generate_search_suggestions(
                library_summary={},
                memories=[],
                count=3,
            )

        self.assertEqual(suggestions, ["雪", "夜景", "café"])


if __name__ == "__main__":
    unittest.main()
