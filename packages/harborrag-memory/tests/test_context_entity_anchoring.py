"""Entity anchoring end to end: resolution at extraction, overlap at recall."""

from __future__ import annotations

from typing import Any

import pytest
from context_test_fakes import (
    NOW,
    EntityResolverFake,
    MemoryRepositoryFake,
    MessageStoreFake,
    memory_row,
    row,
    structured_model,
)

from harborrag_core.ports.conversation import (
    ConversationIdentity,
    ConversationMessage,
)
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope
from harborrag_memory import (
    MemoryContextBuilder,
    MemoryExtractor,
    MemoryPolicy,
    MemoryRecall,
    recalled_entity_ids,
)

pytestmark = [pytest.mark.unit]

MESSAGES: tuple[ConversationMessage, ...] = (
    row("user", "Dana runs the ingest pipeline now.", message_id="m-1"),
    row("assistant", "Noted, Dana owns ingest.", message_id="m-2"),
)
QUERY = "who owns the ingest pipeline?"


def fact(content: str, **overrides: Any) -> dict[str, Any]:
    return {"content": content, "scope": "user", "importance": 0.8, **overrides}


def extractor(
    memories: MemoryRepositoryFake,
    *payloads: Any,
    entities: EntityResolverFake | None = None,
    policy: MemoryPolicy | None = None,
) -> MemoryExtractor:
    return MemoryExtractor(
        policy=policy or MemoryPolicy(),
        memories=memories,
        model=structured_model(*payloads),
        entities=entities,
        clock=lambda: NOW,
    )


def user_fact(content: str, *, memory_id: str, entity_ids: tuple[str, ...]) -> Memory:
    return memory_row(
        content,
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        memory_id=memory_id,
        entity_ids=entity_ids,
    )


@pytest.mark.asyncio
async def test_extraction_stores_the_resolved_graph_ids(
    memories: MemoryRepositoryFake, resolver: EntityResolverFake, owner: MemoryOwner
) -> None:
    resolver.known = {"Dana": ("node-dana", 0.9), "ingest pipeline": ("node-ingest", 0.75)}

    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana", "ingest pipeline"])]},
        entities=resolver,
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ("node-dana", "node-ingest")
    assert resolver.calls == [(("Dana", "ingest pipeline"), "tenant-1")]


@pytest.mark.asyncio
async def test_two_mentions_resolving_to_one_node_are_stored_once(
    memories: MemoryRepositoryFake, resolver: EntityResolverFake, owner: MemoryOwner
) -> None:
    resolver.known = {"Dana": ("node-dana", 0.9), "Dana Chen": ("node-dana", 0.8)}

    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana", "Dana Chen"])]},
        entities=resolver,
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ("node-dana",)


@pytest.mark.asyncio
async def test_extraction_drops_sub_floor_and_unresolved_mentions_but_keeps_the_fact(
    memories: MemoryRepositoryFake, resolver: EntityResolverFake, owner: MemoryOwner
) -> None:
    resolver.known = {"Dana": ("node-dana", 0.9), "ingest pipeline": ("node-ingest", 0.49)}

    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana", "ingest pipeline", "Atlantis"])]},
        entities=resolver,
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ("node-dana",)
    assert saved.content == "Dana owns ingest."


@pytest.mark.asyncio
async def test_extraction_keeps_a_fact_whose_every_mention_is_unresolvable(
    memories: MemoryRepositoryFake, resolver: EntityResolverFake, owner: MemoryOwner
) -> None:
    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana"])]},
        entities=resolver,
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ()
    assert len(memories.rows) == 1


@pytest.mark.asyncio
async def test_extraction_survives_a_failing_resolver_and_still_saves_every_fact(
    memories: MemoryRepositoryFake,
    resolver: EntityResolverFake,
    owner: MemoryOwner,
    caplog: pytest.LogCaptureFixture,
) -> None:
    resolver.failure = "graph unavailable"

    with caplog.at_level("WARNING"):
        saved = await extractor(
            memories,
            {
                "facts": [
                    fact("Dana owns ingest.", entities=["Dana"]),
                    fact("Ravi owns search.", entities=["Ravi"]),
                ]
            },
            entities=resolver,
        ).extract(owner, messages=MESSAGES)

    assert [memory.content for memory in saved] == ["Dana owns ingest.", "Ravi owns search."]
    assert all(memory.entity_ids == () for memory in saved)
    assert "resolving memory entity mentions failed" in caplog.text


