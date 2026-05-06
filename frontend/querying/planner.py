from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
import re

from openai import OpenAI

from core.config import Settings
from core.llm_utils import (
    coerce_json_object,
    create_openai_client,
    extract_vertex_response_text,
    request_minimax_chat_completion,
    request_vertex_generate_content,
)
from core.schemas import RetrievalPlan, StructuredRetrievalQuery


QUERY_PLANNER_PROMPT = """You convert natural-language photo search requests into strict JSON
for a local image retrieval system.

Current datetime: {current_datetime}

Return ONLY one JSON object with this schema:
{{
  "can_fulfill": true,
  "reason": null,
  "query": {{
    "top_k": 9,
    "date_from": "ISO8601 or null",
    "date_to": "ISO8601 or null",
    "location_text": "string or null",
    "descriptive_query": "one short caption-like search sentence",
    "required_terms": ["lowercase term"],
    "optional_terms": ["lowercase term"],
    "excluded_terms": ["lowercase term"]
  }}
}}

If the request cannot be converted into a useful retrieval query, return:
{{
  "can_fulfill": false,
  "reason": "Cannot fulfill your request.",
  "query": null
}}

Rules:
- Resolve relative dates like "last December" using the provided current datetime.
- Use ISO8601 timestamps for date_from and date_to.
- Rewrite the request into a short descriptive_query that looks like an image caption or visual search sentence.
- descriptive_query should focus on visible content and scene details, not on conversational wording like "help me find".
- Keep terms short, lowercase, and retrieval-friendly.
- If a phrase is important, keep it as one term if possible.
- Use location_text for place constraints like "San Diego Zoo".
- Treat negative constraints such as "no people", "without people", "excluding people",
  and equivalent user wording as excluded_terms, not required_terms.
- For no-person requests, include broad human-presence exclusions such as
  people, person, human, portrait, face, selfie, man, woman, child, children,
  boy, girl, and adult.
- Never include markdown or extra explanation.
"""

SEARCH_INSPIRATION_PROMPT = """You are an AI search buddy for a private local photo library.

You receive only sanitized library facts: common themes, rough time range, place labels,
and memory summaries. You never receive original photos, file paths, database paths, API
configuration, or full-library raw text.

Return ONLY one JSON object with this schema:
{
  "suggestions": ["query", "query", "query"]
}

Rules:
- Generate creative natural-language photo search queries the user could paste into a photo retrieval box.
- Each query should ask for a useful set, not a single asset.
- Prefer concrete visual constraints: theme, mood, people/no-people, diversity, posting/story use.
- Return English-only suggestions.
- Keep each query concise, ideally 5-14 English words.
- Do not mention metadata, embeddings, indexes, databases, or model behavior.
- Do not include markdown or extra explanation.
"""

