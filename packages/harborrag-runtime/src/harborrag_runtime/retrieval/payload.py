"""Read optional provenance out of a vector payload.

Separate from ``validation`` because these never fail a turn: a chunk
written before a field was surfaced projects it empty rather than making
the whole result unusable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def optional_text(payload: Mapping[str, Any], key: str) -> str:
    """A payload string, or empty when absent -- never a missing key."""

    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def section_path(payload: Mapping[str, Any]) -> list[str]:
    """The heading trail for the chunk, as a list of non-blank strings.

    Confluence writes this as a real list, but older rows stored a repr of
    one; both degrade to an empty trail rather than failing the turn.
    """

    value = payload.get("section_path")
    if isinstance(value, str):
        value = [value] if value.strip().startswith("[") is False else []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


__all__ = ["optional_text", "section_path"]
