"""Tenant-scoped vector retrieval tool."""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.contracts.reader import RetrievalLane, RetrievalMode, RetrievalRequest
from harborrag_core.contracts.tools import ToolBehavior, ToolInvocationContext
from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_engine.retrieval.evidence_filters import validate_evidence_filter_keys
from harborrag_engine.tools.base import MAX_TOOL_RESULTS

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
    from harborrag_core.contracts.reader import RetrievalResponse
    from harborrag_core.ports.reader import ReaderServices, RetrievalReader

logger = logging.getLogger("harborrag.runtime.tools.vector_search")

_DEFAULT_TOP_K = 5
_MAX_TOP_K = MAX_TOOL_RESULTS
# Per-hit metadata no consumer of this tool reads, dropped so a 30-character
# comment does not travel with a kilobyte of bookkeeping. ``record_kind`` and
# ``retrieval_source`` are the same constant on every hit; ``raw_score`` is a
# lane-internal number (cosine, BM25 or RRF) that is comparable to nothing;
# ``quality_score`` is a chunker default (1.0 for every record chunk); and
# ``content_hash`` has done its job once retrieval collapsed identical texts.
# In-process callers still see them on ``RetrievalResult.metadata``.
TRIMMED_METADATA_KEYS = frozenset(
    {"record_kind", "retrieval_source", "raw_score", "quality_score", "content_hash"}
)


def _meets(result: RetrievalResult, threshold: float) -> bool:
    """Whether a hit is known to be at least ``threshold`` similar to the query.

    ``score`` is rank-fusion arithmetic on the hybrid lane, so its top hit sits
    near 1.0 however poor the match, and thresholding it meant a request for
    high-quality results returned the top hit regardless. ``relevance`` is the
    measured dense similarity. A hit without one -- nothing measured it -- cannot
    be shown to meet a bar, so a non-zero threshold drops it rather than letting
    a squashed keyword score stand in for similarity.
    """

    if threshold <= 0.0:
        return True
    return result.relevance is not None and result.relevance >= threshold


def _results(
    response: RetrievalResponse, threshold: float = 0.0, *, include_content: bool = True
) -> list[dict[str, object]]:
    results = [asdict(result) for result in response.results if _meets(result, threshold)]
    for result in results:
        if not include_content:
            result.pop("text", None)
        metadata = result.get("metadata")
        if isinstance(metadata, dict):
            result["metadata"] = {
                key: value for key, value in metadata.items() if key not in TRIMMED_METADATA_KEYS
            }
    return results


@dataclass(slots=True)
class VectorSearchTool(BaseTool):
    """Vector search with explicit lane, filters, graph observation, and threshold."""

    runtime: ReaderServices | None = None
    reader: RetrievalReader | None = None
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
        behavior=ToolBehavior(read_only=True, idempotent=True),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        if context is not None and principal_id != context.access.principal_id:
            raise PermissionError("tool principal does not match invocation context")
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
            if threshold > 0.0 and lane == RetrievalLane.SPARSE:
                raise ValueError(
                    "score_threshold filters on dense cosine similarity, which the sparse "
                    "lane does not measure; use lane dense or hybrid, or omit it"
                )
            filters = mapping(arguments, "filters")
            validate_evidence_filter_keys(filters)
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
                filters=filters,
                lane=lane,
                mode=RetrievalMode(str(arguments.get("mode", RetrievalMode.FLAT.value))),
                observe_graph=boolean(arguments, "observe_graph", False),
            )
            include_content = boolean(arguments, "include_content", True)
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return await _search(
            self.reader or (self.runtime.retrieval if self.runtime is not None else None),
            request,
            threshold=threshold,
            include_content=include_content,
        )


async def _search(
    reader: RetrievalReader | None,
    request: RetrievalRequest,
    *,
    threshold: float = 0.0,
    include_content: bool = True,
) -> dict[str, object]:
    if reader is None:
        return {"ok": False, "error": "vector retrieval backend is not configured"}
    try:
        response = await reader.search(request)
    except HarborValidationError as exc:
        # A refused filter (an unindexed source field, a range on an exact-match
        # key) is the caller's to fix, so it says what to fix instead of "failed".
        return {"ok": False, "error": str(exc)}
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
