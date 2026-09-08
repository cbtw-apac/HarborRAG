"""Tenant-scoped vector retrieval tool."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_runtime.contracts import RetrievalLane, RetrievalRequest

from .base import BaseMcpTool, McpToolSpec
from .output_schemas import RETRIEVAL_DIAGNOSTICS_SCHEMA, RETRIEVAL_RESULT_SCHEMA
from .retrieval_inputs import (
    TENANT_PROPERTY,
    access,
    boolean,
    integer,
    mapping,
    number,
    success_or_failure_schema,
    text,
)

if TYPE_CHECKING:
    from harborrag_runtime.contracts import RetrievalResponse
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.mcp.tools.vector_search")

_DEFAULT_TOP_K = 5
_MAX_TOP_K = McpToolPolicy().max_results


def _results(response: RetrievalResponse, threshold: float = 0.0) -> list[dict[str, object]]:
    return [asdict(result) for result in response.results if result.score >= threshold]


@dataclass(slots=True)
class VectorSearchTool(BaseMcpTool):
    """Vector search with explicit lane, filters, graph observation, and threshold."""

    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "vector_search",
        "Search tenant-scoped vectors with explicit retrieval controls.",
        {
            "type": "object",
            "required": ["query", "tenant_id"],
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "tenant_id": TENANT_PROPERTY,
                "top_k": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TOP_K,
                    "default": _DEFAULT_TOP_K,
                },
                "lane": {
                    "type": "string",
                    "enum": [lane.value for lane in RetrievalLane],
                    "default": RetrievalLane.HYBRID.value,
                },
                "filters": {
                    "type": "object",
                    "not": {"required": ["tenant_id"]},
                    "default": {},
                },
                "observe_graph": {"type": "boolean", "default": False},
                "score_threshold": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": 0.0,
                },
            },
            "additionalProperties": False,
        },
        output_schema=success_or_failure_schema(
            {
                "type": "object",
                "required": ["ok", "request_id", "lane", "results", "diagnostics"],
                "properties": {
                    "ok": {"const": True},
                    "request_id": {"type": "string", "minLength": 1},
                    "lane": {"type": "string", "enum": [lane.value for lane in RetrievalLane]},
                    "results": {"type": "array", "items": RETRIEVAL_RESULT_SCHEMA},
                    "diagnostics": RETRIEVAL_DIAGNOSTICS_SCHEMA,
                },
                "additionalProperties": False,
            }
        ),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            lane_value = text(
                {"lane": arguments.get("lane", RetrievalLane.HYBRID.value)},
                "lane",
            )
            lane = RetrievalLane(lane_value)
            threshold = number(
                arguments,
                "score_threshold",
                0.0,
                minimum=0.0,
                maximum=1.0,
            )
            request = RetrievalRequest(
                access=access(arguments, principal_id),
                query=text(arguments, "query"),
                top_k=integer(
                    arguments,
                    "top_k",
                    _DEFAULT_TOP_K,
                    minimum=1,
                    maximum=_MAX_TOP_K,
                ),
                filters=mapping(arguments, "filters"),
                lane=lane,
                observe_graph=boolean(arguments, "observe_graph", False),
            )
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return await _search(self.runtime, request, threshold=threshold)


async def _search(
    runtime: HarborRAG | None,
    request: RetrievalRequest,
    *,
    threshold: float = 0.0,
) -> dict[str, object]:
    if runtime is None:
        return {"ok": False, "error": "vector retrieval backend is not configured"}
    try:
        response = await runtime.retrieval.search(request)
    except Exception:
        # The caller only ever sees the generic message below; the real cause
        # (e.g. a misconfigured provider or an unreachable store) is only
        # visible in the server logs, never in the tool response.
        logger.exception("vector retrieval backend raised during search")
        return {"ok": False, "error": "vector retrieval backend failed"}
    return {
        "ok": True,
        "request_id": response.request_id,
        "lane": response.lane.value,
        "results": _results(response, threshold),
        "diagnostics": response.diagnostics,
    }
