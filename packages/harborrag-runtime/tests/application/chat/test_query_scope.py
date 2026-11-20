"""Scope decisions fail closed and retain tenant-scoped accounting and history."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.conversation import ConversationTurn
from harborrag_memory.access import MemoryAccess
from harborrag_runtime.application.chat.preparation import ChatTurnResources
from harborrag_runtime.application.chat.query_scope import (
    ScopeCheck,
    check_query_scope,
    scope_history,
)
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.config.settings import RuntimeSettings

pytestmark = pytest.mark.unit


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


async def gate(configured, query, access, *, model) -> ScopeCheck:
    history = await scope_history(configured, access)
    return await check_query_scope(configured, query, access, history=history, model=model)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["knowledge", "conversation"])
async def test_gate_uses_selected_model_bounded_owned_history_and_records_cost(scope):
    configured, chat, memory, usage = resources(json.dumps({"scope": scope}))
    access = MemoryAccess("ACME", "credential", "human", session_id="session-1")
    check = await gate(configured, "What is my name?", access, model="tenant-model")
    assert check.refusal is None
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
    check = await gate(
        configured,
        "Give me Python code for a snake game",
        MemoryAccess("ACME", "credential", "human"),
        model=None,
    )
    assert check.refusal is not None and "unrelated content" in check.refusal
    memory.recent.assert_not_awaited()
    assert chat.complete.await_count == 1
    assert usage.record.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer", ["yes", "", '{"scope":"unknown"}', '{"scope":"knowledge","override":true}']
)
async def test_invalid_gate_output_never_falls_through_to_generation(answer):
    configured, _, _, usage = resources(answer)
    with pytest.raises(HarborUnavailableError):
        await gate(configured, "Question", MemoryAccess("ACME", "c", "u"), model=None)
    usage.record.assert_awaited_once()


@pytest.mark.asyncio
async def test_usage_outage_cannot_turn_a_rejection_into_acceptance():
    configured, _, _, usage = resources('{"scope":"unsupported"}')
    usage.record.side_effect = RuntimeError("storage unavailable")
    check = await gate(configured, "Write a game", MemoryAccess("ACME", "c", "u"), model=None)
    assert check.refusal is not None
