"""Markdown metadata section rendering for saved smoke output."""

from __future__ import annotations

from typing import Any


def format_metadata_value(value: Any) -> str | None:
    """Render one metadata value for a Markdown bullet, or `None` to skip it.

    Metadata dicts are full of fields that are frequently absent (due date,
    resolution, breadcrumb, ...); skipping empty ones keeps the rendered
    section a dense summary instead of a wall of blank/`None` bullets.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value if item not in (None, "")]
        return ", ".join(items) if items else None
    text = str(value).strip()
    return text or None


def render_metadata_section(metadata: dict[str, Any], fields: list[tuple[str, str]]) -> list[str]:
    """Render a `## Metadata` section from a curated `(label, key)` field list."""
    lines = [
        f"- **{label}**: {rendered}"
        for label, key in fields
        if (rendered := format_metadata_value(metadata.get(key))) is not None
    ]
    if not lines:
        return []
    return ["## Metadata", "", *lines, ""]
