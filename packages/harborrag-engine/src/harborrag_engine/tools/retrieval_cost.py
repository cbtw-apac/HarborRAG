"""Explicit accounting boundary for retrieval model calls."""

from __future__ import annotations

RETRIEVAL_COST_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["amount_usd", "currency", "status", "complete", "scope"],
    "properties": {
        "amount_usd": {"type": "null"},
        "currency": {"const": "USD"},
        "status": {"const": "unavailable"},
        "complete": {"const": False},
        "scope": {"const": "retrieval"},
    },
    "additionalProperties": False,
}


def unavailable_retrieval_cost() -> dict[str, object]:
    """Do not infer provider charges without LiteLLM invocation cost telemetry."""

    return {
        "amount_usd": None,
        "currency": "USD",
        "status": "unavailable",
        "complete": False,
        "scope": "retrieval",
    }
