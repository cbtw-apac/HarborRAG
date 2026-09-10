"""Chat application-service tests for graph-anchored memory recall.

The order under test is: assemble the context (which produces the standalone
query), search for it, then re-rank recall on the graph nodes the search
reported. What matters is that the second step costs one *recall* and not a
second context build -- no second rewrite, no second summary write.
"""

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
from harborrag_app.workflow_control.chat.retrieval import MAX_ANCHORS, graph_anchor_ids
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
        options=ChatExecutionOptions(session_id="session-1"),
    )
    assert response.ok is True, response.error


def test_anchor_ids_are_read_out_of_the_graph_provenance_in_order() -> None:
    diagnostics = graph_diagnostics("node-a", "node-b", "node-a", "  ")

    assert graph_anchor_ids(diagnostics) == ("node-a", "node-b")


def test_anchor_ids_are_empty_for_a_payload_without_graph_observation() -> None:
    assert graph_anchor_ids({}) == ()
    assert graph_anchor_ids({"graph_documents": "not-a-list"}) == ()
    assert graph_anchor_ids({"graph_documents": [{"related_results": [{"nodes": [7]}]}]}) == ()


def test_anchor_ids_are_bounded() -> None:
    diagnostics = graph_diagnostics(*(f"node-{index}" for index in range(MAX_ANCHORS * 2)))

    assert len(graph_anchor_ids(diagnostics)) == MAX_ANCHORS


@pytest.mark.asyncio
async def test_recall_is_re_ranked_on_the_anchors_retrieval_reported() -> None:
    retrieval = FakeRetrievalFacade(diagnostics=graph_diagnostics("node-atlas", "node-other"))
    memory_facade = FakeMemoryFacade()
    service = await _service(retrieval, memory_facade, memories=_Memories(_memory()))

    await _complete(service)

    assert [anchors for _, anchors in memory_facade.anchored] == [("node-atlas", "node-other")]
    # One context build, so the rewrite and the rolling summary were paid once.
    assert len(memory_facade.requests) == 1


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
async def test_an_anchoring_failure_keeps_the_unanchored_context_and_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    retrieval = FakeRetrievalFacade(diagnostics=graph_diagnostics("node-atlas"))
    memory_facade = FakeMemoryFacade()
    memory_facade.anchor_failure = RuntimeError("recall backend gone")
    service = await _service(retrieval, memory_facade, memories=_Memories(_memory()))

    with caplog.at_level(logging.WARNING, logger="harborrag.app.workflow_control.chat"):
        await _complete(service)

    assert "Anchoring conversation memory recall failed" in caplog.text
    assert "session-1" in caplog.text


@pytest.mark.asyncio
async def test_retrieval_is_seeded_with_the_entities_recalled_memories_reference() -> None:
    retrieval = FakeRetrievalFacade()
    memory_facade = FakeMemoryFacade()
    service = await _service(
        retrieval,
        memory_facade,
        memories=_Memories(_memory(entity_ids=("node-atlas", "node-platform"))),
    )

    await _complete(service)

    assert len(retrieval.requests) == 1
    assert retrieval.requests[0].graph_seed_node_keys == ("node-atlas", "node-platform")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_retrieval_carries_no_seeds_without_recalled_entities() -> None:
    retrieval = FakeRetrievalFacade()
    service = await _service(retrieval, FakeMemoryFacade(), memories=_Memories(_memory(())))

    await _complete(service)

    assert retrieval.requests[0].graph_seed_node_keys == ()  # type: ignore[attr-defined]
