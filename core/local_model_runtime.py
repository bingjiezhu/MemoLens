from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache

import requests

from .config import VLMProfileCatalogEntry


@dataclass(frozen=True)
class LocalMachineInfo:
    platform: str
    architecture: str
    model_name: str | None
    chip: str | None
    memory_gb: int | None

    def to_dict(self) -> dict[str, object]:
        return {
            "platform": self.platform,
            "architecture": self.architecture,
            "model_name": self.model_name,
            "chip": self.chip,
            "memory_gb": self.memory_gb,
        }


@dataclass(frozen=True)
class LocalModelRuntimeSummary:
    machine: LocalMachineInfo
    ollama_installed: bool
    ollama_binary: str | None
    ollama_reachable: bool
    recommended_query_profile_name: str | None
    recommended_vision_profile_name: str | None
    summary: str
    recommendation_basis: str
    commands: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "machine": self.machine.to_dict(),
            "ollama_installed": self.ollama_installed,
            "ollama_binary": self.ollama_binary,
            "ollama_reachable": self.ollama_reachable,
            "recommended_query_profile_name": self.recommended_query_profile_name,
            "recommended_vision_profile_name": self.recommended_vision_profile_name,
            "summary": self.summary,
            "recommendation_basis": self.recommendation_basis,
            "commands": list(self.commands),
        }


def detect_local_model_runtime(
    profile_catalog: tuple[VLMProfileCatalogEntry, ...],
) -> LocalModelRuntimeSummary:
    machine = _detect_local_machine_info()
    ollama_binary = shutil.which("ollama")
    ollama_installed = bool(ollama_binary)
    ollama_reachable = _is_ollama_reachable()

    recommended_query, recommended_vision = _recommend_gemma_profiles(
        memory_gb=machine.memory_gb,
        profile_catalog=profile_catalog,
    )
    label_by_name = {entry.name: entry.label for entry in profile_catalog}
    commands = _build_command_hints(
        query_profile_name=recommended_query,
        vision_profile_name=recommended_vision,
        profile_catalog=profile_catalog,
        ollama_reachable=ollama_reachable,
    )

    machine_label_parts = [
        part
        for part in [machine.model_name, machine.chip, f"{machine.memory_gb} GB" if machine.memory_gb else None]
        if isinstance(part, str) and part.strip()
    ]
    machine_label = " · ".join(machine_label_parts) or f"{machine.platform} {machine.architecture}"

    if recommended_query or recommended_vision:
        summary = (
            f"{machine_label} can run Gemma 4 locally. "
            f"Recommended query/copy profile: {label_by_name.get(recommended_query or '', recommended_query or 'keep API')}. "
            f"Recommended vision profile: {label_by_name.get(recommended_vision or '', recommended_vision or 'keep API')}."
        )
    else:
        summary = (
            f"{machine_label} should keep API models as the primary path for now. "
            "No suitable local Gemma 4 profile was found in the current config."
        )

    recommendation_basis = (
        "Recommendation is inferred from total system memory and the presence of configured local "
        "Gemma 4 profiles. Real throughput still depends on quantization, context length, and how "
        "much memory other apps are using."
    )

    return LocalModelRuntimeSummary(
        machine=machine,
        ollama_installed=ollama_installed,
        ollama_binary=ollama_binary,
        ollama_reachable=ollama_reachable,
        recommended_query_profile_name=recommended_query,
        recommended_vision_profile_name=recommended_vision,
        summary=summary,
        recommendation_basis=recommendation_basis,
        commands=commands,
    )


@lru_cache(maxsize=1)
def _detect_local_machine_info() -> LocalMachineInfo:
    detected_platform = platform.system().lower()
    detected_architecture = platform.machine().lower()
    if detected_platform == "darwin":
        detected_architecture = (
            _run_command(["arch", "-arm64", "uname", "-m"], timeout=2)
            or _run_command(["uname", "-m"], timeout=2)
            or detected_architecture
        ).lower()
    model_name = None
    chip = None
    memory_gb = _detect_memory_gb()

    if detected_platform == "darwin":
        hardware_text = _run_command(
            ["system_profiler", "SPHardwareDataType"],
            timeout=6,
        )
        if hardware_text:
            model_name = _extract_hardware_value(hardware_text, "Model Name")
            chip = _extract_hardware_value(hardware_text, "Chip")
            parsed_memory = _extract_memory_gb(hardware_text)
            if parsed_memory is not None:
                memory_gb = parsed_memory

    return LocalMachineInfo(
        platform=detected_platform,
        architecture=detected_architecture,
        model_name=model_name,
        chip=chip,
        memory_gb=memory_gb,
    )


