from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


SETTINGS_FILE_NAME = "backend-settings.json"


@dataclass
class PersistedAppSettings:
    image_library_dir: str | None = None
    db_path: str | None = None
    process_image_width: int | None = None
    vision_profile_name: str | None = None
    query_profile_name: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None
        }


def resolve_app_state_dir(project_root: Path) -> Path:
    configured = os.getenv("MEMOLENS_APP_STATE_DIR")
    if isinstance(configured, str) and configured.strip():
        return Path(configured).expanduser().resolve()

    if os.name == "posix" and "darwin" in os.sys.platform:
        return (Path.home() / "Library/Application Support" / "MemoLens").resolve()

    if os.name == "nt":
        appdata = os.getenv("APPDATA")
        if isinstance(appdata, str) and appdata.strip():
            return (Path(appdata).expanduser() / "MemoLens").resolve()

    xdg_state = os.getenv("XDG_STATE_HOME")
    if isinstance(xdg_state, str) and xdg_state.strip():
        return (Path(xdg_state).expanduser() / "MemoLens").resolve()

    return (project_root / ".memolens-state").resolve()


def settings_file_path(app_state_dir: Path) -> Path:
    return app_state_dir / SETTINGS_FILE_NAME


def load_persisted_app_settings(app_state_dir: Path) -> PersistedAppSettings:
    path = settings_file_path(app_state_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PersistedAppSettings()
    except Exception:
        return PersistedAppSettings()

    if not isinstance(raw, dict):
        return PersistedAppSettings()
    return _normalize_persisted_settings(raw)


def save_persisted_app_settings(
    app_state_dir: Path,
    payload: dict[str, Any],
) -> PersistedAppSettings:
    current = load_persisted_app_settings(app_state_dir)
    merged = current.to_dict()
    merged.update(payload)
    normalized = _normalize_persisted_settings(merged)

    app_state_dir.mkdir(parents=True, exist_ok=True)
    path = settings_file_path(app_state_dir)
    path.write_text(
        f"{json.dumps(normalized.to_dict(), indent=2, ensure_ascii=True)}\n",
        encoding="utf-8",
    )
    return normalized


def _normalize_optional_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return str(Path(value).expanduser().resolve())


def _normalize_optional_profile(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _normalize_persisted_settings(payload: dict[str, Any]) -> PersistedAppSettings:
    process_image_width = payload.get("process_image_width")
    normalized_process_width: int | None = None
    if process_image_width is not None:
        if not isinstance(process_image_width, int) or process_image_width <= 0:
            raise ValueError("`process_image_width` must be a positive integer when set.")
        normalized_process_width = process_image_width

    return PersistedAppSettings(
        image_library_dir=_normalize_optional_path(payload.get("image_library_dir")),
        db_path=_normalize_optional_path(payload.get("db_path")),
        process_image_width=normalized_process_width,
        vision_profile_name=_normalize_optional_profile(payload.get("vision_profile_name")),
        query_profile_name=_normalize_optional_profile(payload.get("query_profile_name")),
    )
