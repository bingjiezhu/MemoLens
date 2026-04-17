from __future__ import annotations

import os
from pathlib import Path


def load_env_files(*paths: Path) -> None:
    for path in paths:
        _load_env_file(path)


def _load_env_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, raw_value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue

        if normalized_key in os.environ:
            continue

        os.environ[normalized_key] = _strip_wrapping_quotes(raw_value.strip())


def _strip_wrapping_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"', "`"}:
        return value[1:-1]
    return value