@pytest.mark.asyncio
async def test_extraction_without_a_resolver_stores_the_mentions_verbatim(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana", "ingest pipeline", "  "])]},
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ("Dana", "ingest pipeline")


@pytest.mark.asyncio
async def test_a_zero_confidence_floor_keeps_every_resolution(
    memories: MemoryRepositoryFake, resolver: EntityResolverFake, owner: MemoryOwner
) -> None:
    resolver.known = {"Dana": ("node-dana", 0.01)}

    (saved,) = await extractor(
        memories,
        {"facts": [fact("Dana owns ingest.", entities=["Dana"])]},
        entities=resolver,
        policy=MemoryPolicy(entity_confidence_floor=0.0),
    ).extract(owner, messages=MESSAGES)

    assert saved.entity_ids == ("node-dana",)


@pytest.mark.asyncio
async def test_recall_with_anchors_drops_nothing(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    for stored in (
        user_fact("Ravi owns search", memory_id="mem-search", entity_ids=("node-search",)),
        user_fact("Dana owns ingest", memory_id="mem-ingest", entity_ids=("node-ingest",)),
    ):
        memories.rows[stored.memory_id] = stored
    recall = MemoryRecall(policy=MemoryPolicy(), memories=memories, clock=lambda: NOW)

    unanchored = await recall.recall(owner, QUERY)
    anchored = await recall.recall(owner, QUERY, anchor_entity_ids=("node-ingest",))

    assert [memory.memory_id for memory in unanchored] == ["mem-ingest", "mem-search"]
    assert [memory.memory_id for memory in anchored] == ["mem-ingest", "mem-search"]
    assert len(anchored) == len(unanchored)


@pytest.mark.asyncio
async def test_recall_promotes_an_anchored_memory_over_a_more_important_one(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    boosted = memory_row(
        "Dana owns ingest",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        memory_id="mem-ingest",
        importance=0.5,
        entity_ids=("node-ingest",),
    )
    plain = memory_row(
        "Ravi owns search",
        scope=MemoryScope.USER,
        owner=MemoryOwner(tenant_id="tenant-1", user_id="user-1"),
        memory_id="mem-search",
        importance=0.6,
    )
    for stored in (plain, boosted):
        memories.rows[stored.memory_id] = stored
    recall = MemoryRecall(policy=MemoryPolicy(), memories=memories, clock=lambda: NOW)

    assert [memory.memory_id for memory in await recall.recall(owner, QUERY)] == [
        "mem-search",
        "mem-ingest",
    ]
    promoted = await recall.recall(owner, QUERY, anchor_entity_ids=("node-ingest",))
    assert [memory.memory_id for memory in promoted] == ["mem-ingest", "mem-search"]


@pytest.mark.asyncio
async def test_a_zero_overlap_weight_leaves_recall_order_untouched(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    for stored in (
        user_fact("Ravi owns search", memory_id="mem-search", entity_ids=()),
        user_fact("Dana owns ingest", memory_id="mem-ingest", entity_ids=("node-ingest",)),
    ):
        memories.rows[stored.memory_id] = stored
    policy = MemoryPolicy(entity_overlap_weight=0.0)
    recall = MemoryRecall(policy=policy, memories=memories, clock=lambda: NOW)

    with_anchors = await recall.recall(owner, QUERY, anchor_entity_ids=("node-ingest",))

    assert [memory.memory_id for memory in with_anchors] == ["mem-ingest", "mem-search"]


@pytest.mark.asyncio
async def test_builder_records_the_anchors_it_recalled_with(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "who owns ingest?", token_count=10)])
    stored = user_fact("Dana owns ingest", memory_id="mem-ingest", entity_ids=("node-ingest",))
    memories.rows[stored.memory_id] = stored
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(),
        messages=store,
        memories=memories,
        clock=lambda: NOW,
    )

    context = await builder.build(
        owner, QUERY, anchor_entity_ids=("node-ingest", "node-ingest", " ")
    )

    assert context.anchor_entity_ids == ("node-ingest",)
    assert recalled_entity_ids(context) == ("node-ingest",)


@pytest.mark.asyncio
async def test_builder_without_anchors_still_recalls_and_records_nothing(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "who owns ingest?", token_count=10)])
    stored = user_fact("Dana owns ingest", memory_id="mem-ingest", entity_ids=("node-ingest",))
    memories.rows[stored.memory_id] = stored
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(),
        messages=store,
        memories=memories,
        clock=lambda: NOW,
    )

    context = await builder.build(owner, QUERY)

    assert context.anchor_entity_ids == ()
    assert [memory.memory_id for memory in context.recalled] == ["mem-ingest"]


@pytest.mark.asyncio
async def test_a_disabled_policy_records_no_anchors(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "who owns ingest?", token_count=10)])
    builder = MemoryContextBuilder(
        policy=MemoryPolicy.disabled(),
        messages=store,
        memories=memories,
        clock=lambda: NOW,
    )

    context = await builder.build(owner, QUERY, anchor_entity_ids=("node-ingest",))

    assert context.anchor_entity_ids == ()
    assert context.recalled == ()
