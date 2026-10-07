"""Shared parsing and completion semantics for bounded graph tools."""

from __future__ import annotations

import logging

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborDeadlineExceeded, HarborValidationError
from harborrag_core.retrieval import GraphDirection
from harborrag_engine.ingestion.projections.graph.source_projector_support import (
    source_provider_id,
)

from .retrieval_inputs import string_list

logger = logging.getLogger("harborrag.runtime.tools.graph_search")

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


def node_selector(value: str) -> str:
    """Reduce a canonical source item id to the provider id its graph node carries.

    vector_search hands out ``metadata.source_item_id`` values such as
    ``jira://CPM/CPM-110455`` or ``jira://CPM/CPM-66246/attachments/256636``, while the
    projection keys a source entity by the provider id it derives from that identity
    (``CPM-110455``, ``256636``) and stores it as the node's logical_id. Deriving the
    selector with the projection's own ``source_provider_id`` -- the URI scheme is the
    connector type -- keeps the two from drifting. Anything that is not a
    ``scheme://`` identity (node keys, chunk and document ids, titles) passes unchanged,
    as does a scheme the projection does not reduce.
    """

    scheme, separator, _ = value.partition("://")
    if not separator or not scheme:
        return value
    return source_provider_id(scheme, value)


def backend_failure(tool_name: str, exc: Exception, *, narrow: str) -> dict[str, object]:
    """Turn a backend exception into the tool's failure payload.

    A graph read that outlives the store's deadline is the request's bounds being too
    wide for the neighborhood, which the caller can fix, so it gets a message naming
    what to narrow; ``narrow`` lists the arguments that bound this tool's search. Every
    other backend error stays opaque so provider internals do not leak to callers.
    """

    if isinstance(exc, HarborDeadlineExceeded):
        logger.warning("%s graph query timed out", tool_name)
        return {"ok": False, "error": f"graph query timed out; narrow {narrow}"}
    logger.error("%s backend raised during call", tool_name, exc_info=exc)
    return {"ok": False, "error": "graph retrieval backend failed"}


def completion(
    diagnostics: dict[str, object], *, extra_reasons: tuple[str, ...] = ()
) -> dict[str, object]:
    reasons = list(extra_reasons)
    if diagnostics.get("projection_truncated") and "edge_limit" not in reasons:
        reasons.append("candidate_limit")
    return {"complete": not reasons, "reasons": reasons}


__all__ = [
    "COMPLETION_SCHEMA",
    "backend_failure",
    "completion",
    "direction",
    "node_selector",
    "relations",
]
