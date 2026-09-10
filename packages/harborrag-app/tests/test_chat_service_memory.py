"""Chat application-service tests for memory persistence, session kind, and serialization."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRuntime, replayed

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.contracts.errors import HarborNotFoundError
from harborrag_core.ports.conversation import ConversationMessage
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


class _BrokenAppendMemory(InMemoryConversationMemory):
    async def append_messages(
        self, identity: ConversationIdentity, messages: Sequence[ConversationMessage]
    ) -> None:
        del identity, messages
        raise RuntimeError("database gone: prompt text must not be logged")


class _BrokenQuestionAppendMemory(InMemoryConversationMemory):
    """Reject the first append -- the question -- and accept everything after.

    The two halves of a turn are written separately, so a store can lose one
    and keep the other. Losing the question is the asymmetric case: the answer
    lands with nothing in front of it.
    """

    def __init__(self) -> None:
        super().__init__()
        self.appends = 0

    async def append_messages(
        self, identity: ConversationIdentity, messages: Sequence[ConversationMessage]
    ) -> None:
        self.appends += 1
        if self.appends == 1:
            raise RuntimeError("database gone: prompt text must not be logged")
        await super().append_messages(identity, messages)


def _chat_service(
    runtime: FakeRuntime, memory: InMemoryConversationMemory
) -> ChatApplicationService:
    return ChatApplicationService(lambda: runtime, RuntimeSettings(), memory=memory)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_chat_completion_returns_the_answer_when_memory_append_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A paid-for answer must not become a 503 because memory could not be written."""

    memory = _BrokenAppendMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity, kind="chat")
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    # The append lives in the memory module, so that is the logger to arm.
    with caplog.at_level(logging.ERROR, logger="harborrag.app.workflow_control.memory"):
        response = await service.complete(
            "Secret question text",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )

    assert response.ok is True
    assert response.data["message"] == {"role": "assistant", "content": "Hello"}
    assert response.data["memory_persisted"] is False
    # The question and the answer are written separately now, so a store that
    # rejects every append fails twice -- once per half of the turn.
    records = [r for r in caplog.records if "memory append failed" in r.getMessage()]
    assert len(records) == 2
    assert records[0].levelno == logging.ERROR
    assert "session-1" in records[0].getMessage()
    assert "Secret question text" not in caplog.text


@pytest.mark.asyncio
async def test_chat_completion_reports_memory_persisted_on_success() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )

    assert response.ok is True
    assert response.data["memory_persisted"] is True


@pytest.mark.asyncio
async def test_chat_completion_rejects_an_agent_session() -> None:
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "agent-session", "reader-1"), kind="agent"
    )
    chat = FakeChatFacade()
    service = _chat_service(FakeRuntime(chat), memory)

    with pytest.raises(HarborNotFoundError):
        await service.complete(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="agent-session"),
        )
    assert chat.requests == []


@pytest.mark.asyncio
async def test_concurrent_completions_on_one_session_are_serialized() -> None:
    """``recent -> append`` must not interleave: the second call sees the first turn."""

    release = asyncio.Event()
    entered: list[str] = []

    class _SlowChat(FakeChatFacade):
        async def complete(self, request, *, prompt=None):
            entered.append(replayed(request)[-1])
            if len(entered) == 1:
                await release.wait()
            return await super().complete(request, prompt=prompt)

    chat = _SlowChat()
    memory = InMemoryConversationMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    service = _chat_service(FakeRuntime(chat), memory)
    options = ChatExecutionOptions(session_id="session-1")

    first = asyncio.create_task(
        service.complete("First", tenant_id="ACME", principal_id="reader-1", options=options)
    )
    second = asyncio.create_task(
        service.complete("Second", tenant_id="ACME", principal_id="reader-1", options=options)
    )
    await asyncio.sleep(0.05)
    assert entered == ["First"], "second completion must wait for the first to finish"
    release.set()
    results = await asyncio.gather(first, second)

    assert all(result.ok for result in results)
    assert entered == ["First", "Second"]
    assert replayed(chat.requests[1]) == [
        "First",
        "Hello",
        "Second",
    ]


@pytest.mark.asyncio
async def test_chat_completion_reports_memory_lost_when_only_the_question_fails() -> None:
    """A half-written turn is a lost turn; ``memory_persisted`` must not claim otherwise.

    The answer landing is not enough. Without the question ahead of it the
    stored history is an assistant message with no prompt, which the next turn
    replays as an orphan -- so the caller has to be told the turn was lost.
    """

    memory = _BrokenQuestionAppendMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity, kind="chat")
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )

    assert response.ok is True
    assert response.data["message"] == {"role": "assistant", "content": "Hello"}
    assert response.data["memory_persisted"] is False
    assert [message.role for message in await memory.recent_messages(identity, limit=10)] == [
        "assistant"
    ]


@pytest.mark.asyncio
async def test_chat_stream_warns_when_only_the_question_append_fails() -> None:
    """The streaming surface reports the same half-written turn as a warning."""

    memory = _BrokenQuestionAppendMemory()
    await memory.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1"),
        )
    ]

    assert events[-1] == {"kind": "warning", "warning": "conversation_memory_unavailable"}
