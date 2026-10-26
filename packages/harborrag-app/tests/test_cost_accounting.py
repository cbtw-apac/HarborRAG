"""Public cost and durable accounting retain complete and partial pricing."""

from __future__ import annotations

import pytest
from chat_service_fixtures import FakeUsageRepository

from harborrag_app.workflow_control.agent.support import result_data
from harborrag_app.workflow_control.agent.turn import record_run_usage
from harborrag_app.workflow_control.chat.turn import StreamedAnswer
from harborrag_app.workflow_control.memory.identity import MemoryIdentity
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatStreamChunk,
    HarborChatUsage,
    StreamEventType,
)
from harborrag_core.models.cost import ModelCost
from harborrag_core.ports.agent_runs import AgentStopReason
from harborrag_runtime.agent import AgentRunResult


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


@pytest.mark.asyncio
@pytest.mark.parametrize("first_cost", [0.01, None])
async def test_agent_ledger_records_complete_run_cost_and_preserves_unknown(first_cost) -> None:
    usage = FakeUsageRepository()
    response = HarborChatResponse(
        id="answer",
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="private",
        message=HarborChatMessage.assistant("Answer"),
        finish_reason="stop",
        usage=HarborChatUsage(total_tokens=5),
        estimated_cost_usd=0.02,
    )
    result = AgentRunResult(
        run_id="run",
        response=response,
        executions=(),
        turns=2,
        usage=HarborChatUsage(total_tokens=10),
        stop_reason=AgentStopReason.FINAL_ANSWER,
        cost=ModelCost().add_call(first_cost).add_call(0.02),
    )
    identity = MemoryIdentity("ACME", "reader", "reader", "session")
    await record_run_usage(usage, identity, result)
    recorded = usage.records[0]
    assert recorded.total_tokens == 10
    if first_cost is None:
        assert recorded.estimated_cost_usd is None
    else:
        assert recorded.estimated_cost_usd == pytest.approx(0.03)
    public = result_data(result, session_id="session")
    assert public["cost"] == result.cost.model_dump(mode="json")
