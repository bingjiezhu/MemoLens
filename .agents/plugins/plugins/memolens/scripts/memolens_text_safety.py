"""Shared fail-closed detection for private locator-shaped display text."""

from __future__ import annotations

import re


_PRIVATE_LOCATOR = re.compile(
    r"(?:"
    # Require a scheme boundary so ordinary words such as ``metadata:`` and
    # ``profile:`` are not mistaken for data/file URIs.
    r"(?<![A-Za-z0-9+.-])(?:file|data):"
    r"|[A-Za-z]:[\\/]"
    r"|\\\\"
    r"|~[\\/]"
    # OS roots stay path-shaped even when hostile text glues words or CJK text
    # directly in front of them.
    r"|/(?:Users|home|var|tmp|Volumes|Library|etc|opt|srv|mnt)(?=/|$)"
    # For a generic POSIX absolute path, the preceding character must not be a
    # Unicode letter/number. Underscore and symbols are boundaries. Requiring a
    # non-space first component preserves prose such as ``A / B`` while keeping
    # taxonomies and dates (a/b/c, 2026/08/22) intact.
    r"|(?<![^\W_])/(?![/\s])"
    r")",
    re.IGNORECASE,
)


def contains_private_locator(value: str) -> bool:
    """Return whether display text contains a path or inline-data locator."""

    return bool(_PRIVATE_LOCATOR.search(value))


__all__ = ["contains_private_locator"]
