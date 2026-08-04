"""Public response projection for chat completions."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.chat import HarborChatResponse, HarborChatStreamChunk


def citation_data(result: RetrievalResult) -> dict[str, object]:
    """Project one retrieval result as a public citation, without raw metadata."""

    return {
        "document_id": str(result.metadata.get("document_id", "")),
        "chunk_id": result.id,
        "score": result.score,
    }


def chat_response_data(
    response: HarborChatResponse,
    results: Sequence[RetrievalResult] = (),
) -> dict[str, object]:
    """Project a provider response without leaking deployment metadata."""

    return {
        "id": response.id,
        "created": response.created,
        "model": response.logical_model,
        "provider": response.provider,
        "provider_model": response.provider_model,
        "message": {
            "role": response.message.role.value,
            "content": response.text,
        },
        "finish_reason": str(response.finish_reason),
        "usage": response.usage.model_dump(mode="json"),
        "latency_ms": response.latency_ms,
        "retry_count": response.retry_count,
        "fallback_count": response.fallback_count,
        "citations": tuple(citation_data(result) for result in results),
    }


def chat_stream_chunk_data(chunk: HarborChatStreamChunk) -> dict[str, object]:
    """Project one stream chunk without leaking deployment/provider-error metadata."""

    return {
        "event": chunk.event.value,
        "model": chunk.logical_model,
        "provider": chunk.provider,
        "provider_model": chunk.provider_model,
        "content": chunk.text_delta,
        "reasoning": chunk.reasoning_delta,
        "finish_reason": chunk.finish_reason,
        "usage": chunk.usage.model_dump(mode="json") if chunk.usage is not None else None,
    }
