"""Compatibility import for the backend-owned query planner.

The active implementation moved to :mod:`backend.src.retrieval.planner`.
Keep this module temporarily so existing third-party imports do not break.
"""

from backend.src.retrieval.planner import OpenAICompatibleQueryPlanner

__all__ = ["OpenAICompatibleQueryPlanner"]
