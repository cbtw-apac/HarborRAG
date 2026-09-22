"""Public agent history uses complete recent exchanges without summary calls."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from chat_service_fixtures import FakeMemoryFacade, fake_runtime_config
from test_agent_service import _Chat

from harborrag_app.workflow_control.agent import AgentApplicationService, AgentExecutionOptions
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.memory import (
    ConversationIdentity,
    InMemoryConversationMemory,
    MemoryContext,
    MemoryContextRequest,
    MemoryPolicy,
)

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
OPTIONS = AgentExecutionOptions(session_id="session-1", user_id="reader-1")


class SummarizingMemoryFacade(FakeMemoryFacade):
    def __init__(self, summary: str) -> None:
        super().__init__(MemoryPolicy(recent_max_messages=4, summary_keep_messages=4))
        self.summary = summary

    async def build_context(
        self,
        request: MemoryContextRequest,
        *,
        messages: object,
        memories: object = None,
        index: object = None,
    ) -> MemoryContext:
        context = await super().build_context(
            request, messages=messages, memories=memories, index=index
        )
        return MemoryContext(
            messages=context.messages,
            summary=self.summary,
            recalled=(),
            standalone_query=context.standalone_query,
            rewritten=False,
            summary_written=False,
        )


class FailingHistory(InMemoryConversationMemory):
    async def recent_complete_messages(self, identity, *, limit=3):
        raise RuntimeError("memory store is down")


async def _service(
    chat: _Chat,
    *,
    memory_facade: object | None = None,
    memory: InMemoryConversationMemory | None = None,
) -> tuple[AgentApplicationService, InMemoryConversationMemory]:
    store = memory or InMemoryConversationMemory()
    await store.create(IDENTITY, kind="agent")
    runtime = SimpleNamespace(
        chat=chat, memory=memory_facade or FakeMemoryFacade(), config=fake_runtime_config()
    )
    service = AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=store,
        runs=InMemoryAgentRunRepository(),
    )
    return service, store


@pytest.mark.asyncio
async def test_the_policy_window_is_replayed_on_the_next_run() -> None:
    chat = _Chat()
    service, _ = await _service(chat)

    await service.complete("first", tenant_id="ACME", principal_id="reader-1", options=OPTIONS)
    await service.complete("second", tenant_id="ACME", principal_id="reader-1", options=OPTIONS)

    replayed = [str(message.content) for message in chat.requests[1].messages]
    assert replayed[1:3] == ["first", "answer-1"]
    assert replayed[-1] == "second"


@pytest.mark.asyncio
async def test_public_agent_does_not_inject_stored_summary() -> None:
    chat = _Chat()
    service, _ = await _service(
        chat,
        memory_facade=SummarizingMemoryFacade("User is migrating the billing service."),
    )

    await service.complete("question", tenant_id="ACME", principal_id="reader-1", options=OPTIONS)

    developer = chat.requests[0].messages[0]
    assert developer.role == "developer"
    assert "User is migrating the billing service." not in str(developer.content)


@pytest.mark.asyncio
async def test_a_memory_failure_runs_the_agent_without_history(
    caplog: pytest.LogCaptureFixture,
) -> None:
    chat = _Chat()
    service, _ = await _service(chat, memory=FailingHistory())

    response = await service.complete(
        "question",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    assert response.ok is True
    replayed = [str(message.content) for message in chat.requests[0].messages]
    assert replayed[-1] == "question"
    assert "Conversation context unavailable" in caplog.text
    assert "question" not in caplog.text
