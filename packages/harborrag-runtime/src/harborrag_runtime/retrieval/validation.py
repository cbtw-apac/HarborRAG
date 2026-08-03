"""Validation helpers for public retrieval inputs and projection payloads."""

from __future__ import annotations

from collections.abc import Mapping


def required_text(values: Mapping[str, object], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"retrieval candidate is missing {key}")
    return value


def required_mapping(
    values: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = values.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"retrieval candidate is missing {key}")
    return value


def validate_retrieval_request(query: str, tenant_id: str, top_k: int) -> None:
    if not query.strip():
        raise ValueError("retrieval query must be non-empty")
    if not tenant_id.strip() or len(tenant_id) > 100:
        raise ValueError("retrieval tenant_id must contain between 1 and 100 characters")
    if not 1 <= top_k <= 100:
        raise ValueError("retrieval top_k must be between 1 and 100")