LOCAL_QUERY_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "around",
    "at",
    "during",
    "find",
    "for",
    "from",
    "help",
    "i",
    "image",
    "images",
    "in",
    "last",
    "me",
    "month",
    "my",
    "near",
    "of",
    "on",
    "photo",
    "photos",
    "picture",
    "pictures",
    "please",
    "show",
    "that",
    "the",
    "this",
    "today",
    "week",
    "with",
    "year",
    "yesterday",
    "\u4e00",
    "\u4e00\u4e9b",
    "\u4e00\u4e0b",
    "\u4e00\u79cd",
    "\u4e0d",
    "\u4e0d\u8981",
    "\u4e0d\u662f",
    "\u4e0d\u7528",
    "\u4eba\u7269",
    "\u4eba\u50cf",
    "\u4eba",
    "\u4eec",
    "\u56fe",
    "\u56fe\u7247",
    "\u573a\u666f",
    "\u5f20",
    "\u5e2e",
    "\u5e2e\u6211",
    "\u627e",
    "\u627e\u627e",
    "\u6311",
    "\u6311\u51fa",
    "\u7167\u7247",
    "\u7167",
    "\u7ed9\u6211",
    "\u81ea\u7136",
    "\u8981",
    "\u9009",
    "\u9009\u51fa",
    "\u8fd9\u7c7b",
    "\u8fd9\u79cd",
    "\u90a3\u79cd",
    "\u91cc",
    "\u98ce\u5149",
    "\u98ce\u666f",
}
LOCAL_DATE_PATTERNS = [
    r"\btoday\b",
    r"\byesterday\b",
    r"\blast\s+week\b",
    r"\blast\s+month\b",
    r"\blast\s+year\b",
    r"\bthis\s+month\b",
    r"\bthis\s+year\b",
    r"\bin\s+(19|20)\d{2}\b",
    "\u4eca\u5929",
    "\u6628\u5929",
    "\u6700\u8fd1\u534a\u5e74",
    "\u6700\u8fd1\u4e00\u4e2a\u6708",
    "\u6700\u8fd1\u4e00\u5468",
    "\u4e0a\u5468",
    "\u4e0a\u4e2a\u6708",
    "\u53bb\u5e74",
    "\u4eca\u5e74",
    "(19|20)\\d{2}\u5e74",
]
LOCAL_EXCLUSION_PATTERN = re.compile(
    r"\b(?:without|excluding|except|not)\s+([a-z0-9-]+(?:\s+[a-z0-9-]+){0,2})",
    re.IGNORECASE,
)
LOCAL_COMPLEX_QUERY_MARKERS = [
    "spring",
    "summer",
    "autumn",
    "fall",
    "winter",
    "\u6625",
    "\u590f",
    "\u79cb",
    "\u51ac",
    "\u4f18\u5148",
    "\u6700\u597d",
    "\u6216\u8005",
    "\u540c\u65f6",
    "\u7136\u540e",
]
LOCAL_HUMAN_PRESENCE_TERMS = [
    "person",
    "people",
    "human",
    "portrait",
    "face",
    "selfie",
    "man",
    "men",
    "woman",
    "women",
    "child",
    "children",
    "kid",
    "kids",
    "baby",
    "babies",
    "toddler",
    "adult",
    "boy",
    "girl",
    "couple",
    "crowd",
    "barista",
    "server",
    "waiter",
    "waitress",
    "customer",
    "worker",
    "staff",
    "tourist",
    "passenger",
    "driver",
    "chef",
    "cook",
    "student",
    "visitor",
]
LOCAL_HUMAN_PRESENCE_TERM_SET = set(LOCAL_HUMAN_PRESENCE_TERMS)
LOCAL_EXCLUSION_TERM_MAP: list[tuple[list[str], list[str]]] = [
    (
        ["\u4e0d\u5305\u542b\u4eba\u50cf", "\u4e0d\u8981\u4eba\u50cf", "\u522b\u8981\u4eba\u50cf", "\u4e0d\u5e26\u4eba\u50cf", "\u4e0d\u8981\u6709\u4eba\u50cf"],
        LOCAL_HUMAN_PRESENCE_TERMS,
    ),
    (
        ["\u4e0d\u5305\u542b\u4eba\u7269", "\u4e0d\u8981\u4eba\u7269", "\u522b\u6709\u4eba\u7269", "\u4e0d\u8981\u6709\u4eba\u7269", "\u4e0d\u5e26\u4eba\u7269"],
        LOCAL_HUMAN_PRESENCE_TERMS,
    ),
    (
        [
            "\u4e0d\u5305\u542b\u4eba",
            "\u4e0d\u8981\u4eba",
            "\u4e0d\u8981\u6709\u4eba",
            "\u4e0d\u5e26\u4eba",
            "\u522b\u5e26\u4eba",
            "\u6ca1\u6709\u4eba",
            "\u4e0d\u80fd\u6709\u4eba",
            "\u4e0d\u51fa\u73b0\u4eba",
            "\u65e0\u4eba",
            "\u6ca1\u4eba",
            "\u907f\u5f00\u4eba",
            "\u6392\u9664\u4eba",
            "no people",
            "no person",
            "without people",
            "without person",
        ],
        LOCAL_HUMAN_PRESENCE_TERMS,
    ),
    (["\u4e0d\u5305\u542b\u8138", "\u4e0d\u8981\u8138\u90e8", "\u4e0d\u8981\u8138"], ["face", "portrait", "selfie"]),
]
LOCAL_SEMANTIC_TERM_MAP: list[tuple[str, list[str]]] = [
    (
        "mountain",
        [
            "\u5c71\u666f",
            "\u5c71\u666f\u7167",
            "\u5c71",
            "\u5c71\u8109",
            "\u5c71\u5cf0",
            "\u96ea\u5c71",
            "\u5ca9\u5c71",
            "\u7fa4\u5c71",
            "mountain",
            "mountains",
            "mountain view",
            "mountain landscape",
            "mountain range",
            "peak",
            "summit",
        ],
    ),
    ("beach", ["\u6d77\u8fb9", "\u6d77", "beach", "coast", "ocean"]),
    (
        "landscape",
        [
            "\u5c71\u666f",
            "\u5c71\u666f\u7167",
            "mountain view",
            "mountain landscape",
            "\u81ea\u7136\u98ce\u5149",
            "\u98ce\u666f",
            "\u98ce\u666f\u7167",
            "\u666f\u8272",
            "landscape",
            "scenery",
        ],
    ),
    ("nature", ["\u81ea\u7136\u98ce\u5149", "\u5927\u81ea\u7136", "\u81ea\u7136\u666f\u8272", "nature"]),
    ("scenery", ["\u81ea\u7136\u98ce\u5149", "\u98ce\u666f", "scenery"]),
    ("quiet", ["\u5b89\u9759", "\u5b89\u9759\u4e00\u70b9", "quiet", "calm"]),
    ("soft", ["\u6e29\u67d4", "\u67d4\u548c", "soft", "gentle"]),
    ("daily", ["\u65e5\u5e38", "\u751f\u6d3b\u611f", "daily"]),
    ("portrait", ["\u4eba\u7269", "\u67d0\u4e2a\u4eba", "\u4e00\u4e2a\u4eba", "portrait"]),
    ("friends", ["\u548c\u670b\u53cb", "\u670b\u53cb\u4eec", "friends"]),
    ("walk", ["\u6563\u6b65", "\u8d70\u8def", "walk"]),
    ("coffee", ["\u5496\u5561", "coffee", "cafe"]),
    ("city", ["\u57ce\u5e02", "\u8857\u5934", "city", "street"]),
    ("sunset", ["\u65e5\u843d", "\u508d\u665a", "sunset"]),
    ("travel", ["\u65c5\u884c", "\u5ea6\u5047", "travel", "trip"]),
    ("nature", ["\u68ee\u6797", "\u82b1\u56ed", "\u6e56", "\u5c71", "nature"]),
    ("bridge", ["\u6865", "\u5927\u6865", "bridge"]),
    ("fog", ["\u96fe", "fog", "mist"]),
    ("food", ["\u5403\u996d", "\u7f8e\u98df", "\u98df\u7269", "food", "dish"]),
    ("dining", ["\u805a\u9910", "\u7528\u9910", "\u9910\u684c", "dining", "meal"]),
    ("restaurant", ["\u9910\u5385", "\u996d\u5e97", "restaurant"]),
]
LOCAL_LOCATION_TERMS: list[tuple[str, list[str]]] = [
    ("los angeles", ["\u6d1b\u6749\u77f6", "los angeles", " la "]),
    ("santa monica", ["\u5723\u5854\u83ab\u5c3c\u5361", "santa monica"]),
    ("malibu", ["\u9a6c\u91cc\u5e03", "malibu"]),
    ("san francisco", ["\u65e7\u91d1\u5c71", "san francisco", "sf "]),
]
HAN_TEXT_RE = re.compile("[\u3400-\u9fff]")
PLANNER_CACHE_LIMIT = 128


