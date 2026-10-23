"""Type affinity: the ranking term, the policy knob, and the recall wiring.

A type hint may only re-rank. Recall still queries every recallable type, and
retention stays keyed on scope -- neither is affected by what a turn wanted.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from context_test_fakes import (
    NOW,
    MemoryRepositoryFake,
    MessageStoreFake,
    memory_row,
    row,
    structured_model,
)

from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import Memory, MemoryOwner, MemoryScope, MemoryType
from harborrag_memory import MemoryContextBuilder, MemoryPolicy, MemoryRecall
from harborrag_memory.context import NO_AFFINITY, ScoringContext, TypeAffinity, type_weight
from harborrag_memory.context.ranking import EntityAnchor, fused_score, rank, score_memory
from harborrag_memory.context.recall import RECALL_MEMORY_TYPES
from harborrag_memory.errors import MemoryConfigurationError

pytestmark = [pytest.mark.unit]

HALF_LIFE = 168.0
WANTED = TypeAffinity(types=(MemoryType.DECISION,), weight=0.5)
QUERY = "why did we pick pgvector?"
USER = MemoryOwner(tenant_id="tenant-1", user_id="user-1")


def typed(
    memory_type: MemoryType,
    *,
    memory_id: str,
    age_hours: float = 0.0,
    **extra: object,
) -> Memory:
    return memory_row(
        f"a stored {memory_type.value}",
        scope=MemoryScope.USER,
        owner=USER,
        memory_type=memory_type,
        importance=0.5,
        updated_at=NOW - timedelta(hours=age_hours),
        memory_id=memory_id,
        **extra,  # type: ignore[arg-type]
    )


def context(**overrides: object) -> ScoringContext:
    return ScoringContext(now=NOW, half_life_hours=HALF_LIFE, **overrides)  # type: ignore[arg-type]


def recaller(memories: MemoryRepositoryFake) -> MemoryRecall:
    return MemoryRecall(policy=MemoryPolicy(), memories=memories, clock=lambda: NOW)


def seed(memories: MemoryRepositoryFake, *rows: Memory) -> None:
    for stored in rows:
        memories.rows[stored.memory_id] = stored


def test_a_wanted_type_scores_the_whole_policy_weight() -> None:
    assert type_weight(typed(MemoryType.DECISION, memory_id="m"), WANTED) == pytest.approx(0.5)


def test_a_type_nobody_asked_for_scores_zero() -> None:
    assert type_weight(typed(MemoryType.FACT, memory_id="m"), WANTED) == 0.0


def test_an_empty_affinity_leaves_every_type_alone() -> None:
    memory = typed(MemoryType.DECISION, memory_id="m")
    assert type_weight(memory, TypeAffinity(types=(), weight=0.5)) == 0.0
    assert type_weight(memory, NO_AFFINITY) == 0.0
    assert type_weight(memory) == 0.0


def test_a_zero_weight_disables_the_term_entirely() -> None:
    memory = typed(MemoryType.DECISION, memory_id="m")
    assert type_weight(memory, TypeAffinity(types=(MemoryType.DECISION,), weight=0.0)) == 0.0


def test_the_type_term_is_never_negative_so_it_can_only_boost() -> None:
    for memory_type in RECALL_MEMORY_TYPES:
        assert type_weight(typed(memory_type, memory_id="m"), WANTED) >= 0.0


def test_a_scoring_context_defaults_to_no_anchor_and_no_affinity() -> None:
    plain = ScoringContext(now=NOW, half_life_hours=HALF_LIFE)
    assert plain.affinity == NO_AFFINITY
    assert plain.affinity.types == ()
    assert plain.affinity.weight == 0.0
    assert plain.anchor.entity_ids == ()


def test_fused_score_multiplies_all_four_boosts_onto_the_relevance() -> None:
    memory = typed(
        MemoryType.DECISION,
        memory_id="m",
        age_hours=HALF_LIFE,
        entity_ids=("node-a",),
    )
    anchor = EntityAnchor(entity_ids=("node-a",), weight=0.5)

    fused = fused_score(
        memory,
        relevance=1.0,
        context=context(anchor=anchor, affinity=WANTED),
    )

    assert fused == pytest.approx(1.5 * 1.5 * 1.5 * 1.5)


def test_a_wanted_type_outranks_a_fresher_type_nobody_asked_for() -> None:
    aging = typed(MemoryType.DECISION, memory_id="mem-decision", age_hours=100.0)
    fresh = typed(MemoryType.FACT, memory_id="mem-fact")

    ranked = rank(
        score_memory(memory, relevance=1.0, context=context(affinity=WANTED))
        for memory in (fresh, aging)
    )

    assert [candidate.memory.memory_id for candidate in ranked] == ["mem-decision", "mem-fact"]


def test_the_type_term_re_ranks_without_dropping_the_unwanted_candidate() -> None:
    scored = [
        score_memory(memory, relevance=1.0, context=context(affinity=WANTED))
        for memory in (
            typed(MemoryType.FACT, memory_id="mem-fact"),
            typed(MemoryType.DECISION, memory_id="mem-decision"),
        )
    ]

    ranked = rank(scored)

    assert len(ranked) == 2
    assert all(candidate.score > 0.0 for candidate in ranked)


def test_policy_defaults_enable_type_affinity() -> None:
    assert MemoryPolicy().type_affinity_weight == 0.5


@pytest.mark.parametrize("weight", [-0.01, 2.01, float("nan")])
def test_policy_rejects_an_out_of_range_type_affinity_weight(weight: float) -> None:
    with pytest.raises(MemoryConfigurationError, match="type_affinity_weight"):
        MemoryPolicy(type_affinity_weight=weight)


def test_policy_accepts_the_disabling_and_maximum_bounds() -> None:
    assert MemoryPolicy(type_affinity_weight=0.0).type_affinity_weight == 0.0
    assert MemoryPolicy(type_affinity_weight=2.0).type_affinity_weight == 2.0


@pytest.mark.asyncio
async def test_recall_promotes_a_wanted_type_over_an_equally_relevant_other(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(
        memories,
        typed(MemoryType.FACT, memory_id="mem-a-fact"),
        typed(MemoryType.DECISION, memory_id="mem-b-decision"),
    )

    unhinted = await recaller(memories).recall(owner, QUERY)
    hinted = await recaller(memories).recall(owner, QUERY, wanted_types=(MemoryType.DECISION,))

    assert [memory.memory_id for memory in unhinted] == ["mem-a-fact", "mem-b-decision"]
    assert [memory.memory_id for memory in hinted] == ["mem-b-decision", "mem-a-fact"]


@pytest.mark.asyncio
async def test_a_type_hint_never_narrows_the_query_it_only_re_ranks(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(
        memories,
        typed(MemoryType.FACT, memory_id="mem-fact"),
        typed(MemoryType.PREFERENCE, memory_id="mem-preference"),
        typed(MemoryType.EPISODE, memory_id="mem-episode"),
        typed(MemoryType.DECISION, memory_id="mem-decision"),
    )

    hinted = await recaller(memories).recall(owner, QUERY, wanted_types=(MemoryType.DECISION,))

    assert len(hinted) == len(RECALL_MEMORY_TYPES)
    assert hinted[0].memory_type is MemoryType.DECISION
    assert {memory.memory_type for memory in hinted} == set(RECALL_MEMORY_TYPES)
    assert all(query.memory_types == RECALL_MEMORY_TYPES for query in memories.queries)


@pytest.mark.asyncio
async def test_a_zero_affinity_weight_leaves_the_recall_order_untouched(
    memories: MemoryRepositoryFake, owner: MemoryOwner
) -> None:
    seed(
        memories,
        typed(MemoryType.FACT, memory_id="mem-a-fact"),
        typed(MemoryType.DECISION, memory_id="mem-b-decision"),
    )
    disabled = MemoryRecall(
        policy=MemoryPolicy(type_affinity_weight=0.0), memories=memories, clock=lambda: NOW
    )

    recalled = await disabled.recall(owner, QUERY, wanted_types=(MemoryType.DECISION,))

    assert [memory.memory_id for memory in recalled] == ["mem-a-fact", "mem-b-decision"]


def hinting_builder(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake | None = None,
    *,
    query_rewrite: bool = True,
) -> MemoryContextBuilder:
    return MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500, query_rewrite=query_rewrite),
        messages=store,
        memories=memories,
        model=structured_model({"query": QUERY, "wanted_types": ["decision"]}),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_builder_records_the_wanted_types_and_recalls_with_them(
    store: MessageStoreFake,
    memories: MemoryRepositoryFake,
    owner: MemoryOwner,
    identity: ConversationIdentity,
) -> None:
    await store.append_messages(identity, [row("user", "Which store did we pick?", token_count=10)])
    seed(
        memories,
        typed(MemoryType.FACT, memory_id="mem-a-fact"),
        typed(MemoryType.DECISION, memory_id="mem-b-decision"),
    )

    built = await hinting_builder(store, memories).build(owner, "and why?")

    assert built.wanted_types == (MemoryType.DECISION,)
    assert [memory.memory_id for memory in built.recalled] == ["mem-b-decision", "mem-a-fact"]


@pytest.mark.asyncio
async def test_the_first_turn_wants_no_types_at_all(
    store: MessageStoreFake, owner: MemoryOwner
) -> None:
    built = await hinting_builder(store).build(owner, "and why?")

    assert built.wanted_types == ()
    assert built.rewritten is False


@pytest.mark.asyncio
async def test_no_types_are_wanted_when_query_rewriting_is_off(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await store.append_messages(identity, [row("user", "Which store did we pick?", token_count=10)])

    built = await hinting_builder(store, query_rewrite=False).build(owner, "and why?")

    assert built.wanted_types == ()
    assert built.standalone_query == "and why?"


@pytest.mark.asyncio
async def test_a_question_the_model_left_alone_still_records_its_wanted_types(
    store: MessageStoreFake, owner: MemoryOwner, identity: ConversationIdentity
) -> None:
    await store.append_messages(identity, [row("user", "Which store did we pick?", token_count=10)])
    builder = MemoryContextBuilder(
        policy=MemoryPolicy(recent_max_tokens=500),
        messages=store,
        model=structured_model({"query": QUERY, "wanted_types": ["decision"]}),
        clock=lambda: NOW,
    )

    built = await builder.build(owner, QUERY)

    assert built.rewritten is False
    assert built.standalone_query == QUERY
    assert built.wanted_types == (MemoryType.DECISION,)
