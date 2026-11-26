"""Validation helpers for public retrieval inputs and projection payloads."""

from __future__ import annotations

from collections.abc import Mapping


def required_text(values: Mapping[str, object], key: str) -> str:
    """Return a required non-blank string field.

    Args:
        values: Payload or input to read.
        key: Name of the field.

    Returns:
        The field's value.

    Raises:
        ValueError: The field is missing or not a non-blank string.
    """
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"retrieval candidate is missing {key}")
    return value


def required_text_values(values: Mapping[str, object], key: str) -> list[str]:
    """Return a required, non-empty list of non-blank strings.

    One string reads as a one-item list, which is how points written before the
    field became a list still store it.

    Args:
        values: Payload or input to read.
        key: Name of the field.

    Returns:
        The field's strings, in stored order.

    Raises:
        ValueError: The field is missing, empty, or holds a blank or non-string item.
    """
    value = values.get(key)
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list | tuple) or not items:
        raise ValueError(f"retrieval candidate is missing {key}")
    texts = [item for item in items if isinstance(item, str) and item.strip()]
    if len(texts) != len(items):
        raise ValueError(f"retrieval candidate is missing {key}")
    return texts


def required_mapping(
    values: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    """Return a required mapping field.

    Args:
        values: Payload or input to read.
        key: Name of the field.

    Returns:
        The field's mapping.

    Raises:
        ValueError: The field is missing or not a mapping.
    """
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"retrieval candidate is missing {key}")
    return value


def validate_retrieval_request(query: str, tenant_id: str, top_k: int) -> None:
    """Validate the public retrieval inputs.

    Args:
        query: Search text; must not be blank.
        tenant_id: Tenant to search, 1 to 100 characters.
        top_k: Results to return, from 1 to 100.

    Raises:
        ValueError: The query is blank, the tenant id is not 1 to 100 characters,
            or ``top_k`` is outside 1 to 100.
    """
    if not query.strip():
        raise ValueError("retrieval query must be non-empty")
    if not tenant_id.strip() or len(tenant_id) > 100:
        raise ValueError("retrieval tenant_id must contain between 1 and 100 characters")
    if not 1 <= top_k <= 100:
        raise ValueError("retrieval top_k must be between 1 and 100")