def _detect_memory_gb() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        total_bytes = int(page_size) * int(page_count)
        if total_bytes > 0:
            return max(round(total_bytes / (1024 ** 3)), 1)
    except (AttributeError, OSError, ValueError):
        return None
    return None


def _extract_hardware_value(hardware_text: str, key: str) -> str | None:
    pattern = re.compile(rf"^\s*{re.escape(key)}:\s*(.+)$", re.MULTILINE)
    match = pattern.search(hardware_text)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.lower() == "unknown":
        return None
    return value


def _extract_memory_gb(hardware_text: str) -> int | None:
    match = re.search(r"^\s*Memory:\s*([0-9]+)\s*GB\s*$", hardware_text, re.MULTILINE)
    if not match:
        return None
    return int(match.group(1))


def _run_command(command: list[str], *, timeout: int) -> str | None:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, PermissionError, subprocess.SubprocessError):
        return None

    stdout = completed.stdout.strip()
    return stdout or None


def _is_ollama_reachable() -> bool:
    try:
        response = requests.get("http://127.0.0.1:11434/api/tags", timeout=0.5)
    except requests.RequestException:
        return False
    return response.ok


def _recommend_gemma_profiles(
    *,
    memory_gb: int | None,
    profile_catalog: tuple[VLMProfileCatalogEntry, ...],
) -> tuple[str | None, str | None]:
    available_names = {
        entry.name
        for entry in profile_catalog
        if entry.execution == "local" and ((entry.family or "").lower() == "gemma4" or "gemma4" in entry.model.lower())
    }

    def pick(*candidates: str) -> str | None:
        for candidate in candidates:
            if candidate in available_names:
                return candidate
        return None

    if memory_gb is None:
        return pick("ollama_gemma4_e4b", "ollama_gemma4_e2b"), pick(
            "ollama_gemma4_e4b",
            "ollama_gemma4_e2b",
        )

    if memory_gb >= 56:
        return pick("ollama_gemma4_31b", "ollama_gemma4_26b", "ollama_gemma4_e4b"), pick(
            "ollama_gemma4_26b",
            "ollama_gemma4_e4b",
            "ollama_gemma4_31b",
        )
    if memory_gb >= 40:
        return pick("ollama_gemma4_26b", "ollama_gemma4_31b", "ollama_gemma4_e4b"), pick(
            "ollama_gemma4_26b",
            "ollama_gemma4_e4b",
        )
    if memory_gb >= 24:
        return pick("ollama_gemma4_26b", "ollama_gemma4_e4b"), pick(
            "ollama_gemma4_e4b",
            "ollama_gemma4_e2b",
        )
    if memory_gb >= 16:
        return pick("ollama_gemma4_e4b", "ollama_gemma4_e2b"), pick(
            "ollama_gemma4_e4b",
            "ollama_gemma4_e2b",
        )
    if memory_gb >= 8:
        return pick("ollama_gemma4_e2b"), pick("ollama_gemma4_e2b")
    return None, None


def _build_command_hints(
    *,
    query_profile_name: str | None,
    vision_profile_name: str | None,
    profile_catalog: tuple[VLMProfileCatalogEntry, ...],
    ollama_reachable: bool,
) -> tuple[str, ...]:
    model_by_name = {entry.name: entry.model for entry in profile_catalog}
    models: list[str] = []
    for profile_name in [query_profile_name, vision_profile_name]:
        model = model_by_name.get(profile_name or "")
        if model and model not in models:
            models.append(model)

    commands: list[str] = []
    if not ollama_reachable:
        commands.append("ollama serve")
    for model in models:
        commands.append(f"ollama pull {model}")
    return tuple(commands)
