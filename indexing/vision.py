from __future__ import annotations

import base64
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
from core.schemas import VisionMetadata
from .files import PreparedImage


VISION_PROMPT = """Analyze this photo for a local image retrieval system.
Return strict JSON with this shape:
{
  "tags": ["english tag", "中文标签", ...],
  "scene": "2-4 word scene summary",
  "subjects": ["person", "cat", ...] or [],
  "count": "e.g. two people, one dog" or "none",
  "mood": "calm/energetic/romantic/dramatic/cozy/..." or null,
  "time_of_day": "morning/afternoon/sunset/night/..." or null,
  "setting": "indoor/outdoor/urban/nature/..." or null,
  "description": "1 concise factual sentence",
  "location_hint": "best guess of place/region or null"
}

Rules:
- tags: 15-20 tags, each tag in BOTH English and Chinese. Include objects, actions, colors, textures, weather, clothing, composition.
- Also include location-related tags: terrain type (beach/海边, mountain/山, river/河), environment (rural/乡村, urban/城市, suburban/郊区), region style cues (e.g. Chinese village/中国乡村, European street/欧洲街道).
- If no people are visible, include tags "no people" and "无人".
- If people are visible, include tags like "person", "人物", "portrait"/"人像" as appropriate.
- scene: ultra-short English summary for semantic matching.
- subjects: list every distinct subject type visible.
- count: describe quantity of main subjects.
- description: 1 factual sentence, no uncertainty language.
- location_hint: infer the most likely place from ALL available cues: landmarks, signs, text, architecture style, vegetation, terrain, road markings, license plates, cultural indicators. Use the most specific name you can justify. Set null only if there are truly no location cues at all.
- Do not include markdown.
"""


class OpenAICompatibleVisionClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: OpenAI | None = None

    def describe_image(
        self,
        prepared_image: PreparedImage,
        model: str,
    ) -> VisionMetadata:
        if self.settings.vision_provider != "vertex" and not self.settings.vision_api_key:
            return self._fallback_metadata(prepared_image.source_name)
        try:
            if self.settings.vision_provider == "minimax":
                return self._describe_image_with_minimax(prepared_image, model)
            if self.settings.vision_provider == "vertex":
                return self._describe_image_with_vertex(prepared_image, model)

            payload = {
                "model": model,
                "temperature": self.settings.vision_temperature,
                "response_format": self.settings.vision_response_format,
                "messages": [
                    {
                        "role": "system",
                        "content": [{"type": "text", "text": VISION_PROMPT}],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Generate tags and a short description for this image.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": self._to_data_url(
                                        content_bytes=prepared_image.content_bytes,
                                        mime_type=prepared_image.mime_type,
                                    ),
                                    "detail": "low",
                                },
                            },
                        ],
                    },
                ],
            }

            response = self._get_client().chat.completions.create(
                model=payload["model"],
                temperature=payload["temperature"],
                response_format=payload["response_format"],
                messages=payload["messages"],
                max_tokens=self.settings.vision_max_tokens,
            )

            content = response.choices[0].message.content
            parsed = coerce_json_object(content)
            return self._coerce_metadata_from_parsed(parsed, prepared_image.source_name)
        except Exception:
            return self._fallback_metadata(prepared_image.source_name)

    def _describe_image_with_minimax(
        self,
        prepared_image: PreparedImage,
        model: str,
    ) -> VisionMetadata:
        encoded_image = base64.b64encode(prepared_image.content_bytes).decode("utf-8")
        response = request_minimax_chat_completion(
            api_key=self.settings.vision_api_key,
            base_url=self.settings.vision_base_url,
            model=model,
            temperature=self.settings.vision_temperature,
            max_tokens=self.settings.vision_max_tokens,
            response_format=self.settings.vision_response_format,
            messages=[
                {
                    "role": "system",
                    "content": VISION_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        "Generate tags, a short factual description, and a conservative "
                        "location hint for this image. Return strict JSON only.\n"
                        f"[Image base64:{encoded_image}]"
                    ),
                },
            ],
        )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("MiniMax response did not contain choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        parsed = coerce_json_object(content)
        return self._coerce_metadata_from_parsed(parsed, prepared_image.source_name)

    def _describe_image_with_vertex(
        self,
        prepared_image: PreparedImage,
        model: str,
    ) -> VisionMetadata:
        response = request_vertex_generate_content(
            base_url=self.settings.vision_base_url,
            model=model,
            temperature=self.settings.vision_temperature,
            max_tokens=self.settings.vision_max_tokens,
            response_format=self.settings.vision_response_format,
            messages=[
                {
                    "role": "system",
                    "content": [{"type": "text", "text": VISION_PROMPT}],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Generate tags and a short description for this image.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": self._to_data_url(
                                    content_bytes=prepared_image.content_bytes,
                                    mime_type=prepared_image.mime_type,
                                ),
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
        )
        parsed = coerce_json_object(extract_vertex_response_text(response))
        return self._coerce_metadata_from_parsed(parsed, prepared_image.source_name)

    def _coerce_metadata_from_parsed(
        self,
        parsed: dict[str, object],
        source_name: str,
    ) -> VisionMetadata:
        fallback = self._fallback_metadata(source_name)

        tags = parsed.get("tags", [])
        description = str(parsed.get("description", "")).strip()
        location_hint = parsed.get("location_hint")

        if not isinstance(tags, list):
            tags = []

        cleaned_tags = []
        for tag in tags:
            normalized = re.sub(r"\s+", " ", str(tag).strip().lower())
            if normalized and normalized not in cleaned_tags:
                cleaned_tags.append(normalized)

        # Merge structured fields into tags for richer retrieval
        for field_name in ("scene", "mood", "time_of_day", "setting", "count"):
            value = parsed.get(field_name)
            if value is not None:
                normalized_value = re.sub(r"\s+", " ", str(value).strip().lower())
                if normalized_value and normalized_value not in {"null", "none", "unknown", ""}:
                    if normalized_value not in cleaned_tags:
                        cleaned_tags.append(normalized_value)

        # Merge subjects into tags
        subjects = parsed.get("subjects", [])
        if isinstance(subjects, list):
            for subject in subjects:
                normalized_subject = re.sub(r"\s+", " ", str(subject).strip().lower())
                if normalized_subject and normalized_subject not in cleaned_tags:
                    cleaned_tags.append(normalized_subject)

        if not description:
            description = fallback.description

        # Enrich description with scene summary if available
        scene = parsed.get("scene")
        if scene and description:
            scene_text = str(scene).strip()
            if scene_text and scene_text.lower() not in {"null", "none"}:
                description = f"{description} Scene: {scene_text}."

        normalized_location_hint = None
        if location_hint is not None:
            candidate_hint = re.sub(r"\s+", " ", str(location_hint).strip())
            if candidate_hint and candidate_hint.lower() not in {"null", "none", "unknown"}:
                normalized_location_hint = candidate_hint

        return VisionMetadata(
            tags=cleaned_tags[:25] or fallback.tags,
            description=description,
            location_hint=normalized_location_hint,
        )

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = create_openai_client(
                api_key=self.settings.vision_api_key,
                base_url=self.settings.vision_base_url,
            )
        return self._client

    @staticmethod
    def _to_data_url(content_bytes: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(content_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _fallback_metadata(name: str) -> VisionMetadata:
        stem_tokens = re.split(r"[_\-\s]+", name.lower())
        tags = [token for token in stem_tokens if token][:6]
        return VisionMetadata(
            tags=tags or ["untagged"],
            description=f"Local image file named {name}.",
            location_hint=None,
        )
