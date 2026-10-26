"""Tests for ``MemoryFacade.recall_anchored``: recall repeats, nothing else does."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from harborrag_core.base import utc_now
from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.memory import (
    MemoryContext,
    MemoryContextRequest,
    MemoryPolicy,
    RuntimeMemoryContextService,
)
from harborrag_runtime.sdk import HarborRAG
from harborrag_runtime.sdk.configuration import HarborRAGConfig

OWNER = MemoryOwner(
    tenant_id="tenant-1",
    project_id="project-1",
    principal_id="principal-1",
    user_id="user-1",
    session_id="session-1",
)


def _memory(memory_id: str, *, importance: float, entity_ids: tuple[str, ...] = ()) -> Memory:
    now = utc_now()
    return Memory(
        memory_id=memory_id,
        scope=MemoryScope.USER,
        memory_type=MemoryType.FACT,
        owner=OWNER,
        content=f"content of {memory_id}",
        importance=importance,
        entity_ids=entity_ids,
        created_at=now,
        updated_at=now,
    )


PLAIN = _memory("mem-plain", importance=0.6)
ANCHORED = _memory("mem-anchored", importance=0.4, entity_ids=("node-atlas",))


class _Memories:
    """Returns every row whose scope the query asked for; counts its searches."""

    def __init__(self, *rows: Memory, failure: Exception | None = None) -> None:
        self.rows = rows
        self.failure = failure
        self.searches = 0

    async def save(self, memory: Memory) -> None:
        del memory

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        del caller
        return next((row for row in self.rows if row.memory_id == memory_id), None)

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]:
        self.searches += 1
        if self.failure is not None:
            raise self.failure
        return tuple(row for row in self.rows if row.scope in query.scopes)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        del caller, memory_id


async def _no_embedder(settings: RuntimeSettings) -> None:
    del settings
    return None


def _harbor(
    policy: MemoryPolicy | None = None,
    *,
    embedder_builder: object = _no_embedder,
) -> HarborRAG:
    harbor = HarborRAG(HarborRAGConfig())
    service = RuntimeMemoryContextService(
        harbor.config.runtime,
        model_builder=_no_embedder,
        embedder_builder=embedder_builder,  # type: ignore[arg-type]
    )
    if policy is not None:
        service._policy = policy
    harbor._memory_runtime = service
    return harbor


def _request() -> MemoryContextRequest:
    return MemoryContextRequest(
        tenant_id="tenant-1",
        principal_id="principal-1",
        user_id="user-1",
        session_id="session-1",
        question="and who owns it?",
        project_id="project-1",
    )


def _context(*recalled: Memory) -> MemoryContext:
    return MemoryContext(
        messages=(),
        summary="rolling summary",
        recalled=recalled,
        standalone_query="who owns the Atlas migration?",
        rewritten=True,
        summary_written=True,
    )


@pytest.mark.asyncio
async def test_anchored_recall_re_ranks_without_rewriting_or_summarising_again() -> None:
    harbor = _harbor()
    memories = _Memories(PLAIN, ANCHORED)
    context = _context(PLAIN, ANCHORED)

    anchored = await harbor.memory.recall_anchored(
        _request(),
        context,
        memories=memories,  # type: ignore[arg-type]
        anchor_entity_ids=("node-atlas", "node-atlas", " "),
    )

    # The anchored memory outranks the more important unanchored one.
    assert [item.memory_id for item in anchored.recalled] == ["mem-anchored", "mem-plain"]
    assert anchored.anchor_entity_ids == ("node-atlas",)
    # Nothing but recall repeated: the query is reused verbatim and the rolling
    # summary's write flag is carried through untouched.
    assert anchored.standalone_query == context.standalone_query
    assert anchored.rewritten is True
    assert anchored.summary_written is True
    assert anchored.messages == context.messages
    assert anchored.summary == context.summary


@pytest.mark.asyncio
async def test_anchored_recall_is_skipped_without_anchors() -> None:
    harbor = _harbor()
    memories = _Memories(PLAIN)
    context = _context(PLAIN)

    result = await harbor.memory.recall_anchored(
        _request(),
        context,
        memories=memories,  # type: ignore[arg-type]
    )

    assert result is context
    assert memories.searches == 0


@pytest.mark.asyncio
async def test_anchored_recall_is_skipped_when_the_first_recall_found_nothing() -> None:
    harbor = _harbor()
    memories = _Memories(PLAIN)

    result = await harbor.memory.recall_anchored(
        _request(),
        _context(),
        memories=memories,  # type: ignore[arg-type]
        anchor_entity_ids=("node-atlas",),
    )

    assert result.recalled == ()
    assert result.anchor_entity_ids == ()
    assert memories.searches == 0


@pytest.mark.asyncio
async def test_anchored_recall_is_skipped_when_recall_is_switched_off() -> None:
    harbor = _harbor(replace(MemoryPolicy(), recall_top_k=0))
    memories = _Memories(PLAIN)

    result = await harbor.memory.recall_anchored(
        _request(),
        _context(PLAIN),
        memories=memories,  # type: ignore[arg-type]
        anchor_entity_ids=("node-atlas",),
    )

    assert result.recalled == (PLAIN,)
    assert memories.searches == 0


@pytest.mark.asyncio
async def test_anchored_recall_is_skipped_when_the_policy_is_disabled() -> None:
    harbor = _harbor(MemoryPolicy.disabled())
    memories = _Memories(PLAIN)

    result = await harbor.memory.recall_anchored(
        _request(),
        _context(PLAIN),
        memories=memories,  # type: ignore[arg-type]
        anchor_entity_ids=("node-atlas",),
    )

    assert result.recalled == (PLAIN,)
    assert memories.searches == 0


@pytest.mark.asyncio
async def test_a_broken_store_keeps_the_memories_the_prompt_already_had(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Recall degrades to ``()`` on a store failure, so re-ranking must not adopt it."""

    harbor = _harbor()
    context = _context(PLAIN)
    memories = _Memories(PLAIN, failure=RuntimeError("store gone"))

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        result = await harbor.memory.recall_anchored(
            _request(),
            context,
            memories=memories,  # type: ignore[arg-type]
            anchor_entity_ids=("node-atlas",),
        )

    assert result.recalled == (PLAIN,)
    assert result.anchor_entity_ids == ()
    assert "Anchored memory recall found nothing" in caplog.text


