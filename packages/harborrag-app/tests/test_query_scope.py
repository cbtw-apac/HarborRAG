"""Scope decisions fail closed and retain tenant-scoped accounting and history."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_app.workflow_control.chat.preparation import ChatTurnResources
from harborrag_app.workflow_control.chat.query_scope import require_query_scope
from harborrag_app.workflow_control.memory import MemoryAccess
from harborrag_core.contracts.errors import HarborUnavailableError, HarborValidationError
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.conversation import ConversationTurn
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.config.settings import RuntimeSettings


def resources(answer, *, failure=None):
    response = HarborChatResponse(
        id="scope-1",
        logical_model="tenant-model",
        provider="mock",
        provider_model="model",
        deployment="private",
        message=HarborChatMessage.assistant(answer),
        finish_reason="stop",
        usage=HarborChatUsage(prompt_tokens=10, completion_tokens=4, total_tokens=14),
        estimated_cost_usd=0.001,
    )
    chat = SimpleNamespace(complete=AsyncMock(return_value=response, side_effect=failure))
    memory = SimpleNamespace(
        recent=AsyncMock(return_value=(ConversationTurn("My name is A", "Hi A"),))
    )
    usage = SimpleNamespace(record=AsyncMock())
    return (
        ChatTurnResources(
            lambda: SimpleNamespace(chat=chat), RuntimeSettings(), memory, usage=usage
        ),
        chat,
        memory,
        usage,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["knowledge", "conversation"])
async def test_gate_uses_selected_model_bounded_owned_history_and_records_cost(scope):
    configured, chat, memory, usage = resources(json.dumps({"scope": scope}))
    access = MemoryAccess("ACME", "credential", "human", session_id="session-1")
    await require_query_scope(
        configured, "What is my name?", access, model="tenant-model", mode="rag"
    )
    request = chat.complete.call_args.args[0]
    assert chat.complete.call_args.kwargs["prompt"] is ChatPrompt.QUERY_GATE
    assert request.logical_model == "tenant-model" and request.sensitive
    assert not request.tools and request.max_tokens == 512
    assert request.metadata.tenant_id == "ACME" and request.metadata.user_id == "human"
    assert json.loads(request.messages[0].content)["recent_history"][0]["user"] == "My name is A"
    assert memory.recent.call_args.args[0].user_id == "human"
    assert memory.recent.call_args.kwargs["limit"] == 3
    record = usage.record.call_args.args[0]
    assert record.finish_reason == "query_scope_gate"
    assert record.total_tokens == 14 and record.estimated_cost_usd == 0.001


@pytest.mark.asyncio
async def test_unsupported_snake_game_decision_blocks_and_accounts_without_history():
    configured, chat, memory, usage = resources('{"scope":"unsupported"}')
    with pytest.raises(HarborValidationError, match="unrelated content"):
        await require_query_scope(
            configured,
            "Give me Python code for a snake game",
            MemoryAccess("ACME", "credential", "human"),
            model=None,
            mode="agent",
        )
    memory.recent.assert_not_awaited()
    assert chat.complete.await_count == 1
    assert usage.record.call_args.args[0].surface == "agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", ["yes", "", '{"scope":"unknown"}', '{"scope":"knowledge","override":true}']
)
async def test_invalid_gate_output_never_falls_through_to_generation(answer):
    configured, _, _, usage = resources(answer)
    with pytest.raises(HarborUnavailableError):
        await require_query_scope(
            configured, "Question", MemoryAccess("ACME", "c", "u"), model=None, mode="rag"
        )
    usage.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_usage_outage_cannot_turn_a_rejection_into_acceptance():
    configured, _, _, usage = resources('{"scope":"unsupported"}')
    usage.record.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(HarborValidationError):
        await require_query_scope(
            configured, "Write a game", MemoryAccess("ACME", "c", "u"), model=None, mode="agent"
        )
