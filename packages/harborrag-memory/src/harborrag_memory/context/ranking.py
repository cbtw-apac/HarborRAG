"""Pure fusion, ranking, dedup, and budget rules for long-term recall.

Every function here is a total function of its arguments: no clock, no
repository, no index. Recall assembles candidates from however many scopes it
was told to consult and then hands them to ``select``, which is where the
ordering contract actually lives -- and where it can be tested without a
single fake.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from harborrag_core.ports.memory import Memory, MemoryScope, MemoryType

from .hashing import normalize_content

SCOPE_RANK: dict[MemoryScope, int] = {scope: index for index, scope in enumerate(MemoryScope)}
"""Narrower scopes rank lower and win ties: RUN < SESSION < USER < PROJECT < TENANT < GLOBAL."""

SECONDS_PER_HOUR = 3600.0

type TextCounter = Callable[[str], int]


@dataclass(frozen=True, slots=True)
class EntityAnchor:
    """The knowledge-graph entities this turn surfaced, and their boost weight.

    ``entity_ids`` are the curated graph nodes the turn's *document* retrieval
    actually returned; ``weight`` is ``MemoryPolicy.entity_overlap_weight``.
    The default is the neutral anchor: no entities and no weight, so ranking
    behaves exactly as it did before entity anchoring existed.
    """

    entity_ids: tuple[str, ...] = ()
    weight: float = 0.0


NO_ANCHOR = EntityAnchor()
"""The neutral anchor: an unanchored turn re-ranks nothing."""


@dataclass(frozen=True, slots=True)
class TypeAffinity:
    """The memory types this turn's question asked for, and their boost weight.

    ``types`` is what the per-turn query rewrite reported the question wants
    -- "why did we pick X" wants decisions, "how do you want it formatted"
    wants preferences -- capped so a hint cannot dilute into noise. ``weight``
    is ``MemoryPolicy.type_affinity_weight``. The default is the neutral
    affinity: nothing asked for and no weight, so ranking behaves exactly as
    it did before the type hint existed.
    """

    types: tuple[MemoryType, ...] = ()
    weight: float = 0.0


NO_AFFINITY = TypeAffinity()
"""The neutral affinity: a turn that wanted no particular type re-ranks nothing."""


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    """One recall candidate with the relevance it came in with and its fused score."""

    memory: Memory
    relevance: float
    score: float


def hours_between(moment: datetime, now: datetime) -> float:
    """Return the non-negative hours from ``moment`` to ``now``.

    A row written by a clock slightly ahead of this one would otherwise decay
    to a weight above one and outrank everything.
    """

    return max(0.0, (now - moment).total_seconds() / SECONDS_PER_HOUR)


def recency_weight(memory: Memory, *, now: datetime, half_life_hours: float) -> float:
    """Return the half-life decay of ``memory``'s age: 1.0 fresh, 0.5 one half-life old."""

    half_lives = hours_between(memory.updated_at, now) / half_life_hours
    return float(0.5**half_lives)


def entity_weight(memory: Memory, anchor: EntityAnchor = NO_ANCHOR) -> float:
    """Return the share of ``memory``'s entities the anchor surfaced, times its weight.

    The term is non-negative by construction, so entity overlap can only
    re-rank candidates -- never drop one. A memory with no ``entity_ids``, an
    empty anchor, or a zero weight scores 0 and is left exactly as it was.
    """

    if anchor.weight <= 0.0 or not memory.entity_ids or not anchor.entity_ids:
        return 0.0
    surfaced = set(anchor.entity_ids)
    overlapping = sum(1 for entity_id in memory.entity_ids if entity_id in surfaced)
    return anchor.weight * overlapping / max(1, len(memory.entity_ids))


def type_weight(memory: Memory, affinity: TypeAffinity = NO_AFFINITY) -> float:
    """Return ``affinity.weight`` when the memory's type was asked for, else 0.0.

    The boost is binary rather than graded by the model's ordering: grading
    would need a decay constant no evaluation harness exists to validate, and
    the cap on how many types a turn may want already stops the hint diluting
    into noise. The term is non-negative by construction, exactly like
    ``entity_weight``, so a hint can only re-rank candidates -- never drop
    one.
    """

    if affinity.weight <= 0.0 or memory.memory_type not in affinity.types:
        return 0.0
    return affinity.weight


