from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from context_test_fakes import (
    NOW,
    EmbedderFake,
    MemoryIndexFake,
    MemoryRepositoryFake,
    MessageStoreFake,
    chat_model,
    memory_row,
    row,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType
from harborrag_memory import MemoryContextBuilder, MemoryPolicy, MemoryRecall

pytestmark = [pytest.mark.unit]

QUERY = "who owns the ingest pipeline?"


def recall(
    memories: MemoryRepositoryFake,
    *,
    index: MemoryIndexFake | None = None,
    embedder: EmbedderFake | None = None,
    policy: MemoryPolicy | None = None,
) -> MemoryRecall:
    return MemoryRecall(
        policy=policy or MemoryPolicy(),
        memories=memories,
        index=index,
        embedder=embedder,
        clock=lambda: NOW,
    )


def seed(memories: MemoryRepositoryFake, *rows: Memory) -> tuple[Memory, ...]:
    for stored in rows:
        memories.rows[stored.memory_id] = stored
    return rows


def user_fact(content: str, *, importance: float = 0.5, **extra: object) -> Memory:
    return memory_row(
        content,
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        importance=importance,
        **extra,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_recall_falls_back_to_repository_search_without_an_index(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(memories, user_fact("Dana owns ingest", importance=0.9), user_fact("Ravi owns search"))

    recalled = await recall(memories).recall(owner, QUERY)

    assert [memory.content for memory in recalled] == ["Dana owns ingest", "Ravi owns search"]
    assert memories.queries[0].text == QUERY


@pytest.mark.asyncio
async def test_recall_prefers_the_index_and_orders_by_its_scores(
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    embedder: EmbedderFake,
    owner: MemoryOwner,
) -> None:
    dana, ravi = seed(
        memories,
        user_fact("Dana owns ingest", importance=0.9),
        user_fact("Ravi owns search"),
    )
    index.scores = {dana.memory_id: 0.1, ravi.memory_id: 0.9}

    recalled = await recall(memories, index=index, embedder=embedder).recall(owner, QUERY)

    assert [memory.content for memory in recalled] == ["Ravi owns search", "Dana owns ingest"]
    assert embedder.texts == [QUERY]
    assert memories.queries == []


@pytest.mark.asyncio
async def test_recall_falls_back_to_the_repository_when_the_index_raises(
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    embedder: EmbedderFake,
    owner: MemoryOwner,
) -> None:
    seed(memories, user_fact("Dana owns ingest"))
    index.fail_search = True

    recalled = await recall(memories, index=index, embedder=embedder).recall(owner, QUERY)

    assert [memory.content for memory in recalled] == ["Dana owns ingest"]
    assert memories.queries[0].text == QUERY


@pytest.mark.asyncio
async def test_recall_yields_nothing_when_the_repository_raises(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(memories, user_fact("Dana owns ingest"))
    memories.fail_search = True

    assert await recall(memories).recall(owner, QUERY) == ()


@pytest.mark.asyncio
async def test_recall_excludes_an_invalidated_memory(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    live, stale = seed(memories, user_fact("Dana owns ingest"), user_fact("Ravi owns ingest"))
    memories.rows[stale.memory_id] = replace(
        stale,
        valid_from=NOW - timedelta(hours=48),
        invalid_at=NOW - timedelta(hours=1),
        superseded_by=live.memory_id,
    )

    recalled = await recall(memories).recall(owner, QUERY)

    assert [memory.content for memory in recalled] == ["Dana owns ingest"]


@pytest.mark.asyncio
async def test_recall_visits_scopes_in_policy_order_without_widening_the_owner(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    await recall(memories).recall(owner, QUERY)

    assert [query.scopes for query in memories.queries] == [
        (MemoryScope.USER,),
        (MemoryScope.PROJECT,),
        (MemoryScope.SESSION,),
        (MemoryScope.TENANT,),
    ]
    assert [query.owner for query in memories.queries] == [
        MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        MemoryOwner(tenant_id="tenant-1", project_id="handbook"),
        MemoryOwner(tenant_id="tenant-1", user_id="user-1", session_id="s-1"),
        MemoryOwner(tenant_id="tenant-1"),
    ]
    assert all(query.owner.run_id is None for query in memories.queries)


@pytest.mark.asyncio
async def test_recall_skips_a_scope_the_owner_cannot_address(
    memories: MemoryRepositoryFake,
) -> None:
    projectless = MemoryOwner(tenant_id="tenant-1", principal_id="principal-1", session_id="s-1")

    await recall(memories).recall(projectless, QUERY)

    assert MemoryScope.PROJECT not in [
        scope for query in memories.queries for scope in query.scopes
    ]


@pytest.mark.asyncio
async def test_recall_is_skipped_for_a_blank_query_or_zero_top_k(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(memories, user_fact("Dana owns ingest"))

    assert await recall(memories).recall(owner, "   ") == ()
    assert await recall(memories, policy=MemoryPolicy.disabled()).recall(owner, QUERY) == ()
    assert memories.queries == []


@pytest.mark.asyncio
async def test_recall_honours_top_k_and_the_block_budget(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(
        memories,
        *(user_fact(f"fact {index} " + "x" * 50, importance=index / 10) for index in range(5)),
    )

    capped = await recall(memories, policy=MemoryPolicy(recall_top_k=3)).recall(owner, QUERY)
    budgeted = await recall(
        memories,
        policy=MemoryPolicy(recall_top_k=3, recent_max_tokens=100, block_budget_fraction=0.2),
    ).recall(owner, QUERY)

    assert len(capped) == 3
    assert len(budgeted) == 1


@pytest.mark.asyncio
async def test_recall_ignores_the_rolling_summary(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(
        memories,
        memory_row(
            "the session so far",
            scope=MemoryScope.SESSION,
            memory_type=MemoryType.SUMMARY,
            owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1", session_id="s-1"),
        ),
    )

    assert await recall(memories).recall(owner, QUERY) == ()


@pytest.mark.asyncio
async def test_builder_recalls_against_the_rewritten_standalone_query(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "Who maintains ingest?", token_count=10)])
    seed(memories, user_fact("Dana owns ingest"))
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500),
        messages=store,
        memories=memories,
        model=chat_model("Who owns the ingest pipeline?"),
        clock=lambda: NOW,
    )

    context = await builder.build(owner, "and who owns it?")

    assert context.rewritten is True
    assert [memory.content for memory in context.recalled] == ["Dana owns ingest"]
    assert [query.text for query in memories.queries if query.text] == [
        "Who owns the ingest pipeline?"
    ] * 4


@pytest.mark.asyncio
async def test_builder_still_returns_a_context_when_recall_fails(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "Who maintains ingest?", token_count=10)])
    memories.fail_search = True
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500),
        messages=store,
        memories=memories,
        model=chat_model("Who owns the ingest pipeline?"),
        clock=lambda: NOW,
    )

    context = await builder.build(owner, "and who owns it?")

    assert context.recalled == ()
    assert context.standalone_query == "Who owns the ingest pipeline?"


@pytest.mark.asyncio
async def test_builder_recalls_through_the_index_when_one_is_wired(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    index: MemoryIndexFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "Who maintains ingest?", token_count=10)])
    (dana,) = seed(memories, user_fact("Dana owns ingest"))
    index.scores = {dana.memory_id: 0.7}
    embedder = EmbedderFake()
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500, query_rewrite=False),
        messages=store,
        memories=memories,
        model=None,
        index=index,
        embedder=embedder,
        clock=lambda: NOW,
    )

    context = await builder.build(owner, "who owns ingest?")

    assert [memory.content for memory in context.recalled] == ["Dana owns ingest"]
    assert embedder.texts == ["who owns ingest?"]
