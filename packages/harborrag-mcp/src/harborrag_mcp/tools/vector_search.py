from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.retrieval.base import BaseRetrievalPipeline
from harborrag_engine.retrieval.mock import MockRetrievalPipeline

from harborrag_mcp.policy import McpToolPolicy
from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec

_DEFAULT_TOP_K = 5
_MAX_TOP_K = McpToolPolicy().max_results
_MISSING = object()

_DEFAULT_SCORE_THRESHOLD = 0.3

_CANNED_RESULTS: list[RetrievalResult] = [
    RetrievalResult("vec-1", "HarborRAG vector search result one", 0.95),
    RetrievalResult("vec-2", "HarborRAG vector search result two", 0.82),
    RetrievalResult("vec-3", "HarborRAG vector search result three", 0.74),
]


@dataclass(slots=True)
class VectorSearchTool(BaseMcpTool):
    """Search the vector store and return ranked retrieval results.

    Wraps a :class:`BaseRetrievalPipeline`; defaults to the deterministic
    :class:`MockRetrievalPipeline` so the tool is usable without a live DB.

    Optional pre-search parameters:
    - ``score_threshold``: drop results whose score is below this value (0.0–1.0).
    - ``filters``: key/value metadata pairs forwarded to the pipeline as
      :attr:`RetrievalQuery.filters` so adapters can push them to the index.
    """

    pipeline: BaseRetrievalPipeline = field(
        default_factory=lambda: MockRetrievalPipeline(_CANNED_RESULTS)
    )
    spec = McpToolSpec(
        "vector_search",
        "Search the vector store and return ranked results.",
        {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "top_k": {
                    "type": "integer",
                    "description": f"Maximum number of results to return (default {_DEFAULT_TOP_K}).",
                    "minimum": 1,
                    "maximum": _MAX_TOP_K,
                    "default": _DEFAULT_TOP_K,
                },
                "score_threshold": {
                    "type": "number",
                    "description": (
                        "Minimum score in [0, 1]. Defaults to 0.3; results below "
                        "this threshold are dropped before returning."
                    ),
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "default": _DEFAULT_SCORE_THRESHOLD,
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional key/value metadata filters forwarded to the pipeline "
                        "before the search runs."
                    ),
                },
            },
        },
    )

    def call(self, arguments: dict[str, object]) -> dict[str, object]:
        query_text = str(arguments.get("query", "")).strip()
        if not query_text:
            return {"ok": False, "error": "query must be a non-empty string"}

        raw_top_k = arguments.get("top_k", _DEFAULT_TOP_K)
        try:
            top_k = int(raw_top_k)
        except (TypeError, ValueError):
            return {"ok": False, "error": "top_k must be an integer"}
        if top_k < 1 or top_k > _MAX_TOP_K:
            return {
                "ok": False,
                "error": f"top_k must be between 1 and {_MAX_TOP_K}",
            }

        raw_filters = arguments.get("filters", _MISSING)
        if raw_filters is _MISSING:
            filters: dict[str, object] = {}
        elif isinstance(raw_filters, Mapping):
            filters = dict(raw_filters)
        else:
            return {"ok": False, "error": "filters must be an object"}

        raw_threshold = arguments.get("score_threshold", _DEFAULT_SCORE_THRESHOLD)
        try:
            score_threshold = float(raw_threshold)
        except (TypeError, ValueError):
            return {"ok": False, "error": "score_threshold must be a number"}
        if score_threshold < 0.0 or score_threshold > 1.0:
            return {"ok": False, "error": "score_threshold must be between 0.0 and 1.0"}

        retrieval_query = RetrievalQuery(text=query_text, top_k=top_k, filters=filters)
        results = self.pipeline.retrieve(retrieval_query)

        results = [r for r in results if r.score >= score_threshold]

        return {
            "ok": True,
            "query": query_text,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "results": [asdict(r) for r in results],
        }
