from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

from harborrag_core.domain.retrieval import RetrievalQuery
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_mcp_server.tools.base import BaseMcpTool, McpToolSpec

_DEFAULT_TOP_K = 5
_MAX_TOP_K = McpToolPolicy().max_results

_DEFAULT_SCORE_THRESHOLD = 0.3


@dataclass(slots=True)
class VectorSearchTool(BaseMcpTool):
    """Search the vector store and return ranked retrieval results.

    Wraps an injected retrieval pipeline from the runtime.

    Optional pre-search parameters:
    - ``score_threshold``: drop results whose score is below this value (0.0–1.0).
    - ``filters``: key/value metadata pairs forwarded to the pipeline as
      :attr:`RetrievalQuery.filters` so adapters can push them to the index.
    """

    pipeline: object | None = None
    spec = McpToolSpec(
        "vector_search",
        "Search the vector store and return ranked results.",
        {
            "type": "object",
            "required": ["query", "filters"],
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
                        "Required key/value metadata filters. Must include tenant_id "
                        "for retrieval scope."
                    ),
                    "required": ["tenant_id"],
                    "properties": {
                        "tenant_id": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Required tenant scope for retrieval.",
                        }
                    },
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

        raw_filters = arguments.get("filters")
        if not isinstance(raw_filters, Mapping):
            return {
                "ok": False,
                "error": "filters is required and must be an object",
            }
        filters = dict(raw_filters)

        tenant_id = filters.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            return {
                "ok": False,
                "error": "filters.tenant_id must be a non-empty string",
            }

        raw_threshold = arguments.get("score_threshold", _DEFAULT_SCORE_THRESHOLD)
        try:
            score_threshold = float(raw_threshold)
        except (TypeError, ValueError):
            return {"ok": False, "error": "score_threshold must be a number"}
        if score_threshold < 0.0 or score_threshold > 1.0:
            return {"ok": False, "error": "score_threshold must be between 0.0 and 1.0"}

        if self.pipeline is None:
            return {
                "ok": False,
                "error": "vector_search backend is not configured",
            }
        if not hasattr(self.pipeline, "retrieve"):
            return {
                "ok": False,
                "error": "vector_search backend does not implement retrieve(query)",
            }

        retrieval_query = RetrievalQuery(
            text=query_text,
            top_k=top_k,
            filters=filters,
        )
        results = self.pipeline.retrieve(retrieval_query)

        results = [r for r in results if r.score >= score_threshold]

        return {
            "ok": True,
            "query": query_text,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "results": [asdict(r) for r in results],
        }
