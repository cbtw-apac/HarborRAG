from __future__ import annotations

from dataclasses import asdict, dataclass, field

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.retrieval.base import BaseRetrievalPipeline
from harborrag_engine.retrieval.mock import MockRetrievalPipeline

from harborrag_mcp.tools.base import BaseMcpTool, McpToolSpec

_DEFAULT_TOP_K = 5

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

        top_k = int(arguments.get("top_k", _DEFAULT_TOP_K))  # type: ignore[arg-type]
        filters: dict[str, object] = dict(arguments.get("filters") or {})  # type: ignore[arg-type]

        raw_threshold = arguments.get("score_threshold", _DEFAULT_SCORE_THRESHOLD)
        score_threshold = float(raw_threshold)
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