@pytest.mark.asyncio
async def test_a_raising_collaborator_keeps_the_unanchored_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _broken_embedder(settings: RuntimeSettings) -> None:
        del settings
        raise RuntimeError("embed catalog unreadable")

    harbor = _harbor(embedder_builder=_broken_embedder)
    context = _context(PLAIN)

    with caplog.at_level(logging.WARNING, logger="harborrag.runtime.memory"):
        result = await harbor.memory.recall_anchored(
            _request(),
            context,
            memories=_Memories(PLAIN),  # type: ignore[arg-type]
            anchor_entity_ids=("node-atlas",),
        )

    assert result is context
    assert "Anchored memory recall failed" in caplog.text


@pytest.mark.asyncio
async def test_anchored_recall_carries_the_type_hint_the_first_pass_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hint travels on the context, so the re-ranked pass keeps it."""

    seen: dict[str, object] = {}

    class _Recorder:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def recall(
            self,
            owner: MemoryOwner,
            query: str,
            *,
            anchor_entity_ids: object = (),
            wanted_types: object = (),
        ) -> tuple[Memory, ...]:
            seen.update(
                owner=owner,
                query=query,
                anchor_entity_ids=anchor_entity_ids,
                wanted_types=wanted_types,
            )
            return (ANCHORED,)

    monkeypatch.setattr("harborrag_memory.MemoryRecall", _Recorder)
    harbor = _harbor()
    context = replace(_context(PLAIN), wanted_types=(MemoryType.DECISION, MemoryType.PREFERENCE))

    result = await harbor.memory.recall_anchored(
        _request(),
        context,
        memories=_Memories(PLAIN),  # type: ignore[arg-type]
        anchor_entity_ids=("node-atlas",),
    )

    assert seen["wanted_types"] == (MemoryType.DECISION, MemoryType.PREFERENCE)
    assert seen["anchor_entity_ids"] == ("node-atlas",)
    assert seen["query"] == context.standalone_query
    assert result.recalled == (ANCHORED,)
    assert result.wanted_types == context.wanted_types
