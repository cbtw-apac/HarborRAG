"""Chat application-service tests for memory persistence, session kind, and serialization."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import pytest

from harborrag_core.contracts.errors import HarborConnectionError
from harborrag_core.ports.conversation import ConversationMessage
from harborrag_memory.tiers.in_memory import InMemoryConversationMemory
from harborrag_runtime.application.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity
from harborrag_runtime.testing.chat_fakes import FakeChatFacade, FakeRuntime

pytestmark = pytest.mark.unit


class _BrokenAppendMemory(InMemoryConversationMemory):
    async def append_messages(
        self, identity: ConversationIdentity, messages: Sequence[ConversationMessage]
    ) -> None:
        del identity, messages
        raise RuntimeError("database gone: prompt text must not be logged")


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
