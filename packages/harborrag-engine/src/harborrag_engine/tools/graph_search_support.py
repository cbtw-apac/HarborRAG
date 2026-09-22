"""Shared parsing and completion semantics for bounded graph tools."""

from __future__ import annotations

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.retrieval import GraphDirection

from .retrieval_inputs import string_list

COMPLETION_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["complete", "reasons"],
    "properties": {
        "complete": {"type": "boolean"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "additionalProperties": False,
}


def relations(arguments: dict[str, object]) -> tuple[RelationType, ...]:
    return tuple(RelationType(item) for item in string_list(arguments, "relationship_types"))


def direction(
    arguments: dict[str, object],
    default: GraphDirection,
) -> GraphDirection:
    value = arguments.get("direction", default.value)
    if not isinstance(value, str):
        raise HarborValidationError("direction must be incoming, outgoing, or both")
    try:
        return GraphDirection(value)
    except ValueError:
        raise HarborValidationError("direction must be incoming, outgoing, or both") from None


def completion(
    diagnostics: dict[str, object], *, extra_reasons: tuple[str, ...] = ()
) -> dict[str, object]:
    reasons = list(extra_reasons)
    if diagnostics.get("projection_truncated") and "edge_limit" not in reasons:
        reasons.append("candidate_limit")
    return {"complete": not reasons, "reasons": reasons}


__all__ = ["COMPLETION_SCHEMA", "completion", "direction", "relations"]
