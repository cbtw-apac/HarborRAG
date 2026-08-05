from __future__ import annotations

from itertools import islice

from pydantic_core import to_jsonable_python

from harborrag_runtime.sdk import RetrievalResponse

from .schemas import AppResponse


def retrieval_response(
    response: RetrievalResponse,
    *,
    include_content: bool,
    include_metadata: bool,
    top_k: int,
    score_threshold: float = 0.0,
) -> AppResponse:
    """Map an authoritative retrieval report onto the transport-neutral envelope."""

    if top_k < 1:
        raise ValueError("retrieval top_k must be positive")
    results: list[dict[str, object]] = []
    selected = islice(
        (item for item in response.results if item.score >= score_threshold),
        top_k,
    )
    for rank, item in enumerate(selected, start=1):
        result: dict[str, object] = {
            "rank": rank,
            "id": item.id,
            "score": item.score,
            "source": item.metadata.get("retrieval_source", "hybrid"),
        }
        if include_content:
            result["content"] = item.text
        if include_metadata:
            result["metadata"] = to_jsonable_python(item.metadata)
        results.append(result)
    return AppResponse(
        True,
        {
            "request_id": response.request_id,
            "lane": response.lane.value,
            "results": results,
            "diagnostics": to_jsonable_python(response.diagnostics),
        },
    )
