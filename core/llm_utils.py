from __future__ import annotations

import json
import os
import re
import subprocess
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from urllib.parse import urlparse

from openai import OpenAI
import requests


def strip_wrapping_fences(text: str) -> str:
    stripped = text.strip()

    fence_patterns = (
        (r"^```(?:json)?\s*", r"\s*```$"),
        (r"^'''(?:json)?\s*", r"\s*'''$"),
    )

    for prefix_pattern, suffix_pattern in fence_patterns:
        if re.match(prefix_pattern, stripped, re.IGNORECASE) and re.search(
            suffix_pattern, stripped
        ):
            stripped = re.sub(prefix_pattern, "", stripped, count=1, flags=re.IGNORECASE)
            stripped = re.sub(suffix_pattern, "", stripped, count=1)
            return stripped.strip()

    return stripped


def coerce_json_object(content) -> dict[str, object]:
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        content = "\n".join(text_parts)

    text = strip_wrapping_fences(str(content).strip())

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError("LLM returned non-JSON content.")
        parsed = json.loads(match.group(0))

    if not isinstance(parsed, dict):
        raise ValueError("LLM did not return a JSON object.")
    return parsed


def create_openai_client(*, api_key: str | None, base_url: str) -> OpenAI:
    try:
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
        )
    except TypeError as exc:
        if "unexpected keyword argument 'proxies'" not in str(exc):
            raise

        openai_version = _safe_version("openai")
        httpx_version = _safe_version("httpx")
        raise RuntimeError(
            "Incompatible OpenAI SDK stack detected "
            f"(openai={openai_version}, httpx={httpx_version}). "
            "Reinstall from `requirements.txt` or pin `httpx<0.28`."
        ) from exc


def request_minimax_chat_completion(
    *,
    api_key: str | None,
    base_url: str,
    model: str,
    messages: list[dict[str, object]],
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict[str, object] | None = None,
) -> dict[str, object]:
    if not api_key:
        raise RuntimeError("MINIMAX_KEY is not set.")

    payload: dict[str, object] = {
        "model": model,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    if max_tokens is not None:
        payload["max_completion_tokens"] = max_tokens
    if response_format:
        payload["response_format"] = response_format

    response = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"MiniMax request failed ({response.status_code}): {response.text[:500]}"
        )

    try:
        parsed = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"MiniMax returned non-JSON content: {response.text[:500]}"
        ) from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("MiniMax response is not a JSON object.")
    return parsed


def request_vertex_generate_content(
    *,
    base_url: str,
    model: str,
    messages: list[dict[str, object]],
    temperature: float | None,
    max_tokens: int | None,
    response_format: dict[str, object] | None = None,
) -> dict[str, object]:
    project = _resolve_vertex_project()
    location = _resolve_vertex_location(base_url)
    access_token = _resolve_vertex_access_token()
    endpoint = (
        f"{base_url.rstrip('/')}/projects/{project}/locations/{location}"
        f"/publishers/google/models/{model}:generateContent"
    )

    system_instruction = None
    contents: list[dict[str, object]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role", "user"))
        content = message.get("content")
        parts = _vertex_parts_from_content(content)
        if not parts:
            continue
        if role == "system" and system_instruction is None:
            system_instruction = {
                "role": "system",
                "parts": parts,
            }
            continue
        contents.append(
            {
                "role": "model" if role == "assistant" else "user",
                "parts": parts,
            }
        )

    if not contents:
        raise RuntimeError("Vertex request must contain at least one non-empty content message.")

    generation_config: dict[str, object] = {}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if max_tokens is not None:
        generation_config["maxOutputTokens"] = max_tokens
    thinking_budget = _resolve_vertex_thinking_budget(model)
    if thinking_budget is not None:
        generation_config["thinkingConfig"] = {
            "thinkingBudget": thinking_budget,
        }
    if isinstance(response_format, dict) and response_format.get("type") == "json_object":
        generation_config["responseMimeType"] = "application/json"

    payload: dict[str, object] = {
        "contents": contents,
    }
    if system_instruction is not None:
        payload["systemInstruction"] = system_instruction
    if generation_config:
        payload["generationConfig"] = generation_config

    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Vertex request failed ({response.status_code}): {response.text[:500]}"
        )

    try:
        parsed = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Vertex returned non-JSON content: {response.text[:500]}"
        ) from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("Vertex response is not a JSON object.")
    return parsed


