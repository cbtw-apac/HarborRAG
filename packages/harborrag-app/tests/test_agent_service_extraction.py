"""Public agent runs preserve history without automatic long-term extraction."""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from chat_service_fixtures import FakeMemoryFacade, fake_runtime_config
from test_agent_service import _Chat
from test_memory_extraction_queue import FakeMemories

from harborrag_app.workflow_control.agent import AgentApplicationService, AgentExecutionOptions
from harborrag_app.workflow_control.memory.extraction import MemoryExtractionQueue
from harborrag_core.ports.conversation import ConversationIdentity, ConversationMessage
from harborrag_runtime.agent import InMemoryAgentRunRepository
from harborrag_runtime.memory import InMemoryConversationMemory

# The conversation belongs to the end user, not to the credential that acted.
IDENTITY = ConversationIdentity("ACME", "reader-1", "session-1", "alice")


class _CountingMemory(InMemoryConversationMemory):
    """Counts history reads so the extraction read-back is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    async def recent_messages(
        self,
        identity: ConversationIdentity,
        *,
        limit: int,
    ) -> tuple[ConversationMessage, ...]:
        self.reads += 1
        return await super().recent_messages(identity, limit=limit)


class _SilentlyBrokenMemory(InMemoryConversationMemory):
    """The engine treats a memory failure as advisory, so the run still succeeds."""

    async def append_messages(
        self,
        identity: ConversationIdentity,
        messages: Sequence[ConversationMessage],
    ) -> None:
        del identity, messages


def _service(
    memory: InMemoryConversationMemory,
    facade: FakeMemoryFacade,
    queue: MemoryExtractionQueue | None,
) -> AgentApplicationService:
    runtime = SimpleNamespace(chat=_Chat(), memory=facade, config=fake_runtime_config())
    return AgentApplicationService(
        lambda: runtime,  # type: ignore[arg-type]
        memory=memory,
        runs=InMemoryAgentRunRepository(),
        memories=FakeMemories(),  # type: ignore[arg-type]
        extraction=queue,
    )


def _queue(facade: FakeMemoryFacade) -> MemoryExtractionQueue:
    runtime = SimpleNamespace(memory=facade, config=fake_runtime_config())
    return MemoryExtractionQueue(
        runtime_provider=lambda: runtime,  # type: ignore[arg-type]
        memories=FakeMemories(),  # type: ignore[arg-type]
        workers=1,
        item_timeout_seconds=5.0,
    )


@pytest.mark.asyncio
async def test_a_persisted_run_does_not_submit_automatic_extraction() -> None:
    facade = FakeMemoryFacade()
    memory = InMemoryConversationMemory()
    await memory.create(IDENTITY, kind="agent")
    queue = _queue(facade)
    await queue.start()

    response = await _service(memory, facade, queue).complete(
        "who owns the runbook?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1", user_id="alice"),
    )
    await queue.drain(timeout=5.0)

    assert response.ok is True
    persisted = await memory.recent_messages(IDENTITY, limit=2)
    assert facade.extractions == []
    assert len(persisted) == 2
    assert all(message.run_id == response.data["run_id"] for message in persisted)


@pytest.mark.asyncio
async def test_a_run_whose_exchange_was_not_written_is_never_submitted() -> None:
    facade = FakeMemoryFacade()
    memory = _SilentlyBrokenMemory()
    await memory.create(IDENTITY, kind="agent")
    queue = _queue(facade)
    await queue.start()

    response = await _service(memory, facade, queue).complete(
        "who owns the runbook?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1", user_id="alice"),
    )
    await queue.drain(timeout=5.0)

    assert response.ok is True
    assert facade.extractions == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wired", "expected_reads"),
    [(False, 1), (True, 1)],
    ids=["no-queue", "queue"],
)
async def test_extraction_configuration_does_not_add_a_history_read(
    wired: bool,
    expected_reads: int,
) -> None:
    """The only recent_messages read verifies the exchange for its title."""

    facade = FakeMemoryFacade()
    memory = _CountingMemory()
    await memory.create(IDENTITY, kind="agent")
    queue = _queue(facade) if wired else None
    if queue is not None:
        await queue.start()

    await _service(memory, facade, queue).complete(
        "who owns the runbook?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=AgentExecutionOptions(session_id="session-1", user_id="alice"),
    )
    if queue is not None:
        await queue.drain(timeout=5.0)

    assert memory.reads == expected_reads
