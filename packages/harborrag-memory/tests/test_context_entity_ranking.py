"""Pure entity-anchoring rules: the overlap term, the policy knobs, and seeding."""

from __future__ import annotations

import pytest
from context_test_fakes import NOW, memory_row

from harborrag_core.ports.memory import Memory, MemoryScope
from harborrag_memory import MemoryPolicy, recalled_entity_ids
from harborrag_memory.context import MemoryContext
from harborrag_memory.context.entities import unique_ids
from harborrag_memory.context.ranking import (
    NO_ANCHOR,
    EntityAnchor,
    ScoringContext,
    entity_weight,
    fused_score,
    rank,
    score_memory,
)
from harborrag_memory.errors import MemoryConfigurationError

pytestmark = [pytest.mark.unit]

HALF_LIFE = 168.0
ANCHOR = EntityAnchor(entity_ids=("node-a", "node-b"), weight=0.5)
PLAIN = ScoringContext(now=NOW, half_life_hours=HALF_LIFE)
ANCHORED = ScoringContext(now=NOW, half_life_hours=HALF_LIFE, anchor=ANCHOR)


def anchored(*entity_ids: str, importance: float = 0.5, memory_id: str | None = None) -> Memory:
    return memory_row(
        f"fact about {'-'.join(entity_ids) or 'nothing'}",
        scope=MemoryScope.USER,
        importance=importance,
        updated_at=NOW,
        memory_id=memory_id,
        entity_ids=tuple(entity_ids),
    )


def context(*memories: Memory) -> MemoryContext:
    return MemoryContext(
        messages=(),
        summary=None,
        recalled=tuple(memories),
        standalone_query="q",
        rewritten=False,
        summary_written=False,
    )


def test_full_overlap_scores_the_whole_policy_weight() -> None:
    assert entity_weight(anchored("node-a", "node-b"), ANCHOR) == pytest.approx(0.5)


def test_partial_overlap_scores_the_matching_share() -> None:
    memory = anchored("node-a", "node-z", "node-y")
    assert entity_weight(memory, ANCHOR) == pytest.approx(0.5 / 3)


def test_no_overlap_scores_zero() -> None:
    assert entity_weight(anchored("node-y", "node-z"), ANCHOR) == 0.0


def test_a_memory_without_entities_is_unaffected() -> None:
    assert entity_weight(anchored(), ANCHOR) == 0.0


def test_an_empty_anchor_set_is_unaffected() -> None:
    memory = anchored("node-a")
    assert entity_weight(memory, EntityAnchor(entity_ids=(), weight=0.5)) == 0.0
    assert entity_weight(memory, NO_ANCHOR) == 0.0
    assert entity_weight(memory) == 0.0


def test_a_zero_weight_disables_the_term_entirely() -> None:
    memory = anchored("node-a", "node-b")
    assert entity_weight(memory, EntityAnchor(entity_ids=("node-a", "node-b"), weight=0.0)) == 0.0


def test_duplicate_anchor_ids_do_not_inflate_the_share() -> None:
    memory = anchored("node-a", "node-b")
    inflated = EntityAnchor(entity_ids=unique_ids(("node-a", "node-a", "node-b")), weight=0.5)
    assert entity_weight(memory, inflated) == pytest.approx(0.5)


def test_the_overlap_term_is_never_negative_so_it_can_only_boost() -> None:
    for memory in (anchored(), anchored("node-a"), anchored("node-y", "node-z")):
        assert entity_weight(memory, ANCHOR) >= 0.0


def test_fused_score_multiplies_the_overlap_boost_onto_the_base() -> None:
    memory = anchored("node-a", "node-b", importance=0.5)
    base = fused_score(memory, relevance=1.0, context=PLAIN)
    boosted = fused_score(memory, relevance=1.0, context=ANCHORED)
    assert base == pytest.approx(2.0 * 1.5)
    assert boosted == pytest.approx(base * 1.5)


def test_fused_score_leaves_an_unanchored_memory_exactly_as_it_was() -> None:
    memory = anchored(importance=0.25)
    assert fused_score(memory, relevance=0.8, context=ANCHORED) == fused_score(
        memory, relevance=0.8, context=PLAIN
    )


def test_overlap_re_ranks_without_dropping_the_unanchored_candidate() -> None:
    matched = anchored("node-a", memory_id="mem-matched")
    unmatched = anchored("node-y", memory_id="mem-unmatched")
    scored = [
        score_memory(memory, relevance=1.0, context=ANCHORED) for memory in (unmatched, matched)
    ]

    ranked = rank(scored)

    assert [candidate.memory.memory_id for candidate in ranked] == [
        "mem-matched",
        "mem-unmatched",
    ]
    assert len(ranked) == 2
    assert all(candidate.score > 0.0 for candidate in ranked)


def test_recalled_entity_ids_dedupes_and_preserves_recall_order() -> None:
    assert recalled_entity_ids(
        context(anchored("node-b", "node-a"), anchored("node-a", "node-c"), anchored())
    ) == ("node-b", "node-a", "node-c")


def test_recalled_entity_ids_is_empty_without_recalled_memories() -> None:
    assert recalled_entity_ids(context()) == ()


def test_policy_defaults_enable_entity_anchoring() -> None:
    policy = MemoryPolicy()
    assert (policy.entity_confidence_floor, policy.entity_overlap_weight) == (0.5, 0.5)


@pytest.mark.parametrize("floor", [-0.01, 1.01, float("nan")])
def test_policy_rejects_an_out_of_range_confidence_floor(floor: float) -> None:
    with pytest.raises(MemoryConfigurationError, match="entity_confidence_floor"):
        MemoryPolicy(entity_confidence_floor=floor)


@pytest.mark.parametrize("weight", [-0.01, 2.01, float("inf")])
def test_policy_rejects_an_out_of_range_overlap_weight(weight: float) -> None:
    with pytest.raises(MemoryConfigurationError, match="entity_overlap_weight"):
        MemoryPolicy(entity_overlap_weight=weight)


def test_policy_accepts_the_disabling_bounds() -> None:
    policy = MemoryPolicy(entity_confidence_floor=0.0, entity_overlap_weight=0.0)
    assert (policy.entity_confidence_floor, policy.entity_overlap_weight) == (0.0, 0.0)
    assert MemoryPolicy(entity_overlap_weight=2.0).entity_overlap_weight == 2.0