def extract_vertex_response_text(response: dict[str, object]) -> str:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise RuntimeError("Vertex response did not contain candidates.")

    first_candidate = candidates[0]
    if not isinstance(first_candidate, dict):
        raise RuntimeError("Vertex candidate payload is invalid.")

    content = first_candidate.get("content")
    if not isinstance(content, dict):
        finish_reason = str(first_candidate.get("finishReason") or "").strip()
        if finish_reason:
            raise RuntimeError(
                f"Vertex candidate is missing content. finishReason={finish_reason}"
            )
        raise RuntimeError("Vertex candidate is missing content.")

    parts = content.get("parts")
    if not isinstance(parts, list):
        finish_reason = str(first_candidate.get("finishReason") or "").strip()
        if finish_reason:
            raise RuntimeError(
                f"Vertex content is missing parts. finishReason={finish_reason}"
            )
        raise RuntimeError("Vertex content is missing parts.")

    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])

    resolved = "\n".join(texts).strip()
    if not resolved:
        raise RuntimeError("Vertex response did not contain text content.")
    return resolved


def _vertex_parts_from_content(content: object) -> list[dict[str, object]]:
    if isinstance(content, str):
        normalized = content.strip()
        return [{"text": normalized}] if normalized else []

    parts: list[dict[str, object]] = []
    if not isinstance(content, list):
        return parts

    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "text":
            text = str(item.get("text", "")).strip()
            if text:
                parts.append({"text": text})
            continue
        if item_type == "image_url":
            image_url = item.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if not isinstance(image_url, str) or not image_url.startswith("data:"):
                continue
            match = re.match(r"^data:([^;]+);base64,(.+)$", image_url, re.DOTALL)
            if not match:
                continue
            mime_type, data = match.groups()
            parts.append(
                {
                    "inlineData": {
                        "mimeType": mime_type,
                        "data": data,
                    }
                }
            )
    return parts


def _resolve_vertex_project() -> str:
    for env_name in ("VERTEX_PROJECT", "GOOGLE_CLOUD_PROJECT", "GCP_PROJECT"):
        value = os.getenv(env_name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for command in (
        ["gcloud", "config", "get-value", "project"],
    ):
        value = _run_gcloud_text(command)
        if value:
            return value

    raise RuntimeError(
        "Vertex project is not configured. Set VERTEX_PROJECT or GOOGLE_CLOUD_PROJECT."
    )


def _resolve_vertex_location(base_url: str) -> str:
    env_value = os.getenv("VERTEX_LOCATION")
    if isinstance(env_value, str) and env_value.strip():
        return env_value.strip()

    hostname = urlparse(base_url).hostname or ""
    match = re.match(r"^([a-z0-9-]+)-aiplatform\.googleapis\.com$", hostname)
    if match:
        return match.group(1)
    return "us-central1"


def _resolve_vertex_access_token() -> str:
    for env_name in ("VERTEX_ACCESS_TOKEN", "GOOGLE_OAUTH_ACCESS_TOKEN"):
        value = os.getenv(env_name)
        if isinstance(value, str) and value.strip():
            return value.strip()

    for command in (
        ["gcloud", "auth", "application-default", "print-access-token"],
        ["gcloud", "auth", "print-access-token"],
    ):
        value = _run_gcloud_text(command)
        if value:
            return value

    raise RuntimeError(
        "Vertex access token is unavailable. Run `gcloud auth application-default login` "
        "or set VERTEX_ACCESS_TOKEN."
    )


def _resolve_vertex_thinking_budget(model: str) -> int | None:
    env_value = os.getenv("VERTEX_THINKING_BUDGET")
    if isinstance(env_value, str) and env_value.strip():
        try:
            return max(0, int(env_value.strip()))
        except ValueError:
            return None

    normalized_model = model.strip().lower()
    if normalized_model.startswith("gemini-2.5"):
        # Retrieval/indexing expects short structured output. Disabling the
        # reasoning budget avoids spending the response budget on hidden thoughts.
        return 0
    return None


def _run_gcloud_text(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None

    if completed.returncode != 0:
        return None

    value = completed.stdout.strip()
    return value or None


def _safe_version(package_name: str) -> str:
    try:
        module = import_module(package_name)
        module_version = getattr(module, "__version__", None)
        if isinstance(module_version, str) and module_version.strip():
            return module_version
    except Exception:
        pass

    try:
        return version(package_name)
    except PackageNotFoundError:
        return "unknown"
