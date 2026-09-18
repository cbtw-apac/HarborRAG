"""Fixed conversation context never reintroduces long-term graph memory."""

from __future__ import annotations

import logging

import pytest
from chat_service_fixtures import (
    FakeChatFacade,
    FakeMemoryFacade,
    FakeRetrievalFacade,
    FakeRuntime,
    graph_diagnostics,
)

from harborrag_app.workflow_control.chat import ChatApplicationService, ChatExecutionOptions
from harborrag_core.base import utc_now
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryQuery, MemoryScope, MemoryType
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import ConversationIdentity, InMemoryConversationMemory


class _Memories:
    """The narrowest ``MemoryRepository`` these tests need: one recallable row."""

    def __init__(self, *rows: Memory) -> None:
        self.rows = rows

    async def save(self, memory: Memory) -> None:
        del memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        del caller
        return next((row for row in self.rows if row.memory_id == memory_id), None)

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        return tuple(row for row in self.rows if row.scope in (query.scopes or (row.scope,)))

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id


def _memory(entity_ids: tuple[str, ...] = ("node-atlas",)) -> Memory:
    return Memory(
        memory_id="mem-1",
        scope=MemoryScope.USER,
        memory_type=MemoryType.FACT,
        owner=MemoryOwner(tenant_id="ACME", principal_id="reader-1", user_id="reader-1"),
        content="the Atlas migration is owned by platform",
        entity_ids=entity_ids,
        valid_from=utc_now(),
    )


async def _service(
    retrieval: FakeRetrievalFacade,
    memory_facade: FakeMemoryFacade,
    *,
    memories: _Memories | None = None,
) -> ChatApplicationService:
    conversations = InMemoryConversationMemory()
    await conversations.create(
        ConversationIdentity("ACME", "reader-1", "session-1", "reader-1"), kind="chat"
    )
    runtime = FakeRuntime(FakeChatFacade(), retrieval, memory=memory_facade)
    return ChatApplicationService(
        lambda: runtime,  # type: ignore[arg-type,return-value]
        RuntimeSettings(),
        memory=conversations,
        memories=memories,  # type: ignore[arg-type]
    )


async def _complete(service: ChatApplicationService) -> None:
    response = await service.complete(
        "who owns it?",
        tenant_id="ACME",
        principal_id="reader-1",
        options=ChatExecutionOptions(session_id="session-1", user_id="reader-1"),
    )
    assert response.ok is True, response.error


@pytest.mark.asyncio
async def test_graph_diagnostics_do_not_trigger_memory_recall() -> None:
    retrieval = FakeRetrievalFacade(diagnostics=graph_diagnostics("node-atlas", "node-other"))
    memory_facade = FakeMemoryFacade()
    service = await _service(retrieval, memory_facade, memories=_Memories(_memory()))

    await _complete(service)

    assert memory_facade.anchored == []
    assert memory_facade.requests == []


@pytest.mark.asyncio
async def test_anchoring_is_skipped_when_retrieval_reported_no_graph_nodes() -> None:
    memory_facade = FakeMemoryFacade()
    service = await _service(FakeRetrievalFacade(), memory_facade, memories=_Memories(_memory()))

    await _complete(service)

    assert memory_facade.anchored == []


@pytest.mark.asyncio
async def test_anchoring_is_skipped_when_no_memory_store_is_wired() -> None:
    retrieval = FakeRetrievalFacade(diagnostics=graph_diagnostics("node-atlas"))
    memory_facade = FakeMemoryFacade()
    service = await _service(retrieval, memory_facade)

    await _complete(service)

    assert memory_facade.anchored == []


@pytest.mark.asyncio
async def test_an_unavailable_anchoring_service_does_not_affect_public_chat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retrieval = FakeRetrievalFacade(diagnostics=graph_diagnostics("node-atlas"))
    memory_facade = FakeMemoryFacade()
    memory_facade.anchor_failure = RuntimeError("recall backend gone")
    service = await _service(retrieval, memory_facade, memories=_Memories(_memory()))

    with caplog.at_level(logging.WARNING, logger="harborrag.app.workflow_control.chat"):
        await _complete(service)

    assert memory_facade.anchored == []
    assert "Anchoring conversation memory recall failed" not in caplog.text


@pytest.mark.asyncio
async def test_stored_long_term_entities_do_not_seed_public_retrieval() -> None:
    retrieval = FakeRetrievalFacade()
    memory_facade = FakeMemoryFacade()
    service = await _service(
        retrieval,
        memory_facade,
        memories=_Memories(_memory(entity_ids=("node-atlas", "node-platform"))),
    )

    await _complete(service)

    assert len(retrieval.requests) == 1
    assert retrieval.requests[0].graph_seed_node_keys == ()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_retrieval_carries_no_seeds_without_recalled_entities() -> None:
    retrieval = FakeRetrievalFacade()
    service = await _service(retrieval, FakeMemoryFacade(), memories=_Memories(_memory(())))

    await _complete(service)

    assert retrieval.requests[0].graph_seed_node_keys == ()  # type: ignore[attr-defined]
