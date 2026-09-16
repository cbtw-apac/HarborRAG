"""Project the completed stream through the same response contract as JSON."""

from __future__ import annotations

from uuid import uuid4

from harborrag_core.models.chat import (
    FinishReason,
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
)

from .preparation import PreparedTurn
from .presenters import chat_response_data
from .turn import StreamedAnswer


def stream_result(
    answer: StreamedAnswer,
    prepared: PreparedTurn,
    *,
    session_id: str,
    project_id: str | None,
    memory_persisted: bool,
) -> dict[str, object]:
    chunk = answer.last_chunk
    if chunk is None:
        raise ValueError("a completed stream must contain a model chunk")
    delivered = answer.delivered()
    response = HarborChatResponse(
        id=chunk.response_id or chunk.request_id or f"chat-{uuid4().hex}",
        logical_model=chunk.logical_model,
        provider=chunk.provider,
        provider_model=chunk.provider_model,
        deployment=chunk.deployment,
        message=HarborChatMessage.assistant(delivered.text),
        finish_reason=FinishReason.parse(answer.finish_reason),
        usage=answer.usage or HarborChatUsage(),
        estimated_cost_usd=(
            delivered.call.estimated_cost_usd if delivered.call is not None else None
        ),
    )
    return chat_response_data(
        response,
        prepared.results,
        session_id=session_id,
        project_id=project_id,
        memory_persisted=memory_persisted,
    )
