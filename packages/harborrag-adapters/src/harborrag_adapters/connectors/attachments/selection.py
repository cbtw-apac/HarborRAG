"""Exact attachment selection from a source query's ``attachment_ids`` filter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def attachment_ids_from_filters(
    filters: Mapping[str, Any],
) -> tuple[str, ...]:
    """Read the optional exact attachment selection from a query's filters.

    Args:
        filters: Query filters; ``attachment_ids`` is a string or a sequence.

    Returns:
        De-duplicated, stripped ids, empty when no selection is set.

    Raises:
        ValueError: If the value is not a string or sequence, or holds an empty id.
    """

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
    """Return the attachment payloads whose ``id`` is selected.

    Args:
        attachments: Provider attachment payloads.
        selected_ids: Selected ids; empty selects every attachment.

    Returns:
        Matching payloads in source order.
    """

    if not selected_ids:
        return list(attachments)
    allowed = set(selected_ids)
    return [
        attachment
        for attachment in attachments
        if str(attachment.get("id") or "").strip() in allowed
    ]
