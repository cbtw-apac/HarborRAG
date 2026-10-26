"""Legacy ``summary:{session_id}`` rows are migrated lazily, with no migration (review #9)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from context_test_fakes import (
    NOW,
    MemoryRepositoryFake,
    MessageStoreFake,
    chat_model,
    memory_row,
    row,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType
from harborrag_memory import MemoryContextBuilder, MemoryPolicy, MemoryRetention
from harborrag_memory.context.summarizer import (
    LAST_COVERED_KEY,
    legacy_summary_memory_id,
    recall_owner,
    summary_memory_id,
)

pytestmark = [pytest.mark.unit]

POLICY = MemoryPolicy(
    recent_max_messages=10,
    recent_max_tokens=100,
    summary_keep_messages=2,
    query_rewrite=False,
    retention=MemoryRetention(session_days=90),
)


def legacy_row(owner: MemoryOwner, content: str, *, frontier: str | None = None) -> Memory:
    return memory_row(
        content,
        scope=MemoryScope.SESSION,
        owner=recall_owner(owner),
        memory_type=MemoryType.SUMMARY,
        memory_id=legacy_summary_memory_id(owner.session_id or ""),
        updated_at=NOW - timedelta(hours=1),
        metadata={LAST_COVERED_KEY: frontier} if frontier else {},
    )


def builder(
    store: MessageStoreFake, memories: MemoryRepositoryFake, *replies: str
) -> MemoryContextBuilder:
    return MemoryContextBuilder(
        policy=POLICY,
        messages=store,
        memories=memories,
        model=chat_model(*replies),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_a_legacy_summary_is_carried_forward_and_retired(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    first = row("user", "hello", token_count=5)
    await store.append_messages(identity, [first, row("assistant", "hi", token_count=5)])
    legacy = legacy_row(owner, "Dana owns ingestion.", frontier=first.message_id)
    memories.rows[legacy.memory_id] = legacy

    context = await builder(store, memories).build(owner, "what now?")

    assert context.summary == "Dana owns ingestion."
    assert legacy.memory_id not in memories.rows
    migrated = memories.rows[summary_memory_id(owner)]
    assert migrated.content == "Dana owns ingestion."
    assert migrated.metadata[LAST_COVERED_KEY] == first.message_id
    assert migrated.expires_at == NOW + timedelta(days=90)


@pytest.mark.asyncio
async def test_a_legacy_summary_beside_a_hashed_one_is_deleted_and_never_read(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    await store.append_messages(identity, [row("user", "hello", token_count=5)])
    legacy = legacy_row(owner, "stale legacy summary")
    current = memory_row(
        "current summary",
        scope=MemoryScope.SESSION,
        owner=recall_owner(owner),
        memory_type=MemoryType.SUMMARY,
        memory_id=summary_memory_id(owner),
        updated_at=NOW - timedelta(hours=2),
    )
    memories.rows[legacy.memory_id] = legacy
    memories.rows[current.memory_id] = current

    context = await builder(store, memories).build(owner, "what now?")

    assert context.summary == "current summary"
    assert legacy.memory_id not in memories.rows
    assert memories.rows[current.memory_id].content == "current summary"


@pytest.mark.asyncio
async def test_a_failed_retirement_keeps_the_turn_working(
    owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    store = MessageStoreFake()
    memories = MemoryRepositoryFake()
    await store.append_messages(identity, [row("user", "hello", token_count=5)])
    legacy = legacy_row(owner, "Dana owns ingestion.")
    memories.rows[legacy.memory_id] = legacy
    memories.fail_save = True

    context = await builder(store, memories).build(owner, "what now?")

    assert context.summary == "Dana owns ingestion."
    assert legacy.memory_id in memories.rows