@dataclass(frozen=True, slots=True)
class ScoringContext:
    """Everything the fused score needs about the turn, rather than the memory.

    Bundling the clock reading, the decay constant, and the two re-ranking
    hints keeps ``fused_score`` and ``score_memory`` at three arguments: both
    are pure functions of one candidate plus this context, and a new hint
    becomes a new field here instead of a new parameter everywhere.
    """

    now: datetime
    half_life_hours: float
    anchor: EntityAnchor = NO_ANCHOR
    affinity: TypeAffinity = NO_AFFINITY


def fused_score(memory: Memory, *, relevance: float, context: ScoringContext) -> float:
    """Return ``relevance * (1 + recency) * (1 + importance) * (1 + entity) * (1 + type)``.

    Relevance leads because a memory that does not answer the question is
    worthless however fresh or important it is; recency and importance are
    multiplicative boosts bounded at 2x each, entity overlap is a further
    boost for memories anchored to the graph entities this turn surfaced, and
    the type term boosts the kinds of memory the question actually asked for.
    """

    recency = recency_weight(memory, now=context.now, half_life_hours=context.half_life_hours)
    entity = entity_weight(memory, context.anchor)
    wanted = type_weight(memory, context.affinity)
    return relevance * (1.0 + recency) * (1.0 + memory.importance) * (1.0 + entity) * (1.0 + wanted)


def score_memory(memory: Memory, *, relevance: float, context: ScoringContext) -> ScoredMemory:
    """Fuse one candidate's relevance with its recency, importance, and hints."""

    return ScoredMemory(
        memory=memory,
        relevance=relevance,
        score=fused_score(memory, relevance=relevance, context=context),
    )


def rank(candidates: Iterable[ScoredMemory]) -> tuple[ScoredMemory, ...]:
    """Order candidates by fused score, breaking ties toward the narrower scope.

    A session fact and a tenant fact that score identically are not
    interchangeable: the narrower one was written closer to this caller, so it
    wins. The memory id is the final tiebreak so the order is total.
    """

    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                -candidate.score,
                SCOPE_RANK[candidate.memory.scope],
                candidate.memory.memory_id,
            ),
        )
    )


def dedupe_key(memory: Memory) -> str:
    """Return the identity two restatements of one fact share."""

    return memory.content_hash or normalize_content(memory.content)


def deduplicate(candidates: Sequence[ScoredMemory]) -> tuple[ScoredMemory, ...]:
    """Drop later restatements, keeping the highest-scoring instance.

    ``candidates`` must already be ranked: the first occurrence of a key is
    the one kept, so ranking before dedup is what makes "highest-scoring
    instance" true.
    """

    seen: set[str] = set()
    kept: list[ScoredMemory] = []
    for candidate in candidates:
        key = dedupe_key(candidate.memory)
        if key in seen:
            continue
        seen.add(key)
        kept.append(candidate)
    return tuple(kept)


def within_budget(
    memories: Sequence[Memory],
    *,
    max_tokens: int,
    counter: TextCounter,
) -> tuple[Memory, ...]:
    """Keep the leading memories whose rendered content fits ``max_tokens``.

    Truncation stops at the first entry that would exceed the budget rather
    than skipping it, so the block is always a prefix of the ranking and the
    cap is never exceeded -- not even by one memory.
    """

    kept: list[Memory] = []
    used = 0
    for memory in memories:
        cost = counter(memory.content)
        if used + cost > max_tokens:
            break
        kept.append(memory)
        used += cost
    return tuple(kept)


def select(
    candidates: Iterable[ScoredMemory],
    *,
    top_k: int,
    max_tokens: int,
    counter: TextCounter,
) -> tuple[Memory, ...]:
    """Rank, dedupe, cap at ``top_k``, and trim to the block token budget."""

    ranked = deduplicate(rank(candidates))[:top_k]
    return within_budget(
        tuple(candidate.memory for candidate in ranked),
        max_tokens=max_tokens,
        counter=counter,
    )


__all__ = [
    "NO_AFFINITY",
    "NO_ANCHOR",
    "SCOPE_RANK",
    "EntityAnchor",
    "ScoredMemory",
    "ScoringContext",
    "TextCounter",
    "TypeAffinity",
    "dedupe_key",
    "deduplicate",
    "entity_weight",
    "fused_score",
    "hours_between",
    "rank",
    "recency_weight",
    "score_memory",
    "select",
    "type_weight",
    "within_budget",
]
