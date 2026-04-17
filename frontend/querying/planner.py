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
- Never include markdown or extra explanation.
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
    "一",
    "一些",
    "一下",
    "一种",
    "一种",
    "不",
    "不要",
    "不是",
    "不用",
    "人物",
    "人像",
    "人",
    "们",
    "图",
    "图片",
    "场景",
    "张",
    "帮",
    "帮我",
    "找",
    "找找",
    "挑",
    "挑出",
    "照片",
    "照",
    "给我",
    "自然",
    "要",
    "选",
    "选出",
    "这类",
    "这种",
    "那种",
    "里",
    "风光",
    "风景",
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
    r"今天",
    r"昨天",
    r"最近半年",
    r"最近一个月",
    r"最近一周",
    r"上周",
    r"上个月",
    r"去年",
    r"今年",
    r"(19|20)\d{2}年",
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
    "春",
    "夏",
    "秋",
    "冬",
    "优先",
    "最好",
    "或者",
    "同时",
    "然后",
]
LOCAL_EXCLUSION_TERM_MAP: list[tuple[list[str], list[str]]] = [
    (["不包含人像", "不要人像", "别要人像", "不带人像"], ["person", "people", "portrait", "human"]),
    (["不包含人物", "不要人物", "别有人物"], ["person", "people", "human"]),
    (["不包含人", "不要人", "没有人", "无人", "没人"], ["person", "people", "human"]),
    (["不包含脸", "不要脸部", "不要脸"], ["face", "portrait"]),
]
LOCAL_SEMANTIC_TERM_MAP: list[tuple[str, list[str]]] = [
    ("beach", ["海边", "海", "beach", "coast", "ocean"]),
    ("landscape", ["自然风光", "风景", "风景照", "景色", "landscape", "scenery"]),
    ("nature", ["自然风光", "大自然", "自然景色", "nature"]),
    ("scenery", ["自然风光", "风景", "scenery"]),
    ("quiet", ["安静", "安静一点", "quiet", "calm"]),
    ("soft", ["温柔", "柔和", "soft", "gentle"]),
    ("daily", ["日常", "生活感", "daily"]),
    ("portrait", ["人物", "某个人", "一个人", "portrait"]),
    ("friends", ["和朋友", "朋友们", "friends"]),
    ("walk", ["散步", "走路", "walk"]),
    ("coffee", ["咖啡", "coffee", "cafe"]),
    ("city", ["城市", "街头", "city", "street"]),
    ("sunset", ["日落", "傍晚", "sunset"]),
    ("travel", ["旅行", "度假", "travel", "trip"]),
    ("nature", ["森林", "花园", "湖", "山", "nature"]),
    ("bridge", ["桥", "大桥", "bridge"]),
    ("fog", ["雾", "fog", "mist"]),
    ("food", ["吃饭", "美食", "食物", "food", "dish"]),
    ("dining", ["聚餐", "用餐", "餐桌", "dining", "meal"]),
    ("restaurant", ["餐厅", "饭店", "restaurant"]),
]
LOCAL_LOCATION_TERMS: list[tuple[str, list[str]]] = [
    ("los angeles", ["洛杉矶", "los angeles", " la "]),
    ("santa monica", ["圣塔莫尼卡", "santa monica"]),
    ("malibu", ["马里布", "malibu"]),
    ("san francisco", ["旧金山", "san francisco", "sf "]),
]
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

        if not required_terms and date_from is None and date_to is None:
            return RetrievalPlan(
                can_fulfill=False,
                reason="Cannot fulfill your request.",
                query=None,
            )

        top_k = top_k_override if isinstance(top_k_override, int) and top_k_override > 0 else 9
        descriptive_query = (
            f"photo of {' '.join(required_terms[:8])}" if required_terms else normalized_text
        )

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

        if "today" in lowered or "今天" in text:
            return day_bounds(reference_datetime)
        if "yesterday" in lowered or "昨天" in text:
            return day_bounds(reference_datetime - timedelta(days=1))
        if "last week" in lowered or "最近一周" in text or "上周" in text:
            start = reference_datetime - timedelta(days=7)
            return start.isoformat(), reference_datetime.isoformat()
        if "this month" in lowered:
            start = reference_datetime.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            return start.isoformat(), reference_datetime.isoformat()
        if "最近一个月" in text:
            start = reference_datetime - timedelta(days=30)
            return start.isoformat(), reference_datetime.isoformat()
        if "last month" in lowered or "上个月" in text:
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
        if "this year" in lowered or "今年" in text:
            start = reference_datetime.replace(
                month=1,
                day=1,
                hour=0,
                minute=0,
                second=0,
                microsecond=0,
            )
            return start.isoformat(), reference_datetime.isoformat()
        if "last year" in lowered or "去年" in text:
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
        if "最近半年" in text:
            start = reference_datetime - timedelta(days=183)
            return start.isoformat(), reference_datetime.isoformat()

        explicit_year = re.search(r"\bin\s+((?:19|20)\d{2})\b", lowered)
        explicit_year_cn = re.search(r"((?:19|20)\d{2})年", text)
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
    def _extract_location_text(text: str) -> str | None:
        lowered_text = f" {text.lower()} "
        for location_text, phrases in LOCAL_LOCATION_TERMS:
            if any(phrase in text or phrase in lowered_text for phrase in phrases):
                return location_text
        return None
