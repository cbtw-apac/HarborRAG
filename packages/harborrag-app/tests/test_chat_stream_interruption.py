"""An interrupted chat stream must never discard the turn it was serving.

Every exit a stream can take -- a provider raising mid-flight, the transport
deadline cancelling it, the client hanging up -- used to leave no trace at
all: the question was written together with the answer, and neither was
written unless the stream reached ``completed``. These tests pin the split:
the question lands before the model is called, and whatever answer text
reached the caller lands on the way out, marked as unfinished.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from chat_service_fixtures import (
    FakeChatFacade,
    FakeRetrievalFacade,
    FakeRuntime,
    FakeUsageRepository,
    replayed,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.models.chat import HarborChatStreamChunk, HarborChatUsage
from harborrag_core.models.chat.enums import StreamEventType
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory

IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "reader-1")
OPTIONS = ChatExecutionOptions(session_id="session-1")


def _chunk(
    event: StreamEventType,
    *,
    text: str | None = None,
    usage: HarborChatUsage | None = None,
) -> HarborChatStreamChunk:
    return HarborChatStreamChunk(
        event=event,
        logical_model="primary",
        provider="mock",
        provider_model="mock-chat",
        deployment="internal-deployment",
        text_delta=text,
        usage=usage,
    )


class _PartialChat(FakeChatFacade):
    """Deliver one delta plus reported usage, then fail without completing."""

    async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
        yield _chunk(StreamEventType.TEXT_DELTA, text="Half an ")
        yield _chunk(
            StreamEventType.USAGE,
            usage=HarborChatUsage(prompt_tokens=9, completion_tokens=4, total_tokens=13),
        )
        raise RuntimeError("secret provider response")


class _HangingChat(FakeChatFacade):
    """Deliver one delta and then never finish, so a deadline can cut in."""

    async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
        yield _chunk(StreamEventType.TEXT_DELTA, text="Half an ")
        yield _chunk(
            StreamEventType.USAGE,
            usage=HarborChatUsage(prompt_tokens=9, completion_tokens=4, total_tokens=13),
        )
        await asyncio.Event().wait()
        yield _chunk(StreamEventType.COMPLETED)


def _service(
    memory: InMemoryConversationMemory,
    chat: FakeChatFacade,
    *,
    usage: FakeUsageRepository | None = None,
) -> ChatApplicationService:
    runtime = FakeRuntime(chat, FakeRetrievalFacade())
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
        usage=usage,  # type: ignore[arg-type]
    )


async def _session() -> InMemoryConversationMemory:
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="chat")
    return memory


@pytest.mark.asyncio
async def test_a_stream_that_raises_still_persists_the_question_and_partial_answer() -> None:
    memory = await _session()
    usage = FakeUsageRepository()
    service = _service(memory, _PartialChat(), usage=usage)

    events = [
        event
        async for event in service.stream(
            "What is the release policy?",
            tenant_id="ACME",
            principal_id="reader-1",
            options=OPTIONS,
        )
    ]

    assert [event["kind"] for event in events][-1] == "error"
    assert "secret provider response" not in str(events)
    question, answer = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "What is the release policy?")
    assert (answer.role, answer.content) == ("assistant", "Half an ")
    assert answer.partial is True  # type: ignore[attr-defined]
    # Those four completion tokens were bought, so they are accounted for.
    assert [record.completion_tokens for record in usage.records] == [4]


@pytest.mark.asyncio
async def test_a_stream_cut_by_its_deadline_persists_what_it_delivered() -> None:
    """The transport wraps each ``__anext__`` in ``asyncio.timeout``; mirror that."""

    memory = await _session()
    service = _service(memory, _HangingChat())
    events = service.stream(
        "What is the release policy?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    delivered = [await anext(events) for _ in range(3)]
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await anext(events)
    await events.aclose()

    assert [event["kind"] for event in delivered] == ["citations", "chunk", "chunk"]
    question, answer = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "What is the release policy?")
    assert (answer.role, answer.content) == ("assistant", "Half an ")
    assert answer.partial is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_client_that_hangs_up_persists_what_it_delivered() -> None:
    memory = await _session()
    service = _service(memory, _HangingChat())
    events = service.stream(
        "What is the release policy?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=OPTIONS,
    )

    for _ in range(3):
        await anext(events)
    await events.aclose()

    question, answer = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "What is the release policy?")
    assert (answer.role, answer.content) == ("assistant", "Half an ")
    assert answer.partial is True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_completed_stream_is_unchanged_and_not_marked_partial() -> None:
    memory = await _session()
    service = _service(memory, FakeChatFacade())

    events = [
        event
        async for event in service.stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=OPTIONS
        )
    ]

    assert [event["kind"] for event in events] == [
        "citations",
        "chunk",
        "chunk",
        "cited_sources",
    ]
    question, answer = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "Hello")
    assert (answer.role, answer.content, answer.token_count) == ("assistant", "Hello", 1)
    assert answer.partial is False  # type: ignore[attr-defined]
    (turn,) = await memory.recent(IDENTITY, limit=2)
    assert (turn.user_content, turn.assistant_content) == ("Hello", "Hello")


@pytest.mark.asyncio
async def test_a_stream_that_delivered_nothing_writes_only_the_question() -> None:
    """A blank answer would pair with the question and replay as a finished turn."""

    class _SilentChat(FakeChatFacade):
        async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
            raise RuntimeError("secret provider response")
            yield  # pragma: no cover - unreachable, keeps this a generator

    memory = await _session()
    service = _service(memory, _SilentChat())

    events = [
        event
        async for event in service.stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=OPTIONS
        )
    ]

    assert [event["kind"] for event in events] == ["citations", "error"]
    (question,) = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "Hello")
    assert await memory.recent(IDENTITY, limit=2) == ()


@pytest.mark.asyncio
async def test_a_completed_but_empty_answer_does_not_claim_memory_is_broken() -> None:
    """No answer text means nothing to write, which is not a memory failure."""

    class _EmptyChat(FakeChatFacade):
        async def _events(self) -> AsyncIterator[HarborChatStreamChunk]:
            yield _chunk(
                StreamEventType.COMPLETED,
                usage=HarborChatUsage(prompt_tokens=7, completion_tokens=0, total_tokens=7),
            )

    memory = await _session()
    service = _service(memory, _EmptyChat())

    events = [
        event
        async for event in service.stream(
            "Hello", tenant_id="ACME", principal_id="reader-1", options=OPTIONS
        )
    ]

    assert [event["kind"] for event in events] == ["citations", "chunk"]
    (question,) = await memory.recent_messages(IDENTITY, limit=10)
    assert (question.role, question.content) == ("user", "Hello")


@pytest.mark.asyncio
async def test_the_recent_window_never_contains_the_question_being_answered() -> None:
    """Context first, then the question: a caller that appended first would
    replay its own question and defeat the first-turn no-rewrite rule."""

    memory = await _session()
    chat = FakeChatFacade()
    service = _service(memory, chat)

    for prompt in ("First question", "Second question"):
        _ = [
            event
            async for event in service.stream(
                prompt, tenant_id="ACME", principal_id="reader-1", options=OPTIONS
            )
        ]

    first, second = chat.requests
    assert replayed(first) == ["First question"]
    assert replayed(second) == [
        "First question",
        "Hello",
        "Second question",
    ]
