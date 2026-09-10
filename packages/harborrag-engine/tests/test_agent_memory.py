"""The engine leaves one user and one assistant history message per run."""

from __future__ import annotations

import pytest
from agent_test_helpers import Chat, Memory, Tools
from agent_test_helpers import response as _response

from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_engine.agent import AgentRunOptions, AgentService
from harborrag_engine.conversation import run_exchange_messages


@pytest.mark.asyncio
async def test_run_appends_question_and_final_answer_tagged_with_run_id() -> None:
    memory = Memory()
    service = AgentService(
        Chat([_response(call=("call-1", "vector_search", "{}")), _response(text="final")]),
        Tools(),
        memory=memory,
    )

    result = await service.run(
        [HarborChatMessage.user("question")],
        AgentRunOptions(tenant_id="ACME", principal_id="principal-1", session_id="session-1"),
    )

    identity = ConversationIdentity("ACME", "principal-1", "session-1", "principal-1")
    user, assistant = memory.messages[identity]
    assert (user.role, user.content, user.run_id) == ("user", "question", result.run_id)
    assert (assistant.role, assistant.content, assistant.run_id) == (
        "assistant",
        "final",
        result.run_id,
    )
    assert assistant.token_count == 1
    # Tool traffic lives in the run checkpoint, never in conversation history.
    assert assistant.tool_calls_json is None
    assert [message.role for message in memory.messages[identity]] == ["user", "assistant"]


def test_run_exchange_messages_omits_prompt_token_attribution() -> None:
    user, assistant = run_exchange_messages("q", "a", run_id="run-1", completion_tokens=7)

    assert user.token_count is None
    assert assistant.token_count == 7
    assert user.message_id != assistant.message_id
    assert user.created_at.tzinfo is not None
