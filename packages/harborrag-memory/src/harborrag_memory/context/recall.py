"""Long-term recall: gather scope-safe candidates, then rank them.

Recall is best-effort by construction. Every collaborator it touches -- the
embedder, the semantic index, the canonical repository -- may be missing or
may fail, and each failure degrades one step further instead of failing the
turn: no embedder or a raising index falls back to the repository's own
substring search, and a raising repository yields no candidates for that
scope.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from harborrag_core.ports.memory import (
    Memory,
    MemoryEmbedder,
    MemoryIndex,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    MemoryType,
)

from ..langchain.converters import utc_now
from .entities import unique_ids
from .policy import MemoryPolicy
from .ranking import (
    EntityAnchor,
    ScoredMemory,
    ScoringContext,
    TypeAffinity,
    score_memory,
    select,
)
from .scoping import scope_query_owner
from .tokens import approximate_tokens

logger = logging.getLogger(__name__)

RECALL_MEMORY_TYPES: tuple[MemoryType, ...] = (
    MemoryType.FACT,
    MemoryType.PREFERENCE,
    MemoryType.DECISION,
    MemoryType.EPISODE,
)
"""Types recall surfaces. The rolling summary is already its own context field."""

FANOUT_MULTIPLIER = 2
"""Per-scope over-fetch so dedup and budget trimming have candidates to drop."""

MAX_QUERY_LIMIT = 1000
"""``MemoryQuery`` rejects a larger limit."""

FALLBACK_RELEVANCE = 1.0
"""Substring search ranks nothing, so recency and importance decide the order."""


class MemoryRecall:
    """Recall the long-term memories worth spending prompt budget on.

    Scopes are consulted in ``policy.recall_scopes`` order, each as an owner
    narrowed to exactly the fields that scope keys on, so a recall can never
    read a project, user, or session the caller does not already own.
    """

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        *,
        policy: MemoryPolicy,
        memories: MemoryRepository,
        index: MemoryIndex | None = None,
        embedder: MemoryEmbedder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._memories = memories
        self._index = index
        self._embedder = embedder
        self._clock: Callable[[], datetime] = clock or utc_now

    async def recall(
        self,
        owner: MemoryOwner,
        query: str,
        *,
        anchor_entity_ids: Sequence[str] = (),
        wanted_types: Sequence[MemoryType] = (),
    ) -> tuple[Memory, ...]:
        """Return at most ``policy.recall_top_k`` memories relevant to ``query``.

        ``anchor_entity_ids`` are the knowledge-graph entities this turn's
        document retrieval surfaced. They only ever re-rank: a memory sharing
        entities with them scores higher, and a memory with no entities -- or
        an empty anchor set -- is left exactly where it was.

        ``wanted_types`` are the memory types the turn's query rewrite
        reported the question asks for. They only ever re-rank too: every
        query still asks for all of ``RECALL_MEMORY_TYPES``, so a hint can
        promote the kinds asked for without hiding the ones that were not.
        """

        text = query.strip()
        if self._policy.recall_top_k == 0 or not text:
            return ()
        context = ScoringContext(
            now=self._clock(),
            half_life_hours=self._policy.recency_half_life_hours,
            anchor=self._anchor(anchor_entity_ids),
            affinity=self._affinity(wanted_types),
        )
        embedding = await self._embed(text)
        candidates: list[ScoredMemory] = []
        for scope in self._policy.recall_scopes:
            scoped = self._query(owner, scope, text)
            if scoped is None:
                continue
            found = await self._candidates(scoped, embedding)
            candidates.extend(
                score_memory(memory, relevance=relevance, context=context)
                for memory, relevance in found
                if scoped.include_invalid or memory.is_valid_at(context.now)
            )
        return select(
            candidates,
            top_k=self._policy.recall_top_k,
            max_tokens=self._block_budget(),
            counter=approximate_tokens,
        )

    def _anchor(self, anchor_entity_ids: Sequence[str]) -> EntityAnchor:
        """Pair this turn's surfaced graph entities with the policy's boost weight."""

        return EntityAnchor(
            entity_ids=unique_ids(tuple(anchor_entity_ids)),
            weight=self._policy.entity_overlap_weight,
        )

    def _affinity(self, wanted_types: Sequence[MemoryType]) -> TypeAffinity:
        """Pair the types this turn's question asked for with the policy's weight."""

        return TypeAffinity(
            types=tuple(dict.fromkeys(wanted_types)),
            weight=self._policy.type_affinity_weight,
        )

    def _block_budget(self) -> int:
        """Tokens the rendered memory block may occupy."""

        return int(self._policy.block_budget_fraction * self._policy.recent_max_tokens)

    def _query(self, owner: MemoryOwner, scope: MemoryScope, text: str) -> MemoryQuery | None:
        """Build the one-scope query, or ``None`` when the owner cannot address it."""

        scoped = scope_query_owner(owner, scope)
        if scoped is None:
            return None
        limit = min(MAX_QUERY_LIMIT, max(1, self._policy.recall_top_k * FANOUT_MULTIPLIER))
        return MemoryQuery(
            owner=scoped,
            scopes=(scope,),
            memory_types=RECALL_MEMORY_TYPES,
            text=text,
            limit=limit,
        )

    async def _embed(self, text: str) -> Sequence[float] | None:
        """Embed the recall query, or return ``None`` to take the substring path."""

        if self._embedder is None:
            return None
        try:
            return await self._embedder(text)
        except Exception:
            logger.warning("embedding the recall query failed", exc_info=True)
            return None

    async def _candidates(
        self,
        query: MemoryQuery,
        embedding: Sequence[float] | None,
    ) -> tuple[tuple[Memory, float], ...]:
        """Return one scope's candidates with their relevance, index first."""

        hits = await self._index_candidates(query, embedding)
        if hits is not None:
            return hits
        try:
            found = await self._memories.search(query)
        except Exception:
            logger.warning("long-term recall repository search failed", exc_info=True)
            return ()
        return tuple((memory, FALLBACK_RELEVANCE) for memory in found)

    async def _index_candidates(
        self,
        query: MemoryQuery,
        embedding: Sequence[float] | None,
    ) -> tuple[tuple[Memory, float], ...] | None:
        """Search the index and hydrate its hits, or ``None`` to fall back.

        The index returns ids and scores only, so the canonical repository
        stays the system of record and re-applies the scope filter as the hits
        are hydrated. An empty tuple means the index genuinely knows nothing;
        ``None`` means the index could not be used at all.
        """

        if self._index is None or embedding is None:
            return None
        try:
            matches = await self._index.search_memories(query, embedding=embedding)
            hydrated = [
                (memory, match.score)
                for match in matches
                if (memory := await self._memories.get(query.owner, match.memory_id)) is not None
            ]
        except Exception:
            logger.warning(
                "long-term recall index search failed; falling back to the repository",
                exc_info=True,
            )
            return None
        return tuple(hydrated)


__all__ = ["FALLBACK_RELEVANCE", "RECALL_MEMORY_TYPES", "MemoryRecall"]
