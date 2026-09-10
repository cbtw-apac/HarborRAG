from __future__ import annotations

from datetime import timedelta

import pytest
from context_test_fakes import NOW, memory_row

from harborrag_core.ports.memory import MemoryOwner, MemoryScope
from harborrag_memory.context import approximate_tokens, content_hash, scope_query_owner
from harborrag_memory.context.hashing import normalize_content
from harborrag_memory.context.ranking import (
    ScoringContext,
    dedupe_key,
    deduplicate,
    fused_score,
    hours_between,
    rank,
    recency_weight,
    score_memory,
    select,
    within_budget,
)

pytestmark = [pytest.mark.unit]

HALF_LIFE = 168.0
CONTEXT = ScoringContext(now=NOW, half_life_hours=HALF_LIFE)


def scored(
    content: str,
    *,
    relevance: float = 1.0,
    importance: float = 0.5,
    scope: MemoryScope = MemoryScope.USER,
    age_hours: float = 0.0,
    **extra: object,
):
    return score_memory(
        memory_row(
            content,
            scope=scope,
            importance=importance,
            updated_at=NOW - timedelta(hours=age_hours),
            **extra,  # type: ignore[arg-type]
        ),
        relevance=relevance,
        context=CONTEXT,
    )


@pytest.mark.parametrize(
    ("age_hours", "expected"),
    [(0.0, 1.0), (HALF_LIFE, 0.5), (2 * HALF_LIFE, 0.25)],
)
def test_recency_weight_halves_every_half_life(age_hours: float, expected: float) -> None:
    memory = memory_row("x", updated_at=NOW - timedelta(hours=age_hours))

    assert recency_weight(memory, now=NOW, half_life_hours=HALF_LIFE) == pytest.approx(expected)


def test_hours_between_never_goes_negative() -> None:
    assert hours_between(NOW + timedelta(hours=5), NOW) == 0.0
    assert hours_between(NOW - timedelta(hours=3), NOW) == pytest.approx(3.0)


def test_fused_score_multiplies_relevance_by_recency_and_importance() -> None:
    memory = memory_row("x", importance=0.25, updated_at=NOW - timedelta(hours=HALF_LIFE))

    fused = fused_score(memory, relevance=0.5, context=CONTEXT)

    assert fused == pytest.approx(0.5 * 1.5 * 1.25)


def test_importance_breaks_a_tie_between_equally_relevant_memories() -> None:
    ranked = rank([scored("dull", importance=0.1), scored("vital", importance=0.9)])

    assert [candidate.memory.content for candidate in ranked] == ["vital", "dull"]


def test_freshness_outranks_an_older_equally_relevant_memory() -> None:
    ranked = rank([scored("stale", age_hours=1000.0), scored("fresh")])

    assert [candidate.memory.content for candidate in ranked] == ["fresh", "stale"]


def test_equal_scores_break_toward_the_narrower_scope() -> None:
    candidates = [
        scored("tenant", scope=MemoryScope.TENANT),
        scored("project", scope=MemoryScope.PROJECT),
        scored("user", scope=MemoryScope.USER),
        scored("session", scope=MemoryScope.SESSION),
    ]

    ranked = rank(candidates)

    assert [candidate.memory.content for candidate in ranked] == [
        "session",
        "user",
        "project",
        "tenant",
    ]


def test_dedupe_key_prefers_the_content_hash_over_the_content() -> None:
    digest = content_hash("Dana owns ingest", MemoryScope.USER)

    assert dedupe_key(memory_row("anything at all", content_hash=digest)) == digest
    assert dedupe_key(memory_row("Dana  OWNS ingest")) == normalize_content("dana owns ingest")


def test_deduplicate_by_hash_keeps_the_highest_scoring_instance() -> None:
    digest = content_hash("Dana owns ingest", MemoryScope.USER)
    ranked = rank(
        [
            scored("weak restatement", importance=0.1, content_hash=digest),
            scored("strong restatement", importance=0.9, content_hash=digest),
        ]
    )

    kept = deduplicate(ranked)

    assert [candidate.memory.content for candidate in kept] == ["strong restatement"]


def test_deduplicate_by_normalized_content_when_no_hash_is_stored() -> None:
    ranked = rank([scored("Dana owns ingest", importance=0.9), scored("dana   OWNS ingest")])

    kept = deduplicate(ranked)

    assert len(kept) == 1
    assert kept[0].memory.importance == 0.9


def test_within_budget_stops_before_exceeding_the_block_budget() -> None:
    rows = [memory_row("a" * 40), memory_row("b" * 40), memory_row("c" * 40)]

    kept = within_budget(rows, max_tokens=25, counter=approximate_tokens)

    assert [row.content[0] for row in kept] == ["a", "b"]
    assert sum(approximate_tokens(row.content) for row in kept) <= 25


def test_within_budget_drops_everything_when_the_first_entry_will_not_fit() -> None:
    assert within_budget([memory_row("a" * 40)], max_tokens=1, counter=approximate_tokens) == ()


def test_select_caps_at_top_k_before_trimming_to_the_budget() -> None:
    candidates = [scored(f"fact {index}", importance=index / 10) for index in range(6)]

    selected = select(candidates, top_k=2, max_tokens=1000, counter=approximate_tokens)

    assert [memory.content for memory in selected] == ["fact 5", "fact 4"]


def test_scope_query_owner_narrows_the_owner_and_never_widens_it(owner: MemoryOwner) -> None:
    scoped = scope_query_owner(owner, MemoryScope.USER)

    assert scoped == MemoryOwner(tenant_id="tenant-1", user_id="user-1")
    assert scope_query_owner(owner, MemoryScope.SESSION) == MemoryOwner(
        tenant_id="tenant-1", user_id="user-1", session_id="s-1"
    )


def test_scope_query_owner_refuses_a_scope_the_owner_cannot_address() -> None:
    projectless = MemoryOwner(tenant_id="tenant-1", principal_id="principal-1", session_id="s-1")

    assert scope_query_owner(projectless, MemoryScope.PROJECT) is None
    assert scope_query_owner(projectless, MemoryScope.RUN) is None


def test_scope_query_owner_derives_user_id_from_principal_id() -> None:
    owner = MemoryOwner(tenant_id="tenant-1", principal_id="principal-1", session_id="s-1")

    assert scope_query_owner(owner, MemoryScope.USER) == MemoryOwner(
        tenant_id="tenant-1", user_id="principal-1"
    )
