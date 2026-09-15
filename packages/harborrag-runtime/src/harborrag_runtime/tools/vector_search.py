"""Tenant-scoped vector retrieval tool."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_runtime.contracts import RetrievalLane, RetrievalMode, RetrievalRequest
from harborrag_runtime.tools.base import MAX_TOOL_RESULTS

from .base import BaseTool, ToolSpec
from .output_schemas import RETRIEVAL_DIAGNOSTICS_SCHEMA, RETRIEVAL_RESULT_SCHEMA
from .retrieval_cost import RETRIEVAL_COST_SCHEMA, unavailable_retrieval_cost
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
from .retrieval_schemas import vector_search_schema

if TYPE_CHECKING:
    from harborrag_runtime.contracts import RetrievalResponse
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.runtime.tools.vector_search")

_DEFAULT_TOP_K = 5
_MAX_TOP_K = MAX_TOOL_RESULTS
_ANNOTATIONS: dict[str, object] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def _results(
    response: RetrievalResponse, threshold: float = 0.0, *, include_content: bool = True
) -> list[dict[str, object]]:
    results = [asdict(result) for result in response.results if result.score >= threshold]
    if not include_content:
        for result in results:
            result.pop("text", None)
    return results


@dataclass(slots=True)
class VectorSearchTool(BaseTool):
    """Vector search with explicit lane, filters, graph observation, and threshold."""

    runtime: HarborRAG | None = None
    spec = ToolSpec(
        "vector_search",
        "Search tenant-scoped vectors with explicit retrieval controls. Set include_content "
        "to false for compact discovery, then fetch_evidence for selected chunk IDs.",
        vector_search_schema(max_results=_MAX_TOP_K, tenant=TENANT_PROPERTY),
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
                    "cost": RETRIEVAL_COST_SCHEMA,
                },
                "additionalProperties": False,
            }
        ),
        annotations=_ANNOTATIONS,
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
                mode=RetrievalMode(str(arguments.get("mode", RetrievalMode.FLAT.value))),
                observe_graph=boolean(arguments, "observe_graph", False),
            )
            include_content = boolean(arguments, "include_content", True)
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return await _search(
            self.runtime, request, threshold=threshold, include_content=include_content
        )


async def _search(
    runtime: HarborRAG | None,
    request: RetrievalRequest,
    *,
    threshold: float = 0.0,
    include_content: bool = True,
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
        "results": _results(response, threshold, include_content=include_content),
        "diagnostics": response.diagnostics,
        "cost": unavailable_retrieval_cost(),
    }
