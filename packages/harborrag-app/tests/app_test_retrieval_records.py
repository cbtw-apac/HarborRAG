"""The retrieval payload shared by the app test doubles."""

from __future__ import annotations

from harborrag_runtime.sdk import RetrievalLane


def retrieval_payload(
    *,
    lane: RetrievalLane,
    top_k: int,
    include_content: bool,
    include_metadata: bool,
    score_threshold: float,
) -> dict[str, object]:
    """Return the single-hit retrieval envelope the app service double reports."""

    result: dict[str, object] = {
        "rank": 1,
        "id": "chunk-1",
        "score": 0.9,
        "source": "hybrid",
    }
    if include_content:
        result["content"] = "retrieved text"
    if include_metadata:
        result["metadata"] = {"category": "architecture"}
    return {
        "request_id": "mock-retrieval",
        "lane": lane.value,
        "results": ([result][:top_k] if score_threshold <= 0.9 else []),
        "diagnostics": {
            "vector_hits": 1,
            "graph_nodes": 2,
            "graph_edges": 1,
            "graph_hits": 1,
            "graph_truncated": False,
            "duration_ms": 1.0,
        },
    }
