"""Chat application-service tests for memory persistence, session kind, and serialization."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest
from chat_service_fixtures import FakeChatFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.contracts.errors import HarborConnectionError
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
async def test_an_exchange_the_store_refuses_fails_the_turn_without_logging_the_question(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The completion id must be in history, so an unstored answer is not returned."""

    memory = _BrokenAppendMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity)
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(HarborConnectionError, match="could not be stored"),
    ):
        await service.complete(
            "Secret question text",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
        )

    assert "Secret question text" not in caplog.text


@pytest.mark.asyncio
async def test_an_answered_turn_is_stored_once_under_the_completion_id() -> None:
    memory = InMemoryConversationMemory()
    identity = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
    await memory.create(identity)
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    response = await service.complete(
        "Hello",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1", trace_id="a" * 32),
    )

    question, answer = await memory.recent_messages(identity, limit=10)
    assert response.ok is True
    assert (question.role, question.content, question.mode) == ("user", "Hello", None)
    assert answer.message_id == response.data["completion_id"]
    assert (answer.content, answer.mode, answer.trace_id) == ("Hello", "classic", "a" * 32)


@pytest.mark.asyncio
async def test_chat_stream_warns_when_only_the_question_append_fails() -> None:
    """The streaming surface reports the same half-written turn as a warning."""

    memory = _BrokenQuestionAppendMemory()
    await memory.create(ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"))
    service = _chat_service(FakeRuntime(FakeChatFacade()), memory)

    events = [
        event
        async for event in service.stream(
            "Hello",
            tenant_id="ACME",
            principal_id="reader-1",
            options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
        )
    ]

    assert events[-2] == {"kind": "warning", "warning": "conversation_memory_unavailable"}
    assert events[-1]["result"]["memory_persisted"] is False
