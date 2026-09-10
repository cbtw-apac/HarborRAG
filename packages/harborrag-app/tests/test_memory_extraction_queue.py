"""The bounded extraction queue: best effort, never in the caller's way."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import pytest
from chat_service_fixtures import FakeChatFacade, FakeMemoryFacade, FakeRuntime

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_app.workflow_control.memory import MemoryIdentity
from harborrag_app.workflow_control.memory.extraction import (
    MemoryExtractionQueue,
    dropped_extractions,
    reset_dropped_extractions,
    submit_exchange,
)
from harborrag_core.base import utc_now
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import InMemoryConversationMemory, MemoryContextRequest


class FakeMemories:
    """The narrowest ``MemoryRepository`` the queue needs to consider itself wired."""

    def __init__(self) -> None:
        self.saved: list[Memory] = []

    async def save(self, memory: Memory) -> None:
        self.saved.append(memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        del caller, memory_id
        return None

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        del query
        return ()

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id


def _message(role: str, content: str) -> ConversationMessage:
    return ConversationMessage(
        message_id=f"msg-{role}",
        role=role,  # type: ignore[arg-type]
        content=content,
        created_at=utc_now(),
    )


def _request(session_id: str = "session-1") -> MemoryContextRequest:
    return MemoryContextRequest(
        tenant_id="ACME",
        principal_id="reader-1",
        user_id="user-1",
        session_id=session_id,
        question="what did we decide?",
    )


def _queue(
    runtime: FakeRuntime,
    *,
    max_queue: int = 8,
    workers: int = 2,
    item_timeout_seconds: float = 5.0,
) -> MemoryExtractionQueue:
    return MemoryExtractionQueue(
        runtime_provider=lambda: runtime,  # type: ignore[arg-type]
        memories=FakeMemories(),
        max_queue=max_queue,
        workers=workers,
        item_timeout_seconds=item_timeout_seconds,
    )


@pytest.fixture(autouse=True)
def _clean_counter() -> None:
    reset_dropped_extractions()


@pytest.mark.asyncio
async def test_drain_runs_every_queued_exchange_and_stops_the_workers() -> None:
    memory = FakeMemoryFacade()
    queue = _queue(FakeRuntime(FakeChatFacade(), memory=memory))
    await queue.start()

    assert queue.submit(_request("session-1"), (_message("user", "q"),)) is True
    assert queue.submit(_request("session-2"), (_message("user", "q"),)) is True
    await queue.drain(timeout=5.0)

    assert [request.session_id for request, _ in memory.extractions] == [
        "session-1",
        "session-2",
    ]


@pytest.mark.asyncio
async def test_drain_is_idempotent_and_safe_when_never_started() -> None:
    queue = _queue(FakeRuntime(FakeChatFacade()))

    await queue.drain(timeout=0.1)
    await queue.drain(timeout=0.1)

    assert queue.submit(_request(), (_message("user", "q"),)) is False


@pytest.mark.asyncio
async def test_submitting_before_the_pool_starts_is_refused_not_hoarded() -> None:
    """The CLI never starts the pool, so nothing may pile up behind it."""

    queue = _queue(FakeRuntime(FakeChatFacade()))

    assert queue.submit(_request(), (_message("user", "q"),)) is False
    assert dropped_extractions() == 0


@pytest.mark.asyncio
async def test_a_drained_pool_can_be_started_again() -> None:
    memory = FakeMemoryFacade()
    queue = _queue(FakeRuntime(FakeChatFacade(), memory=memory), workers=1)
    await queue.start()
    await queue.drain(timeout=5.0)

    await queue.start()
    assert queue.submit(_request("session-2"), (_message("user", "q"),)) is True
    await queue.drain(timeout=5.0)

    assert [request.session_id for request, _ in memory.extractions] == ["session-2"]


@pytest.mark.asyncio
async def test_a_full_queue_drops_with_a_warning_instead_of_raising(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()

    class _Blocking(FakeMemoryFacade):
        async def extract(self, request: object, **kwargs: object) -> tuple[object, ...]:
            del request, kwargs
            await gate.wait()
            return ()

    queue = _queue(FakeRuntime(FakeChatFacade(), memory=_Blocking()), max_queue=1, workers=1)
    logger = "harborrag.app.workflow_control.memory.extraction"
    await queue.start()

    with caplog.at_level(logging.WARNING, logger=logger):
        assert queue.submit(_request(), (_message("user", "secret question"),)) is True
        await asyncio.sleep(0.05)
        assert queue.submit(_request(), (_message("user", "secret question"),)) is True
        assert queue.submit(_request(), (_message("user", "secret question"),)) is False

    assert dropped_extractions() == 1
    assert "Memory extraction queue is full" in caplog.text
    assert "secret question" not in caplog.text
    gate.set()
    await queue.drain(timeout=5.0)


@pytest.mark.asyncio
async def test_a_failing_exchange_does_not_kill_the_worker_pool(
    caplog: pytest.LogCaptureFixture,
) -> None:
    memory = FakeMemoryFacade()
    memory.extract_failure = RuntimeError("model gone")
    queue = _queue(FakeRuntime(FakeChatFacade(), memory=memory), workers=1)
    logger = "harborrag.app.workflow_control.memory.extraction"
    await queue.start()

    with caplog.at_level(logging.WARNING, logger=logger):
        assert queue.submit(_request("session-1"), (_message("user", "q"),)) is True
        await asyncio.sleep(0)
        memory.extract_failure = None
        assert queue.submit(_request("session-2"), (_message("user", "q"),)) is True
        await queue.drain(timeout=5.0)

    assert "Memory extraction failed" in caplog.text
    assert [request.session_id for request, _ in memory.extractions] == ["session-2"]


@pytest.mark.asyncio
async def test_a_slow_exchange_is_bounded_so_drain_still_completes() -> None:
    class _Hanging(FakeMemoryFacade):
        async def extract(self, request: object, **kwargs: object) -> tuple[object, ...]:
            del request, kwargs
            await asyncio.sleep(60)
            raise AssertionError("the item timeout must fire first")

    queue = _queue(
        FakeRuntime(FakeChatFacade(), memory=_Hanging()),
        workers=1,
        item_timeout_seconds=0.05,
    )
    await queue.start()
    queue.submit(_request(), (_message("user", "q"),))

    await queue.drain(timeout=5.0)


@pytest.mark.asyncio
async def test_submit_exchange_skips_an_unpersisted_turn() -> None:
    memory = FakeMemoryFacade()
    queue = _queue(FakeRuntime(FakeChatFacade(), memory=memory))
    identity = MemoryIdentity.build(
        tenant_id="ACME",
        principal_id="reader-1",
        session_id="session-1",
    )
    await queue.start()

    submit_exchange(queue, identity, "q", None)
    submit_exchange(None, identity, "q", (_message("user", "q"),))
    await queue.drain(timeout=1.0)

    assert memory.extractions == []


def _chat_service(
    runtime: FakeRuntime,
    memory: InMemoryConversationMemory,
    queue: MemoryExtractionQueue | None,
) -> ChatApplicationService:
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        RuntimeSettings(),
        memory=memory,
        memories=FakeMemories(),
        extraction=queue,
    )


class _BrokenAppendMemory(InMemoryConversationMemory):
    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        del identity, messages
        raise RuntimeError("database gone")


@pytest.mark.asyncio
async def test_a_persisted_chat_turn_is_submitted_with_its_own_messages() -> None:
    facade = FakeMemoryFacade()
    runtime = FakeRuntime(FakeChatFacade(), memory=facade)
    history = InMemoryConversationMemory()
    await history.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    queue = _queue(runtime, workers=1)
    await queue.start()
    service = _chat_service(runtime, history, queue)

    response = await service.complete(
        "Where is the runbook?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )
    await queue.drain(timeout=5.0)

    assert response.data["memory_persisted"] is True
    request, messages = facade.extractions[0]
    assert request.session_id == "session-1"
    assert request.question == "Where is the runbook?"
    persisted = await history.recent_messages(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), limit=2
    )
    assert [message.message_id for message in messages] == [  # type: ignore[attr-defined]
        message.message_id for message in persisted
    ]


@pytest.mark.asyncio
async def test_an_unpersisted_chat_turn_is_never_submitted() -> None:
    facade = FakeMemoryFacade()
    runtime = FakeRuntime(FakeChatFacade(), memory=facade)
    history = _BrokenAppendMemory()
    await history.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    queue = _queue(runtime, workers=1)
    await queue.start()
    service = _chat_service(runtime, history, queue)

    response = await service.complete(
        "Where is the runbook?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1"),
    )
    await queue.drain(timeout=5.0)

    assert response.data["memory_persisted"] is False
    assert facade.extractions == []
