from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from harborrag_core.models.errors import HarborRerankMalformedResponseError
from harborrag_core.models.rerank import (
    HarborRerankRequest,
    HarborRerankResponse,
    HarborRerankResult,
    HarborRerankUsage,
    RerankDocumentContent,
)

from harborrag_core.models.common.responses import coerce_sdk_mapping, sdk_hidden_parameters
from .configs import HarborRerankProviderConfig


def normalize_rerank_response(
    raw: Any,
    *,
    request: HarborRerankRequest,
    logical_model: str,
    deployment: HarborRerankProviderConfig,
    request_id: str,
    latency_ms: float,
    retry_count: int,
) -> HarborRerankResponse:
    """Normalize scores, indexes, documents, metadata, usage, and deterministic ordering."""

    data = coerce_sdk_mapping(raw)
    raw_results = data.get("results")
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes)):
        raise HarborRerankMalformedResponseError("rerank response is missing results[]")
    if request.top_n is not None and len(raw_results) > request.top_n:
        raise HarborRerankMalformedResponseError(
            "rerank response returned more results than requested top_n"
        )
    parsed: list[tuple[int, float, RerankDocumentContent | None]] = []
    seen: set[int] = set()
    for item in raw_results:
        item_data = coerce_sdk_mapping(item)
        index = item_data.get("index")
        score = item_data.get("relevance_score", item_data.get("score"))
        if not isinstance(index, int) or not 0 <= index < len(request.documents):
            raise HarborRerankMalformedResponseError("rerank result contains an invalid index")
        if index in seen:
            raise HarborRerankMalformedResponseError("rerank response contains duplicate indices")
        seen.add(index)
        if (
            isinstance(score, bool)
            or not isinstance(score, int | float)
            or not math.isfinite(float(score))
        ):
            raise HarborRerankMalformedResponseError(
                "rerank result contains an invalid relevance score"
            )
        document = _result_document(item_data.get("document"), request, index)
        parsed.append((index, float(score), document))
    parsed.sort(key=lambda value: (-value[1], value[0]))
    results = tuple(
        HarborRerankResult(
            rank=rank,
            index=index,
            relevance_score=score,
            document_id=request.documents[index].document_id,
            document=document,
            metadata=request.documents[index].metadata,
        )
        for rank, (index, score, document) in enumerate(parsed, start=1)
    )
    hidden = sdk_hidden_parameters(raw, data)
    headers = getattr(raw, "_response_headers", None)
    header_values = dict(headers) if isinstance(headers, Mapping) else {}
    provider_request_id = (
        hidden.get("request_id")
        or hidden.get("provider_request_id")
        or header_values.get("x-request-id")
        or header_values.get("request-id")
    )
    cost = hidden.get("response_cost")
    return HarborRerankResponse(
        results=results,
        logical_model=logical_model,
        provider=str(hidden.get("custom_llm_provider") or deployment.provider.value),
        provider_model=str(hidden.get("model") or deployment.model),
        deployment=str(hidden.get("model_id") or deployment.name),
        request_id=request_id,
        response_id=str(data.get("id")) if data.get("id") else None,
        usage=normalize_rerank_usage(data.get("meta") or data.get("usage")),
        estimated_cost_usd=(float(cost) if isinstance(cost, int | float) and cost >= 0 else None),
        latency_ms=latency_ms,
        retry_count=retry_count,
        provider_request_id=str(provider_request_id) if provider_request_id else None,
        cache_hit=bool(hidden.get("cache_hit")),
        provider_metadata=_safe_provider_metadata(hidden),
    )


def normalize_rerank_usage(raw: Any) -> HarborRerankUsage:
    """Normalize provider billed units and token counts."""

    data = coerce_sdk_mapping(raw)
    billed = coerce_sdk_mapping(data.get("billed_units"))
    tokens = coerce_sdk_mapping(data.get("tokens"))
    search_units = _nonnegative_int(billed.get("search_units", data.get("search_units")))
    input_tokens = _nonnegative_int(tokens.get("input_tokens", data.get("input_tokens")))
    output_tokens = _nonnegative_int(tokens.get("output_tokens", data.get("output_tokens")))
    billed_total = _nonnegative_int(billed.get("total_tokens", data.get("total_tokens")))
    return HarborRerankUsage(
        search_units=search_units,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=billed_total or input_tokens + output_tokens,
    )


def _result_document(
    value: Any,
    request: HarborRerankRequest,
    index: int,
) -> RerankDocumentContent | None:
    if request.return_documents is False:
        return None
    normalized = _normalize_document(value)
    return request.documents[index].content if normalized is None else normalized


def _normalize_document(value: Any) -> RerankDocumentContent | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    data = coerce_sdk_mapping(value)
    if not data:
        return None
    if set(data) == {"text"} and isinstance(data.get("text"), str):
        return str(data["text"])
    return data


def _nonnegative_int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and value >= 0 else 0


def _safe_provider_metadata(hidden: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"region_name", "cache_hit", "model_id", "response_ms"}
    return {
        key: hidden[key]
        for key in allowed
        if key in hidden
        and (isinstance(hidden.get(key), str | int | float | bool) or hidden.get(key) is None)
    }
