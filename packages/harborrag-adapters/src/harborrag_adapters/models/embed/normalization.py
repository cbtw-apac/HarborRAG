from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from harborrag_core.models.embed import HarborEmbedding, HarborEmbedResponse, HarborEmbedUsage
from harborrag_core.models.errors import HarborEmbedMalformedResponseError

from harborrag_core.models.common.responses import coerce_sdk_mapping, sdk_hidden_parameters
from .configs import HarborEmbedProviderConfig


def normalize_embedding_batch(
    raw: Any,
    *,
    input_count: int,
    index_offset: int,
    logical_model: str,
    embedding_space: str,
    deployment: HarborEmbedProviderConfig,
    request_id: str,
    latency_ms: float,
    normalize_vectors: bool,
) -> HarborEmbedResponse:
    """Normalize one provider batch while restoring stable global input indexes."""

    data = coerce_sdk_mapping(raw)
    items = data.get("data")
    if not isinstance(items, list):
        raise HarborEmbedMalformedResponseError("embedding response is missing data[]")
    if len(items) != input_count:
        raise HarborEmbedMalformedResponseError(
            f"embedding response returned {len(items)} vectors for {input_count} inputs"
        )
    embeddings: list[HarborEmbedding] = []
    seen: set[int] = set()
    expected_dimensions = (
        deployment.expected_dimensions or deployment.capabilities.default_dimensions
    )
    for position, item in enumerate(items):
        item_data = coerce_sdk_mapping(item)
        raw_index = item_data.get("index", position)
        if not isinstance(raw_index, int) or not 0 <= raw_index < input_count:
            raise HarborEmbedMalformedResponseError("embedding response contains an invalid index")
        if raw_index in seen:
            raise HarborEmbedMalformedResponseError("embedding response contains duplicate indices")
        seen.add(raw_index)
        value, dimensions = _normalize_value(
            item_data.get("embedding"),
            normalize_vectors=normalize_vectors,
            expected_dimensions=expected_dimensions,
        )
        if expected_dimensions is not None and dimensions not in {None, expected_dimensions}:
            raise HarborEmbedMalformedResponseError(
                f"deployment expected {expected_dimensions} dimensions but returned {dimensions}"
            )
        embeddings.append(
            HarborEmbedding(
                index=index_offset + raw_index,
                value=value,
                dimensions=dimensions,
            )
        )
    embeddings.sort(key=lambda item: item.index)
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
    return HarborEmbedResponse(
        embeddings=tuple(embeddings),
        logical_model=logical_model,
        embedding_space=embedding_space,
        provider=str(hidden.get("custom_llm_provider") or deployment.provider.value),
        provider_model=str(data.get("model") or deployment.model),
        deployment=str(hidden.get("model_id") or deployment.name),
        request_id=request_id,
        usage=normalize_embed_usage(data.get("usage")),
        estimated_cost_usd=(float(cost) if isinstance(cost, int | float) and cost >= 0 else None),
        latency_ms=latency_ms,
        normalized=normalize_vectors,
        provider_request_id=str(provider_request_id) if provider_request_id else None,
        cache_hit=bool(hidden.get("cache_hit")),
        provider_metadata=_safe_provider_metadata(hidden),
    )


def merge_embedding_batches(
    responses: list[HarborEmbedResponse],
    *,
    request_id: str,
    total_latency_ms: float,
    retry_count: int,
) -> HarborEmbedResponse:
    """Aggregate batches, usage, cost, and vectors without changing input order."""

    if not responses:
        raise HarborEmbedMalformedResponseError("no embedding batches were returned")
    first = responses[0]
    for response in responses[1:]:
        if (
            response.provider != first.provider
            or response.provider_model != first.provider_model
            or response.deployment != first.deployment
        ):
            raise HarborEmbedMalformedResponseError(
                "embedding batches were produced by different deployments"
            )
        if response.dimensions != first.dimensions:
            raise HarborEmbedMalformedResponseError(
                "embedding batches returned inconsistent dimensions"
            )
    usage = HarborEmbedUsage()
    embeddings: list[HarborEmbedding] = []
    total_cost = 0.0
    cost_known = True
    for response in responses:
        usage += response.usage
        embeddings.extend(response.embeddings)
        if response.estimated_cost_usd is None:
            cost_known = False
        else:
            total_cost += response.estimated_cost_usd
    embeddings.sort(key=lambda item: item.index)
    return first.model_copy(
        update={
            "embeddings": tuple(embeddings),
            "request_id": request_id,
            "usage": usage,
            "estimated_cost_usd": total_cost if cost_known else None,
            "latency_ms": total_latency_ms,
            "retry_count": retry_count,
        }
    )


def normalize_embed_usage(raw: Any) -> HarborEmbedUsage:
    """Normalize prompt/input and total token usage from one embedding batch."""

    data = coerce_sdk_mapping(raw)
    prompt = _nonnegative_int(data.get("prompt_tokens", data.get("input_tokens", 0)))
    total = _nonnegative_int(data.get("total_tokens", prompt))
    return HarborEmbedUsage(prompt_tokens=prompt, total_tokens=total)


def _normalize_value(
    value: Any,
    *,
    normalize_vectors: bool,
    expected_dimensions: int | None,
) -> tuple[tuple[float, ...] | str, int | None]:
    if isinstance(value, list):
        try:
            vector = tuple(float(number) for number in value)
        except (TypeError, ValueError) as exc:
            raise HarborEmbedMalformedResponseError(
                "embedding response contains a non-numeric vector",
                original_exception=exc,
            ) from exc
        if not vector or any(not math.isfinite(number) for number in vector):
            raise HarborEmbedMalformedResponseError(
                "embedding response contains an empty or non-finite vector"
            )
        return (_l2_normalize(vector) if normalize_vectors else vector), len(vector)
    if isinstance(value, str):
        if normalize_vectors:
            raise HarborEmbedMalformedResponseError("base64 embeddings cannot be normalized")
        return value, expected_dimensions
    raise HarborEmbedMalformedResponseError(
        "embedding response contains an unsupported embedding value"
    )


def _l2_normalize(vector: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(number * number for number in vector))
    if norm == 0:
        raise HarborEmbedMalformedResponseError("cannot normalize a zero embedding vector")
    return tuple(number / norm for number in vector)


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
