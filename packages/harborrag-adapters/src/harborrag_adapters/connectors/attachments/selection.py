from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def attachment_ids_from_filters(
    filters: Mapping[str, Any],
) -> tuple[str, ...]:
    """Normalize an optional exact attachment selection from a source query."""

    value = filters.get("attachment_ids")
    if value is None:
        return ()
    values = (value,) if isinstance(value, str) else value
    if not isinstance(values, Sequence):
        raise ValueError("attachment_ids must be a string or sequence")
    normalized = tuple(dict.fromkeys(str(item).strip() for item in values))
    if not normalized or any(not item for item in normalized):
        raise ValueError("attachment_ids must contain non-empty values")
    return normalized


def select_attachment_payloads(
    attachments: Sequence[Mapping[str, Any]],
    selected_ids: Sequence[str],
) -> list[Mapping[str, Any]]:
    """Return source attachment descriptors matching an explicit selection."""

    if not selected_ids:
        return list(attachments)
    allowed = set(selected_ids)
    return [
        attachment
        for attachment in attachments
        if str(attachment.get("id") or "").strip() in allowed
    ]
