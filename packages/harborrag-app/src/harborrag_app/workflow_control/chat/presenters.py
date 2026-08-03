"""Public response projection for chat completions."""

from __future__ import annotations

from harborrag_core.models.chat import HarborChatResponse


def chat_response_data(response: HarborChatResponse) -> dict[str, object]:
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
    }
