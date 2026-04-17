from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass
from pathlib import Path

from openai import OpenAI
from PIL import Image

from core.config import Settings
from core.llm_utils import (
    coerce_json_object,
    create_openai_client,
    extract_vertex_response_text,
    request_minimax_chat_completion,
    request_vertex_generate_content,
)
from core.schemas import GeneratedCopy, RetrievedImageSummary


COPYWRITER_PROMPT = """You write polished copy based on a user's original request
and a small set of retrieved reference photos.

Return strict JSON with this shape:
{
  "title": "short title or null",
  "body": "2-4 sentence copy grounded in the images",
  "highlights": ["short phrase", "short phrase"]
}

Rules:
- Use the user's original request as the intent anchor.
- Ground the copy in what is consistently visible across the retrieved photos.
- Do not invent specific facts that are not visually supported.
- If the images vary, emphasize the common scene or subject instead of hallucinating details.
- Match the language of the user's original request when reasonable.
- Keep the tone polished but factual, not overly salesy.
- Do not mention that these are retrieved images.
- Do not include markdown.
"""


@dataclass
class PreparedCopyImage:
    source_path: Path
    mime_type: str
    content_bytes: bytes


class RetrievalCopywriter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: OpenAI | None = None
        self._text_client: OpenAI | None = None

    def generate(
        self,
        *,
        query_text: str,
        retrieved_images: list[RetrievedImageSummary],
        image_library_dir: Path,
        image_limit: int = 6,
    ) -> GeneratedCopy:
        if self.settings.vision_provider == "vertex" or self.settings.vision_api_key:
            prepared_images = self._load_images(
                retrieved_images=retrieved_images,
                image_library_dir=image_library_dir,
                image_limit=image_limit,
            )
            if prepared_images:
                try:
                    model = self.settings.vision_model
                    if self.settings.vision_provider == "minimax":
                        return self._generate_with_minimax(
                            query_text=query_text,
                            prepared_images=prepared_images,
                            model=model,
                        )
                    if self.settings.vision_provider == "vertex":
                        return self._generate_with_vertex(
                            query_text=query_text,
                            prepared_images=prepared_images,
                            model=model,
                        )
                    return self._generate_with_openai(
                        query_text=query_text,
                        prepared_images=prepared_images,
                        model=model,
                    )
                except Exception:
                    pass

        if self.settings.query_provider == "vertex" or self.settings.query_api_key:
            try:
                return self._generate_from_text_context(
                    query_text=query_text,
                    retrieved_images=retrieved_images,
                )
            except Exception:
                pass

        return self._fallback_generated_copy(
            query_text=query_text,
            retrieved_images=retrieved_images,
        )

    def _generate_with_openai(
        self,
        *,
        query_text: str,
        prepared_images: list[PreparedCopyImage],
        model: str,
    ) -> GeneratedCopy:
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "Original user request:\n"
                    f"{query_text}\n\n"
                    "Write a short piece of image-grounded copy from these photos."
                ),
            }
        ]
        for image in prepared_images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": self._to_data_url(
                            content_bytes=image.content_bytes,
                            mime_type=image.mime_type,
                        ),
                        "detail": "low",
                    },
                }
            )

        response = self._get_client().chat.completions.create(
            model=model,
            temperature=self.settings.vision_temperature,
            response_format=self.settings.vision_response_format,
            max_tokens=self.settings.vision_max_tokens,
            messages=[
                {
                    "role": "system",
                    "content": [{"type": "text", "text": COPYWRITER_PROMPT}],
                },
                {
                    "role": "user",
                    "content": content,
                },
            ],
        )
        parsed = coerce_json_object(response.choices[0].message.content)
        return self._coerce_generated_copy(
            parsed=parsed,
            model=model,
            image_count=len(prepared_images),
        )

    def _generate_with_minimax(
        self,
        *,
        query_text: str,
        prepared_images: list[PreparedCopyImage],
        model: str,
    ) -> GeneratedCopy:
        image_blocks = []
        for image in prepared_images:
            encoded = base64.b64encode(image.content_bytes).decode("utf-8")
            image_blocks.append(f"[Image base64:{encoded}]")

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
                    "content": COPYWRITER_PROMPT,
                },
                {
                    "role": "user",
                    "content": (
                        "Original user request:\n"
                        f"{query_text}\n\n"
                        "Write a short piece of image-grounded copy from these photos.\n"
                        + "\n".join(image_blocks)
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
        return self._coerce_generated_copy(
            parsed=parsed,
            model=model,
            image_count=len(prepared_images),
        )

    def _generate_with_vertex(
        self,
        *,
        query_text: str,
        prepared_images: list[PreparedCopyImage],
        model: str,
    ) -> GeneratedCopy:
        content: list[dict[str, object]] = [
            {
                "type": "text",
                "text": (
                    "Original user request:\n"
                    f"{query_text}\n\n"
                    "Write a short piece of image-grounded copy from these photos."
                ),
            }
        ]
        for image in prepared_images:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": self._to_data_url(
                            content_bytes=image.content_bytes,
                            mime_type=image.mime_type,
                        ),
                        "detail": "low",
                    },
                }
            )

        response = request_vertex_generate_content(
            base_url=self.settings.vision_base_url,
            model=model,
            temperature=self.settings.vision_temperature,
            max_tokens=self.settings.vision_max_tokens,
            response_format=self.settings.vision_response_format,
            messages=[
                {
                    "role": "system",
                    "content": [{"type": "text", "text": COPYWRITER_PROMPT}],
                },
                {
                    "role": "user",
                    "content": content,
                },
            ],
        )
        parsed = coerce_json_object(extract_vertex_response_text(response))
        return self._coerce_generated_copy(
            parsed=parsed,
            model=model,
            image_count=len(prepared_images),
        )

    def _coerce_generated_copy(
        self,
        *,
        parsed: dict[str, object],
        model: str,
        image_count: int,
    ) -> GeneratedCopy:
        title = self._normalize_optional_text(parsed.get("title"))
        body = self._normalize_optional_text(parsed.get("body"))
        if body is None:
            raise ValueError("Copywriter did not return a `body` field.")

        raw_highlights = parsed.get("highlights")
        highlights: list[str] = []
        if isinstance(raw_highlights, list):
            for item in raw_highlights:
                normalized = re.sub(r"\s+", " ", str(item).strip())
                if normalized and normalized not in highlights:
                    highlights.append(normalized)

        return GeneratedCopy(
            model=model,
            title=title,
            body=body,
            highlights=highlights[:8],
            image_count=image_count,
        )

    def _get_client(self) -> OpenAI:
        if self._client is None:
            self._client = create_openai_client(
                api_key=self.settings.vision_api_key,
                base_url=self.settings.vision_base_url,
            )
        return self._client

    def _get_text_client(self) -> OpenAI:
        if self._text_client is None:
            self._text_client = create_openai_client(
                api_key=self.settings.query_api_key,
                base_url=self.settings.query_base_url,
            )
        return self._text_client

    def _generate_from_text_context(
        self,
        *,
        query_text: str,
        retrieved_images: list[RetrievedImageSummary],
    ) -> GeneratedCopy:
        lines = []
        for index, item in enumerate(retrieved_images[:9], start=1):
            location = ", ".join(
                [
                    value.strip()
                    for value in [item.place_name, item.country]
                    if isinstance(value, str) and value.strip()
                ]
            ) or "unknown location"
            tags = ", ".join(item.tags[:8]) if item.tags else "no tags"
            lines.append(
                f"{index}. filename={item.filename}; location={location}; "
                f"taken_at={item.taken_at or 'unknown'}; tags={tags}; description={item.description}"
            )

        messages = [
            {
                "role": "system",
                "content": COPYWRITER_PROMPT,
            },
            {
                "role": "user",
                "content": (
                    "Original user request:\n"
                    f"{query_text}\n\n"
                    "Retrieved image summaries:\n"
                    + "\n".join(lines)
                ),
            },
        ]

        if self.settings.query_provider == "minimax":
            response = request_minimax_chat_completion(
                api_key=self.settings.query_api_key,
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=max(0.1, min(1.0, self.settings.query_temperature)),
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=messages,
            )
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices:
                raise RuntimeError("MiniMax response did not contain choices.")
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            parsed = coerce_json_object(content)
        elif self.settings.query_provider == "vertex":
            response = request_vertex_generate_content(
                base_url=self.settings.query_base_url,
                model=self.settings.query_model,
                temperature=self.settings.query_temperature,
                max_tokens=self.settings.query_max_tokens,
                response_format=self.settings.query_response_format,
                messages=messages,
            )
            parsed = coerce_json_object(extract_vertex_response_text(response))
        else:
            response = self._get_text_client().chat.completions.create(
                model=self.settings.query_model,
                temperature=self.settings.query_temperature,
                response_format=self.settings.query_response_format,
                max_tokens=self.settings.query_max_tokens,
                messages=messages,
            )
            parsed = coerce_json_object(response.choices[0].message.content)

        return self._coerce_generated_copy(
            parsed=parsed,
            model=self.settings.query_model,
            image_count=min(len(retrieved_images), 9),
        )

    def _load_images(
        self,
        *,
        retrieved_images: list[RetrievedImageSummary],
        image_library_dir: Path,
        image_limit: int,
    ) -> list[PreparedCopyImage]:
        library_root = image_library_dir.resolve()
        prepared_images: list[PreparedCopyImage] = []
        for item in retrieved_images[: max(image_limit, 0)]:
            source_path = (library_root / item.relative_path).resolve()
            try:
                source_path.relative_to(library_root)
            except ValueError:
                continue
            if not source_path.exists() or not source_path.is_file():
                continue
            try:
                prepared_images.append(self._prepare_image(source_path))
            except Exception:
                continue
        return prepared_images

    def _prepare_image(self, image_path: Path) -> PreparedCopyImage:
        with Image.open(image_path) as handle:
            image = handle.convert("RGB")

            target_width = max(int(self.settings.process_image_width), 1)
            if image.width > target_width:
                target_height = max(round(image.height * target_width / image.width), 1)
                image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)

            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=90)

        return PreparedCopyImage(
            source_path=image_path,
            mime_type="image/jpeg",
            content_bytes=buffer.getvalue(),
        )

    @staticmethod
    def _normalize_optional_text(value: object) -> str | None:
        if value is None:
            return None
        normalized = re.sub(r"\s+", " ", str(value).strip())
        if not normalized or normalized.lower() in {"null", "none"}:
            return None
        return normalized

    def _fallback_generated_copy(
        self,
        *,
        query_text: str,
        retrieved_images: list[RetrievedImageSummary],
    ) -> GeneratedCopy:
        tags: list[str] = []
        locations: list[str] = []
        for image in retrieved_images:
            for tag in image.tags:
                normalized_tag = re.sub(r"\s+", " ", str(tag).strip().lower())
                if normalized_tag and normalized_tag not in tags:
                    tags.append(normalized_tag)
            for location in [image.place_name, image.country]:
                normalized_location = self._normalize_optional_text(location)
                if normalized_location and normalized_location not in locations:
                    locations.append(normalized_location)

        lead_tag = tags[0].replace("_", " ").title() if tags else "Local Moments"
        body = (
            f'This set stays close to the request "{query_text}" and keeps the result centered '
            f"on {lead_tag.lower()} details from your library."
        )
        highlights = [
            f"Grounded in {lead_tag.lower()} details",
            f"Primary location: {locations[0]}" if locations else "Local library result",
            "Generated from indexed metadata",
        ]
        return GeneratedCopy(
            model="local_fallback",
            title=lead_tag,
            body=body,
            highlights=highlights,
            image_count=min(len(retrieved_images), 9),
        )

    @staticmethod
    def _to_data_url(content_bytes: bytes, mime_type: str) -> str:
        encoded = base64.b64encode(content_bytes).decode("utf-8")
        return f"data:{mime_type};base64,{encoded}"