class OpenAICompatibleQueryPlanner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: OpenAI | None = None
        self._plan_cache: OrderedDict[tuple[str, int | None, str], RetrievalPlan] = OrderedDict()

    def plan(
        self,
        text: str,
        current_datetime: str,
        top_k_override: int | None = None,
    ) -> RetrievalPlan:
        cache_key = self._cache_key(
            text=text,
            current_datetime=current_datetime,
            top_k_override=top_k_override,
        )
        cached = self._plan_cache.get(cache_key)
        if cached is not None:
            self._plan_cache.move_to_end(cache_key)
            return cached

        fallback_plan = self._fallback_plan(
            text=text,
            current_datetime=current_datetime,
            top_k_override=top_k_override,
        )
        if self._should_use_fast_local_plan(text=text, fallback_plan=fallback_plan):
            self._store_plan_cache(cache_key, fallback_plan)
            return fallback_plan
        if self.settings.query_provider != "vertex" and not self.settings.query_api_key:
            self._store_plan_cache(cache_key, fallback_plan)
            return fallback_plan

        try:
            content = self._request_planning_content(
                text=text,
                current_datetime=current_datetime,
            )
            parsed = coerce_json_object(content)
            if not parsed.get("can_fulfill"):
                self._store_plan_cache(cache_key, fallback_plan)
                return fallback_plan

            raw_query = parsed.get("query")
            if not isinstance(raw_query, dict):
                self._store_plan_cache(cache_key, fallback_plan)
                return fallback_plan

            top_k = top_k_override if top_k_override is not None else raw_query.get("top_k", 9)
            if not isinstance(top_k, int) or top_k <= 0:
                top_k = 9

            location_text = self._normalize_optional_text(raw_query.get("location_text"))
            descriptive_query = self._normalize_optional_text(raw_query.get("descriptive_query"))
            required_terms = self._normalize_terms(raw_query.get("required_terms"))
            optional_terms = self._normalize_terms(raw_query.get("optional_terms"))
            excluded_terms = self._normalize_terms(raw_query.get("excluded_terms"))
            if fallback_plan.query is not None:
                required_terms = self._merge_unique_terms(
                    required_terms,
                    fallback_plan.query.required_terms,
                )
                optional_terms = self._merge_unique_terms(
                    optional_terms,
                    fallback_plan.query.optional_terms,
                )
                excluded_terms = self._merge_unique_terms(
                    excluded_terms,
                    fallback_plan.query.excluded_terms,
                )
            if descriptive_query is None:
                descriptive_query = self._build_fallback_descriptive_query(
                    original_text=text,
                    location_text=location_text,
                    required_terms=required_terms,
                    optional_terms=optional_terms,
                )

            result = RetrievalPlan(
                can_fulfill=True,
                reason=None,
                query=StructuredRetrievalQuery(
                    top_k=top_k,
                    date_from=self._normalize_optional_text(raw_query.get("date_from")),
                    date_to=self._normalize_optional_text(raw_query.get("date_to")),
                    location_text=location_text,
                    descriptive_query=descriptive_query,
                    required_terms=required_terms,
                    optional_terms=optional_terms,
                    excluded_terms=excluded_terms,
                ),
            )
            self._store_plan_cache(cache_key, result)
            return result
        except Exception:
            self._store_plan_cache(cache_key, fallback_plan)
            return fallback_plan

    def generate_search_suggestions(
        self,
        *,
        library_summary: dict[str, object],
        memories: list[dict[str, object]],
        context_assets: list[dict[str, object]] | None = None,
        count: int = 5,
    ) -> list[str]:
        desired_count = max(3, min(int(count or 5), 8))
        context_assets = context_assets or []
        fallback_suggestions = self._fallback_search_suggestions(
            library_summary=library_summary,
            memories=memories,
            context_assets=context_assets,
            count=desired_count,
        )
        if self.settings.query_provider != "vertex" and not self.settings.query_api_key:
            return fallback_suggestions

        safe_summary = {
            "top_concepts": list(library_summary.get("top_concepts") or [])[:12],
            "places": list(library_summary.get("places") or [])[:8],
            "time_range": library_summary.get("time_range") or {},
            "asset_count": library_summary.get("asset_count"),
            "memory_count": library_summary.get("memory_count"),
            "quality_avg": library_summary.get("quality_avg"),
            "people_risk_count": library_summary.get("people_risk_count"),
        }
        safe_memories = [
            {
                "label": memory.get("label"),
                "asset_count": memory.get("asset_count"),
                "time_label": memory.get("time_label"),
                "place_label": memory.get("place_label"),
                "top_concepts": list(memory.get("top_concepts") or [])[:5],
                "people_risk": memory.get("people_risk"),
            }
            for memory in memories[:10]
        ]
        safe_context_assets = [
            {
                "title": asset.get("title") or asset.get("filename"),
                "taken_at": asset.get("taken_at"),
                "place_name": asset.get("place_name"),
                "tags": list(asset.get("tags") or [])[:8],
                "description": str(asset.get("description") or "")[:220],
                "quality_score": asset.get("quality_score"),
                "people_risk": asset.get("people_risk"),
            }
            for asset in context_assets[:12]
        ]
        user_content = (
            f"Generate {desired_count} search suggestions from this private local photo library summary.\n"
            f"Library summary:\n{safe_summary}\n\n"
            f"Representative memories:\n{safe_memories}\n\n"
            f"User-selected visual context photos:\n{safe_context_assets}"
        )

        try:
            content = self._request_inspiration_content(
                user_content=user_content,
                count=desired_count,
            )
            parsed = coerce_json_object(content)
            raw_suggestions = parsed.get("suggestions")
            if not isinstance(raw_suggestions, list):
                return fallback_suggestions
            suggestions = []
            for item in raw_suggestions:
                suggestion = re.sub(r"\s+", " ", str(item or "").strip())
                if 4 <= len(suggestion) <= 120:
                    suggestions.append(suggestion)
            merged = self._merge_unique_terms(suggestions, fallback_suggestions)
            return merged[:desired_count]
        except Exception:
            return fallback_suggestions

    def _request_inspiration_content(
        self,
        *,
        user_content: str,
        count: int,
    ) -> str:
        if self.settings.query_provider == "minimax":
            response = request_minimax_chat_completion(
                api_key=self.settings.query_api_key,
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=max(0.35, min(0.9, self.settings.query_temperature or 0.7)),
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=[
                    {
                        "role": "system",
                        "content": SEARCH_INSPIRATION_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
            )
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("MiniMax response did not contain choices.")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            return str(content or "")
        if self.settings.query_provider == "vertex":
            response = request_vertex_generate_content(
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=0.65,
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=[
                    {
                        "role": "system",
                        "content": SEARCH_INSPIRATION_PROMPT,
                    },
                    {
                        "role": "user",
                        "content": user_content,
                    },
                ],
            )
            return extract_vertex_response_text(response)

        response = self._get_client().chat.completions.create(
            model=self.settings.query_model,
            temperature=0.65,
            response_format=self.settings.query_response_format,
            max_tokens=self.settings.query_max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": SEARCH_INSPIRATION_PROMPT,
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
        )
        return str(response.choices[0].message.content or "")

    def _fallback_search_suggestions(
        self,
        *,
        library_summary: dict[str, object],
        memories: list[dict[str, object]],
        context_assets: list[dict[str, object]] | None = None,
        count: int,
    ) -> list[str]:
        context_assets = context_assets or []
        context_terms = []
        for asset in context_assets[:12]:
            for term in list(asset.get("tags") or [])[:6]:
                cleaned = str(term).strip()
                if cleaned and not HAN_TEXT_RE.search(cleaned) and cleaned not in context_terms:
                    context_terms.append(cleaned)
        concepts = [
            str(term).strip()
            for term in list(library_summary.get("top_concepts") or [])
            if str(term).strip() and not HAN_TEXT_RE.search(str(term))
        ]
        places = [
            str(place).strip()
            for place in list(library_summary.get("places") or [])
            if str(place).strip() and not HAN_TEXT_RE.search(str(place))
        ]
        suggestions: list[str] = []
        if context_terms:
            suggestions.append(
                f"Find more photos like the selected set, with {', '.join(context_terms[:3])} and low repetition"
            )
            suggestions.append(
                f"Expand the selected photos into a nine-photo set with a {context_terms[0]} mood"
            )
            suggestions.append("Keep pure landscapes similar to the selected photos, without people")
        if concepts:
            suggestions.append(f"Find 9 {concepts[0]} photos with low repetition for a social post")
        if len(concepts) >= 2:
            suggestions.append(f"Build a quiet storyline around {concepts[0]} and {concepts[1]}")
        for memory in memories[:4]:
            memory_concepts = [
                str(term).strip()
                for term in list(memory.get("top_concepts") or [])[:3]
                if str(term).strip() and not HAN_TEXT_RE.search(str(term))
            ]
            label = str(memory.get("label") or "").strip()
            if label and not HAN_TEXT_RE.search(label) and memory_concepts:
                suggestions.append(
                    f"Pick 9 photos from {label}, emphasizing {', '.join(memory_concepts)}, with low repetition"
                )
        if places:
            suggestions.append(f"Find a travel fragment around {places[0]} with varied compositions")
        suggestions.append("Suggest 3 publishable story themes from recent photos")
        suggestions.append("Find 9 mountain landscape photos without people and with low repetition")
        return list(dict.fromkeys(suggestions))[:count]

    def _request_planning_content(
        self,
        *,
        text: str,
        current_datetime: str,
    ) -> str:
        if self.settings.query_provider == "minimax":
            response = request_minimax_chat_completion(
                api_key=self.settings.query_api_key,
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=max(0.1, min(1.0, self.settings.query_temperature)),
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=[
                    {
                        "role": "system",
                        "content": QUERY_PLANNER_PROMPT.format(current_datetime=current_datetime),
                    },
                    {
                        "role": "user",
                        "content": text,
                    },
                ],
            )
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("MiniMax response did not contain choices.")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            return str(content or "")
        if self.settings.query_provider == "vertex":
            response = request_vertex_generate_content(
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=0.0,
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=[
                    {
                        "role": "system",
                        "content": QUERY_PLANNER_PROMPT.format(current_datetime=current_datetime),
                    },
                    {
                        "role": "user",
                        "content": text,
                    },
                ],
            )
            return extract_vertex_response_text(response)

        response = self._get_client().chat.completions.create(
            model=self.settings.query_model,
            temperature=0.0,
            response_format=self.settings.query_response_format,
            max_tokens=self.settings.query_max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": QUERY_PLANNER_PROMPT.format(current_datetime=current_datetime),
                },
                {
                    "role": "user",
                    "content": text,
                },
            ],
        )
        return str(response.choices[0].message.content or "")

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = create_openai_client(
                api_key=self.settings.query_api_key,
                base_url=self.settings.query_base_url,
            )
        return self._client

    def _cache_key(
        self,
        *,
        text: str,
        current_datetime: str,
        top_k_override: int | None,
    ) -> tuple[str, int | None, str]:
        normalized_text = re.sub(r"\s+", " ", text.strip())
        reference_date = self._parse_current_datetime(current_datetime).date().isoformat()
        return normalized_text, top_k_override, reference_date

    def _store_plan_cache(
        self,
        cache_key: tuple[str, int | None, str],
        plan: RetrievalPlan,
    ) -> None:
        self._plan_cache[cache_key] = plan
        self._plan_cache.move_to_end(cache_key)
        while len(self._plan_cache) > PLANNER_CACHE_LIMIT:
            self._plan_cache.popitem(last=False)

    @staticmethod
    def _should_use_fast_local_plan(
        *,
        text: str,
        fallback_plan: RetrievalPlan,
    ) -> bool:
        if not fallback_plan.can_fulfill or fallback_plan.query is None:
            return False

        if any(marker in text.lower() or marker in text for marker in LOCAL_COMPLEX_QUERY_MARKERS):
            return False

        query = fallback_plan.query
        return bool(
            query.required_terms
            or query.excluded_terms
            or query.location_text
            or query.date_from
            or query.date_to
        )

    def _fallback_plan(
        self,
        *,
        text: str,
        current_datetime: str,
        top_k_override: int | None,
    ) -> RetrievalPlan:
        normalized_text = re.sub(r"\s+", " ", text.strip())
        if not normalized_text:
            return RetrievalPlan(
                can_fulfill=False,
                reason="Cannot fulfill your request.",
                query=None,
            )

        reference_datetime = self._parse_current_datetime(current_datetime)
        date_from, date_to = self._extract_date_range(
            text=normalized_text,
            reference_datetime=reference_datetime,
        )
        excluded_terms = self._extract_excluded_terms(normalized_text)
        term_source = self._strip_date_phrases(normalized_text)
        term_source = self._strip_excluded_phrases(term_source)
        location_text = self._extract_location_text(term_source)
        required_terms = self._extract_required_terms(
            text=term_source,
            excluded_terms=excluded_terms,
        )
        if self._has_human_presence_exclusion(excluded_terms) and "no people" not in required_terms:
            required_terms.append("no people")

        if not required_terms and not excluded_terms and date_from is None and date_to is None:
            return RetrievalPlan(
                can_fulfill=False,
                reason="Cannot fulfill your request.",
                query=None,
            )

        top_k = top_k_override if isinstance(top_k_override, int) and top_k_override > 0 else 9
        if required_terms:
            descriptive_query = f"photo of {' '.join(required_terms[:8])}"
        elif excluded_terms:
            descriptive_query = f"photo without {' '.join(excluded_terms[:8])}"
        else:
            descriptive_query = normalized_text

        return RetrievalPlan(
            can_fulfill=True,
            reason=None,
            query=StructuredRetrievalQuery(
                top_k=top_k,
                date_from=date_from,
                date_to=date_to,
                location_text=location_text,
                descriptive_query=descriptive_query,
                required_terms=required_terms,
                optional_terms=[],
                excluded_terms=excluded_terms,
            ),
        )

    @staticmethod
    def _normalize_optional_text(value) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _normalize_terms(value) -> list[str]:
        if not isinstance(value, list):
            return []

        seen: list[str] = []
        for item in value:
            normalized = re.sub(r"\s+", " ", str(item).strip().lower())
            if normalized and normalized not in seen:
                seen.append(normalized)
        return seen

    @staticmethod
    def _merge_unique_terms(primary: list[str], secondary: list[str]) -> list[str]:
        merged: list[str] = []
        for term in primary + secondary:
            normalized = re.sub(r"\s+", " ", str(term).strip().lower())
            if normalized and normalized not in merged:
                merged.append(normalized)
        return merged

    @staticmethod
    def _build_fallback_descriptive_query(
        *,
        original_text: str,
        location_text: str | None,
        required_terms: list[str],
        optional_terms: list[str],
    ) -> str | None:
        terms = required_terms + [term for term in optional_terms if term not in required_terms]
        if terms:
            caption = f"photo of {' '.join(terms[:8])}"
            if location_text:
                caption += f" at {location_text}"
            return caption

        normalized_text = re.sub(r"\s+", " ", original_text.strip())
        return normalized_text or None

    @staticmethod
    def _parse_current_datetime(current_datetime: str) -> datetime:
        try:
            return datetime.fromisoformat(current_datetime)
        except ValueError:
            return datetime.now().astimezone()

    @staticmethod
    def _extract_date_range(
        *,
        text: str,
        reference_datetime: datetime,
    ) -> tuple[str | None, str | None]:
        lowered = text.lower()

        def day_bounds(target: datetime) -> tuple[str, str]:
            start = target.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1) - timedelta(microseconds=1)
            return start.isoformat(), end.isoformat()

        if "today" in lowered or "\u4eca\u5929" in text:
            return day_bounds(reference_datetime)
        if "yesterday" in lowered or "\u6628\u5929" in text:
            return day_bounds(reference_datetime - timedelta(days=1))
        if "last week" in lowered or "\u6700\u8fd1\u4e00\u5468" in text or "\u4e0a\u5468" in text:
            start = reference_datetime - timedelta(days=7)
            return start.isoformat(), reference_datetime.isoformat()
        if "this month" in lowered:
            start = reference_datetime.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return start.isoformat(), reference_datetime.isoformat()
        if "\u6700\u8fd1\u4e00\u4e2a\u6708" in text:
            start = reference_datetime - timedelta(days=30)
            return start.isoformat(), reference_datetime.isoformat()
        if "last month" in lowered or "\u4e0a\u4e2a\u6708" in text:
            current_month_start = reference_datetime.replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            previous_month_end = current_month_start - timedelta(microseconds=1)
            previous_month_start = previous_month_end.replace(
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            return previous_month_start.isoformat(), previous_month_end.isoformat()
        if "this year" in lowered or "\u4eca\u5e74" in text:
            start = reference_datetime.replace(
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            return start.isoformat(), reference_datetime.isoformat()
        if "last year" in lowered or "\u53bb\u5e74" in text:
            previous_year = reference_datetime.year - 1
            start = reference_datetime.replace(
                year=previous_year,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            end = reference_datetime.replace(
                year=previous_year,
                month=12,
                day=31,
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
            )
            return start.isoformat(), end.isoformat()
        if "\u6700\u8fd1\u534a\u5e74" in text:
            start = reference_datetime - timedelta(days=183)
            return start.isoformat(), reference_datetime.isoformat()

        explicit_year = re.search(r"\bin\s+((?:19|20)\d{2})\b", lowered)
        explicit_year_cn = re.search("((?:19|20)\\d{2})\u5e74", text)
        year_match = explicit_year or explicit_year_cn
        if year_match:
            year = int(year_match.group(1))
            start = reference_datetime.replace(
                year=year,
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            end = reference_datetime.replace(
                year=year,
                month=12,
                day=31,
                hour=23,
                minute=59,
                second=59,
                microsecond=999999,
            )
            return start.isoformat(), end.isoformat()

        return None, None

    @staticmethod
    def _extract_excluded_terms(text: str) -> list[str]:
        excluded_terms: list[str] = []
        lowered_text = text.lower()
        for match in LOCAL_EXCLUSION_PATTERN.finditer(text):
            candidate = re.sub(r"\s+", " ", match.group(1).strip().lower())
            if candidate and candidate not in excluded_terms:
                excluded_terms.append(candidate)
        for phrases, mapped_terms in LOCAL_EXCLUSION_TERM_MAP:
            if any(phrase in text or phrase in lowered_text for phrase in phrases):
                for term in mapped_terms:
                    if term not in excluded_terms:
                        excluded_terms.append(term)
        return excluded_terms

    @staticmethod
    def _strip_date_phrases(text: str) -> str:
        stripped = text
        for pattern in LOCAL_DATE_PATTERNS:
            stripped = re.sub(pattern, " ", stripped, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", stripped).strip()

    @staticmethod
    def _strip_excluded_phrases(text: str) -> str:
        stripped = LOCAL_EXCLUSION_PATTERN.sub(" ", text)
        for phrases, _mapped_terms in LOCAL_EXCLUSION_TERM_MAP:
            for phrase in sorted(phrases, key=len, reverse=True):
                stripped = re.sub(re.escape(phrase), " ", stripped, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", stripped).strip()

    @staticmethod
    def _extract_required_terms(
        *,
        text: str,
        excluded_terms: list[str],
    ) -> list[str]:
        excluded_tokens = {
            token
            for phrase in excluded_terms
            for token in re.findall(r"[a-z0-9]+", phrase.lower())
        }
        required_terms: list[str] = []
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            if token in LOCAL_QUERY_STOPWORDS:
                continue
            if token in excluded_tokens:
                continue
            if len(token) <= 1:
                continue
            if token not in required_terms:
                required_terms.append(token)

        lowered_text = f" {text.lower()} "
        for canonical_term, phrases in LOCAL_SEMANTIC_TERM_MAP:
            if any(phrase in text or phrase in lowered_text for phrase in phrases):
                if canonical_term not in excluded_tokens and canonical_term not in required_terms:
                    required_terms.append(canonical_term)
        return required_terms

    @staticmethod
    def _has_human_presence_exclusion(excluded_terms: list[str]) -> bool:
        normalized_terms = {
            re.sub(r"\s+", " ", str(term).strip().lower())
            for term in excluded_terms
            if str(term).strip()
        }
        return bool(normalized_terms.intersection(LOCAL_HUMAN_PRESENCE_TERM_SET))

    @staticmethod
    def _extract_location_text(text: str) -> str | None:
        lowered_text = f" {text.lower()} "
        for location_text, phrases in LOCAL_LOCATION_TERMS:
            if any(phrase in text or phrase in lowered_text for phrase in phrases):
                return location_text
        return None
