"""Application wiring tests for the runtime-backed agent use case."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_app.workflow_control.agent import (
    AgentApplicationService,
    AgentExecutionOptions,
)
from harborrag_core.models.chat import (
    HarborChatMessage,
    HarborChatResponse,
    HarborChatUsage,
)
from harborrag_runtime.chat import ChatPrompt
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


class _Chat:
    def __init__(self) -> None:
        self.requests = []
        self.prompts = []

    async def complete(self, request, *, prompt):
        self.requests.append(request)
        self.prompts.append(prompt)
        return HarborChatResponse(
            id=f"response-{len(self.requests)}",
            logical_model="primary",
            provider="mock",
            provider_model="mock-chat",
            deployment="private",
            message=HarborChatMessage.assistant(f"answer-{len(self.requests)}"),
            finish_reason="stop",
            usage=HarborChatUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
        )


@pytest.mark.asyncio
async def test_agent_service_uses_created_session_and_recalls_it_on_follow_up() -> None:
    chat = _Chat()
    runtime = SimpleNamespace(chat=chat)
    memory = InMemoryConversationMemory()
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
    )
    session_id = "session-1"
    await memory.create(ConversationIdentity("ACME", "reader-1", session_id))

    first = await service.complete(
        "first question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id=session_id),
    )
    second = await service.complete(
        "follow-up question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id=session_id),
    )

    assert first.ok is True
    assert second.ok is True
    assert second.data["session_id"] == session_id
    assert chat.prompts == [ChatPrompt.DEFAULT, ChatPrompt.DEFAULT]
    assert [message.content for message in chat.requests[1].messages[1:]] == [
        "first question",
        "answer-1",
        "follow-up question",
    ]
    assert chat.requests[1].metadata.user_id is None
    assert chat.requests[1].metadata.conversation_id == session_id
