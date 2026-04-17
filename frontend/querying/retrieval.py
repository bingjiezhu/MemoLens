from __future__ import annotations

from functools import lru_cache
import json
import math
import re
import time
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher

import numpy as np

from core.config import Settings
from core.db import ImageIndexRepository
from core.schemas import RetrievedImageSummary, RetrievalPlan, RetrievalRequest, RetrievalResponse
from core.semantic_hints import expand_text_with_hints
from core.text_embeddings import TextEmbeddingService, build_combined_text

from .planner import OpenAICompatibleQueryPlanner


MMR_LAMBDA = 0.6
CANDIDATE_POOL_FACTOR = 6
MIN_CANDIDATE_POOL = 36
NEAR_DUPLICATE_SIMILARITY = 0.6
DESCRIPTION_SIMILARITY_WEIGHT = 4.0
TEXT_EMBEDDING_WEIGHT = 6.0
TERM_SIMILARITY_WEIGHT = 0.45
EXCLUDED_TERM_WEIGHT = 1.1
EXCLUDED_TERM_HARD_FILTER_THRESHOLD = 0.94
TERM_SIMILARITY_MIN_MATCH = 0.82
TERM_SIMILARITY_SUBSTRING_MATCH = 0.94


class RetrievalService:
    def __init__(
        self,
        settings: Settings,
        repository: ImageIndexRepository,
        planner: OpenAICompatibleQueryPlanner,
        text_embedding_service: TextEmbeddingService,
    ):
        self.settings = settings
        self.repository = repository
        self.planner = planner
        self.text_embedding_service = text_embedding_service

    def run(self, retrieval_request: RetrievalRequest) -> RetrievalResponse:
        current_datetime = datetime.now().astimezone().isoformat()
        try:
            plan = self.planner.plan(
                text=retrieval_request.text,
                current_datetime=current_datetime,
                top_k_override=retrieval_request.top_k,
            )
        except Exception:
            return RetrievalResponse(
                id=f"ret_{int(time.time())}",
                query_text=retrieval_request.text,
                current_datetime=current_datetime,
                parsed_query=None,
                data=[],
                status="cannot_fulfill",
                message="Cannot fulfill your request.",
            )

        if not plan.can_fulfill or plan.query is None:
            return RetrievalResponse(
                id=f"ret_{int(time.time())}",
                query_text=retrieval_request.text,
                current_datetime=current_datetime,
                parsed_query=None,
                data=[],
                status="cannot_fulfill",
                message="Cannot fulfill your request.",
            )

        candidates = self.repository.fetch_candidates(
            date_from=plan.query.date_from,
            date_to=plan.query.date_to,
            location_text=plan.query.location_text,
        )
        query_text_embedding = self._encode_query_text(plan.query.descriptive_query)
        ranked = self._rank_candidates(
            candidates,
            plan,
            query_text_embedding=query_text_embedding,
        )[: plan.query.top_k]

        return RetrievalResponse(
            id=f"ret_{int(time.time())}",
            query_text=retrieval_request.text,
            current_datetime=current_datetime,
            parsed_query=plan.query,
            data=ranked,
            status="completed",
        )

    def _rank_candidates(
        self,
        candidates,
        plan: RetrievalPlan,
        *,
        query_text_embedding: np.ndarray | None,
    ) -> list[RetrievedImageSummary]:
        assert plan.query is not None

        scored_candidates: list[dict[str, object]] = []
        query_terms = self._merge_unique(plan.query.required_terms, plan.query.optional_terms)
        prepared_query_terms = self._prepare_query_terms(query_terms)
        prepared_excluded_terms = self._prepare_query_terms(plan.query.excluded_terms)
        descriptive_query = expand_text_with_hints(
            plan.query.descriptive_query or "",
            self.settings.semantic_hints,
        )
        for row in candidates:
            tags = self._parse_tags(row["tags_json"])
            location_tags = self._build_location_tags(
                place_name=row["place_name"],
                country=row["country"],
            )
            augmented_tags = self._merge_unique(tags, location_tags)
            description = self._build_search_description(
                description=row["description"],
                place_name=row["place_name"],
                country=row["country"],
            )
            combined_text = str(row["combined_text"] or "").strip() or build_combined_text(
                description=description,
                tags=augmented_tags,
                place_name=row["place_name"],
                country=row["country"],
                semantic_hints=self.settings.semantic_hints,
            )
            searchable_blob = " ".join(
                [
                    row["filename"] or "",
                    row["relative_path"] or "",
                    row["place_name"] or "",
                    row["country"] or "",
                    combined_text,
                    " ".join(augmented_tags),
                ]
            ).lower()
            normalized_blob = self._normalize_text(searchable_blob)
            normalized_tag_terms = self._normalize_candidate_terms(augmented_tags)
            normalized_candidate_terms = self._merge_unique(
                normalized_tag_terms,
                [token for token in normalized_blob.split() if len(token) >= 3],
            )
            if self._should_exclude_candidate(
                excluded_terms=[normalized for _, normalized in prepared_excluded_terms],
                normalized_tag_terms=normalized_tag_terms,
                normalized_candidate_terms=normalized_candidate_terms,
            ):
                continue

            score = 0.0
            matched_terms: list[str] = []
            text_embedding_similarity = self._text_embedding_similarity(
                query_text_embedding=query_text_embedding,
                document_text_embedding=self._decode_embedding(row["combined_text_embedding"]),
                row_text_embedding_model=row["text_embedding_model"],
            )
            score += TEXT_EMBEDDING_WEIGHT * text_embedding_similarity
            description_similarity = self._full_text_similarity(
                query_text=descriptive_query,
                document_text=combined_text,
            )
            score += DESCRIPTION_SIMILARITY_WEIGHT * description_similarity

            for term, normalized_term in prepared_query_terms:
                tag_similarity = self._term_similarity_normalized(
                    normalized_term=normalized_term,
                    normalized_term_candidates=normalized_tag_terms,
                )
                text_similarity = self._term_similarity_normalized(
                    normalized_term=normalized_term,
                    normalized_term_candidates=normalized_candidate_terms,
                )
                term_score = max(1.2 * tag_similarity, text_similarity)
                if term_score > 0:
                    score += TERM_SIMILARITY_WEIGHT * term_score
                    matched_terms.append(term)

            for _, normalized_term in prepared_excluded_terms:
                tag_similarity = self._term_similarity_normalized(
                    normalized_term=normalized_term,
                    normalized_term_candidates=normalized_tag_terms,
                )
                text_similarity = self._term_similarity_normalized(
                    normalized_term=normalized_term,
                    normalized_term_candidates=normalized_candidate_terms,
                )
                score -= EXCLUDED_TERM_WEIGHT * max(1.2 * tag_similarity, text_similarity)

            if plan.query.location_text:
                location_text = plan.query.location_text.lower()
                place_name = str(row["place_name"] or "").lower()
                country = str(row["country"] or "").lower()
                if location_text in place_name:
                    score += 1.2
                elif location_text in country:
                    score += 0.6
                elif location_text in searchable_blob:
                    score += 0.3

            if row["taken_at"]:
                score += 0.2

            scored_candidates.append(
                {
                    "summary": RetrievedImageSummary(
                        id=row["id"],
                        filename=row["filename"],
                        relative_path=row["relative_path"],
                        taken_at=row["taken_at"],
                        place_name=row["place_name"],
                        country=row["country"],
                        description=description,
                        tags=augmented_tags,
                        score=score,
                        matched_terms=matched_terms,
                    ),
                    "base_score": score,
                    "embedding_backend": row["embedding_backend"],
                    "raw_embedding": row["embedding"],
                    "embedding": None,
                }
            )

        scored_candidates.sort(
            key=lambda item: (
                float(item["base_score"]),
                str(item["summary"].taken_at or ""),
                str(item["summary"].filename),
            ),
            reverse=True,
        )

        if not scored_candidates:
            return []

        pool_size = min(
            len(scored_candidates),
            max(plan.query.top_k * CANDIDATE_POOL_FACTOR, MIN_CANDIDATE_POOL),
        )
        diversity_pool = scored_candidates[:pool_size]
        self._hydrate_candidate_embeddings(diversity_pool)
        reranked = self._apply_diversity_rerank(
            candidates=diversity_pool,
            target_k=plan.query.top_k,
        )
        selected_ids = {item.id for item in reranked}
        remaining = [
            item["summary"]
            for item in scored_candidates
            if item["summary"].id not in selected_ids
        ]
        return reranked + remaining

    @staticmethod
    def _parse_tags(tags_json: str) -> list[str]:
        return list(RetrievalService._parse_tags_cached(tags_json))

    @staticmethod
    @lru_cache(maxsize=4096)
    def _parse_tags_cached(tags_json: str) -> tuple[str, ...]:
        try:
            parsed = json.loads(tags_json)
        except json.JSONDecodeError:
            return ()

        if not isinstance(parsed, list):
            return ()
        return tuple(str(tag) for tag in parsed)

    @staticmethod
    def _build_search_description(
        *,
        description: str | None,
        place_name: str | None,
        country: str | None,
    ) -> str:
        base_description = str(description or "").strip()
        location_parts = [part.strip() for part in [place_name, country] if isinstance(part, str) and part.strip()]
        if not location_parts:
            return base_description

        location_sentence = f"Location: {', '.join(location_parts)}."
        if not base_description:
            return location_sentence
        return f"{base_description} {location_sentence}"

    @staticmethod
    def _build_location_tags(
        *,
        place_name: str | None,
        country: str | None,
    ) -> list[str]:
        location_tags: list[str] = []

        if isinstance(place_name, str) and place_name.strip():
            normalized_place = place_name.strip().lower()
            location_tags.append(normalized_place)
            location_tags.extend(
                token
                for token in normalized_place.replace(",", " ").split()
                if token and token not in {"the", "of"}
            )

        if isinstance(country, str) and country.strip():
            normalized_country = country.strip().lower()
            location_tags.append(normalized_country)

        return RetrievalService._merge_unique([], location_tags)

    @staticmethod
    def _merge_unique(primary: list[str], secondary: list[str]) -> list[str]:
        merged: list[str] = []
        for item in primary + secondary:
            normalized = str(item).strip()
            if normalized and normalized not in merged:
                merged.append(normalized)
        return merged

    def _apply_diversity_rerank(
        self,
        *,
        candidates: list[dict[str, object]],
        target_k: int,
    ) -> list[RetrievedImageSummary]:
        if not candidates or target_k <= 0:
            return []

        normalized_scores = self._normalize_scores(
            [float(item["base_score"]) for item in candidates]
        )
        pending = [
            {
                **item,
                "normalized_score": normalized_scores[index],
            }
            for index, item in enumerate(candidates)
        ]
        selected: list[dict[str, object]] = []
        reranked: list[RetrievedImageSummary] = []

        # Inspired by greedy k-medoids subset selection: keep relevance high,
        # but subtract similarity to the nearest already-selected DINO neighbor.
        while pending and len(reranked) < target_k:
            candidate_scores = [
                (index, self._max_similarity(candidate, selected))
                for index, candidate in enumerate(pending)
            ]
            similarity_by_index = dict(candidate_scores)
            eligible_indices = [
                index
                for index, max_similarity in candidate_scores
                if max_similarity < NEAR_DUPLICATE_SIMILARITY
            ]
            search_indices = eligible_indices or [index for index, _ in candidate_scores]

            best_index = search_indices[0]
            best_score = float("-inf")

            for index in search_indices:
                candidate = pending[index]
                diversity_penalty = similarity_by_index[index]
                mmr_score = (
                    MMR_LAMBDA * float(candidate["normalized_score"])
                    - (1.0 - MMR_LAMBDA) * diversity_penalty
                )
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_index = index

            chosen = pending.pop(best_index)
            selected.append(chosen)
            summary = chosen["summary"]
            reranked.append(
                RetrievedImageSummary(
                    id=summary.id,
                    filename=summary.filename,
                    relative_path=summary.relative_path,
                    taken_at=summary.taken_at,
                    place_name=summary.place_name,
                    country=summary.country,
                    description=summary.description,
                    tags=summary.tags,
                    score=best_score,
                    matched_terms=summary.matched_terms,
                )
            )

        return reranked

    @staticmethod
    def _max_similarity(
        candidate: dict[str, object],
        selected: list[dict[str, object]],
    ) -> float:
        if not selected:
            return 0.0

        similarities = [
            RetrievalService._embedding_similarity(candidate, other)
            for other in selected
        ]
        return max(similarities, default=0.0)

    @staticmethod
    def _embedding_similarity(
        left: dict[str, object],
        right: dict[str, object],
    ) -> float:
        left_embedding = left.get("embedding")
        right_embedding = right.get("embedding")
        if left_embedding is None or right_embedding is None:
            return 0.0

        left_backend = str(left.get("embedding_backend") or "")
        right_backend = str(right.get("embedding_backend") or "")
        if not left_backend or left_backend != right_backend:
            return 0.0

        if left_embedding.shape != right_embedding.shape:
            return 0.0

        similarity = float(np.dot(left_embedding, right_embedding))
        return max(0.0, similarity)

    @staticmethod
    def _normalize_scores(scores: list[float]) -> list[float]:
        if not scores:
            return []

        min_score = min(scores)
        max_score = max(scores)
        if max_score == min_score:
            if max_score <= 0:
                return [0.0 for _ in scores]
            return [1.0 for _ in scores]

        return [
            (score - min_score) / (max_score - min_score)
            for score in scores
        ]

    @staticmethod
    def _should_exclude_candidate(
        *,
        excluded_terms: list[str],
        normalized_tag_terms: list[str],
        normalized_candidate_terms: list[str],
    ) -> bool:
        for term in excluded_terms:
            tag_similarity = RetrievalService._term_similarity_normalized(
                normalized_term=term,
                normalized_term_candidates=normalized_tag_terms,
            )
            candidate_similarity = RetrievalService._term_similarity_normalized(
                normalized_term=term,
                normalized_term_candidates=normalized_candidate_terms,
            )
            if max(tag_similarity, candidate_similarity) >= EXCLUDED_TERM_HARD_FILTER_THRESHOLD:
                return True
        return False

    @staticmethod
    def _prepare_query_terms(terms: list[str]) -> list[tuple[str, str]]:
        prepared: list[tuple[str, str]] = []
        seen_normalized_terms: set[str] = set()
        for term in terms:
            normalized_term = RetrievalService._normalize_text(term)
            if not normalized_term or normalized_term in seen_normalized_terms:
                continue
            prepared.append((term, normalized_term))
            seen_normalized_terms.add(normalized_term)
        return prepared

    @staticmethod
    @lru_cache(maxsize=8192)
    def _normalize_text(value: str) -> str:
        lowered = re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower())
        tokens = [
            RetrievalService._singularize_token(token)
            for token in lowered.split()
            if token
        ]
        return " ".join(tokens)

    @staticmethod
    def _normalize_candidate_terms(values: list[str]) -> list[str]:
        return list(RetrievalService._normalize_candidate_terms_cached(tuple(values)))

    @staticmethod
    @lru_cache(maxsize=4096)
    def _normalize_candidate_terms_cached(values: tuple[str, ...]) -> tuple[str, ...]:
        normalized_terms: list[str] = []
        for value in values:
            normalized = RetrievalService._normalize_text(value)
            if not normalized:
                continue
            if normalized not in normalized_terms:
                normalized_terms.append(normalized)
            for token in normalized.split():
                if len(token) >= 3 and token not in normalized_terms:
                    normalized_terms.append(token)
        return tuple(normalized_terms)

    @staticmethod
    def _singularize_token(token: str) -> str:
        if len(token) <= 3:
            return token
        if token.endswith("ies") and len(token) > 4:
            return token[:-3] + "y"
        if token.endswith("sses"):
            return token[:-2]
        if token.endswith(("xes", "zes", "ches", "shes")) and len(token) > 4:
            return token[:-2]
        if token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token

    @staticmethod
    def _term_similarity(
        *,
        term: str,
        normalized_term_candidates: list[str],
    ) -> float:
        normalized_term = RetrievalService._normalize_text(term)
        return RetrievalService._term_similarity_normalized(
            normalized_term=normalized_term,
            normalized_term_candidates=normalized_term_candidates,
        )

    @staticmethod
    def _term_similarity_normalized(
        *,
        normalized_term: str,
        normalized_term_candidates: list[str],
    ) -> float:
        if not normalized_term:
            return 0.0

        best = 0.0
        for candidate in normalized_term_candidates:
            similarity = RetrievalService._pair_term_similarity(normalized_term, candidate)
            if similarity == 1.0:
                return 1.0
            best = max(best, similarity)

        if best < TERM_SIMILARITY_MIN_MATCH:
            return 0.0
        return best

    @staticmethod
    @lru_cache(maxsize=65536)
    def _pair_term_similarity(normalized_term: str, candidate: str) -> float:
        if not normalized_term or len(candidate) < 3:
            return 0.0
        if candidate == normalized_term:
            return 1.0
        if normalized_term in candidate or candidate in normalized_term:
            return TERM_SIMILARITY_SUBSTRING_MATCH
        if not RetrievalService._maybe_fuzzy_match(normalized_term, candidate):
            return 0.0

        similarity = SequenceMatcher(None, normalized_term, candidate).ratio()
        if similarity < TERM_SIMILARITY_MIN_MATCH:
            return 0.0
        return similarity

    @staticmethod
    @lru_cache(maxsize=65536)
    def _maybe_fuzzy_match(normalized_term: str, candidate: str) -> bool:
        if not normalized_term or not candidate:
            return False
        if abs(len(normalized_term) - len(candidate)) > 4:
            return False
        if normalized_term[0] == candidate[0]:
            return True
        if len(normalized_term) >= 2 and len(candidate) >= 2:
            if normalized_term[:2] == candidate[:2]:
                return True
            if normalized_term[-2:] == candidate[-2:]:
                return True
        return False

    def _encode_query_text(self, descriptive_query: str | None) -> np.ndarray | None:
        normalized_query = str(descriptive_query or "").strip()
        if not normalized_query:
            return None

        try:
            return self.text_embedding_service.encode_query(normalized_query)
        except Exception:
            return None

    def _text_embedding_similarity(
        self,
        *,
        query_text_embedding: np.ndarray | None,
        document_text_embedding: np.ndarray | None,
        row_text_embedding_model: object,
    ) -> float:
        if query_text_embedding is None or document_text_embedding is None:
            return 0.0

        model_name = str(row_text_embedding_model or "").strip()
        if model_name and model_name != self.settings.text_embedding_model_id:
            return 0.0
        if query_text_embedding.shape != document_text_embedding.shape:
            return 0.0

        return max(0.0, float(np.dot(query_text_embedding, document_text_embedding)))

    @staticmethod
    def _full_text_similarity(
        *,
        query_text: str,
        document_text: str,
    ) -> float:
        normalized_query = RetrievalService._normalize_text(query_text)
        normalized_document = RetrievalService._normalize_text(document_text)
        if not normalized_query or not normalized_document:
            return 0.0

        query_tokens = [token for token in normalized_query.split() if len(token) >= 3]
        document_tokens = [token for token in normalized_document.split() if len(token) >= 3]
        if not query_tokens or not document_tokens:
            return 0.0

        cosine_similarity = RetrievalService._token_cosine_similarity(
            query_tokens=query_tokens,
            document_tokens=document_tokens,
        )
        if cosine_similarity <= 0.0:
            return 0.0
        if normalized_query in normalized_document:
            return max(cosine_similarity, TERM_SIMILARITY_SUBSTRING_MATCH)

        phrase_similarity = SequenceMatcher(None, normalized_query, normalized_document).ratio()
        return max(cosine_similarity, 0.75 * cosine_similarity + 0.25 * phrase_similarity)

    @staticmethod
    def _token_cosine_similarity(
        *,
        query_tokens: list[str],
        document_tokens: list[str],
    ) -> float:
        query_counts = Counter(query_tokens)
        document_counts = Counter(document_tokens)
        if not query_counts or not document_counts:
            return 0.0

        dot_product = sum(
            count * document_counts.get(token, 0)
            for token, count in query_counts.items()
        )
        if dot_product == 0:
            return 0.0

        query_norm = math.sqrt(sum(count * count for count in query_counts.values()))
        document_norm = math.sqrt(sum(count * count for count in document_counts.values()))
        if query_norm == 0.0 or document_norm == 0.0:
            return 0.0

        return dot_product / (query_norm * document_norm)

    @staticmethod
    def _decode_embedding(raw_embedding: object) -> np.ndarray | None:
        if raw_embedding is None:
            return None

        if isinstance(raw_embedding, memoryview):
            raw_bytes = raw_embedding.tobytes()
        elif isinstance(raw_embedding, bytearray):
            raw_bytes = bytes(raw_embedding)
        elif isinstance(raw_embedding, bytes):
            raw_bytes = raw_embedding
        else:
            return None

        if not raw_bytes:
            return None

        return RetrievalService._decode_embedding_cached(raw_bytes)

    @staticmethod
    def _hydrate_candidate_embeddings(candidates: list[dict[str, object]]) -> None:
        for candidate in candidates:
            if candidate.get("embedding") is not None:
                continue
            candidate["embedding"] = RetrievalService._decode_embedding(candidate.get("raw_embedding"))

    @staticmethod
    @lru_cache(maxsize=4096)
    def _decode_embedding_cached(raw_bytes: bytes) -> np.ndarray | None:
        if not raw_bytes:
            return None

        embedding = np.frombuffer(raw_bytes, dtype=np.float32)
        if embedding.size == 0:
            return None

        norm = float(np.linalg.norm(embedding))
        if norm == 0.0:
            return None

        return (embedding / norm).astype(np.float32, copy=False)
