"""Public cost and durable accounting retain complete and partial pricing."""

from __future__ import annotations

from harborrag_app.workflow_control.chat.turn import StreamedAnswer
from harborrag_core.models.chat import HarborChatStreamChunk, HarborChatUsage, StreamEventType


def test_stream_retains_cost_when_later_chunks_omit_it() -> None:
    answer = StreamedAnswer()
    chunk = HarborChatStreamChunk(
        event=StreamEventType.USAGE,
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        usage=HarborChatUsage(total_tokens=10),
        estimated_cost_usd=0.03,
    )
    answer.observe(chunk)
    answer.observe(
        chunk.model_copy(update={"event": StreamEventType.COMPLETED, "estimated_cost_usd": None})
    )
    delivered = answer.delivered()
    assert delivered.call is not None
    assert delivered.call.estimated_cost_usd == 0.03
