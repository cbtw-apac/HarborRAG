"""Extract durable memories from a conversation, add-only and idempotent.

Nothing here overwrites or deletes. A new fact that contradicts a stored one
closes the stored row's validity interval (``invalid_at``) and points it at
its replacement (``superseded_by``), so the history stays readable and a
recall that asks "what was true then" still answers. A restatement of a fact
already stored is skipped outright, by content hash first and by index
similarity second, which is what makes a retried turn a no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from langchain_core.language_models import BaseChatModel

from harborrag_core.ports.conversation import ConversationMessage
from harborrag_core.ports.memory import (
    Memory,
    MemoryEmbedder,
    MemoryEntityResolver,
    MemoryIndex,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    new_memory_id,
)

from ..langchain.converters import utc_now
from .entities import anchored_fact
from .facts import ExtractedFact, extracted_facts
from .hashing import content_hash
from .policy import MemoryPolicy
from .prompting import render_transcript
from .prompts import EXTRACTION_USER_TEMPLATE, NO_MEMORIES_PLACEHOLDER
from .proposing import propose_facts
from .recall import RECALL_MEMORY_TYPES
from .scoping import scope_query_owner
from .summarizer import recall_owner

logger = logging.getLogger(__name__)

EXTRACTION_SCOPE_ORDER: tuple[MemoryScope, ...] = (
    MemoryScope.SESSION,
    MemoryScope.USER,
    MemoryScope.PROJECT,
)
"""Scopes extraction reads for context and may write to, narrowest first."""

EXISTING_LIMIT = 50
"""How many stored memories per scope are shown to the model and deduped against."""

REFERENCE_LENGTH = 12
"""Content-hash prefix length used as the model-facing memory reference."""


def reference_token(memory: Memory) -> str:
    """Return the short, stable reference the prompt shows for ``memory``."""

    if memory.content_hash is not None:
        return memory.content_hash[:REFERENCE_LENGTH]
    return memory.memory_id


def render_existing(existing: dict[str, Memory]) -> str:
    """Render stored memories as ``[reference] (scope) content`` lines."""

    lines = [
        f"[{token}] ({memory.scope.value}) {memory.content}" for token, memory in existing.items()
    ]
    return "\n".join(lines) if lines else NO_MEMORIES_PLACEHOLDER


@dataclass(frozen=True, slots=True)
class _WritePlan:
    """Everything the per-fact write step needs beyond the fact itself."""

    existing: dict[str, Memory]
    hashes: set[str]
    messages: tuple[ConversationMessage, ...]


class MemoryExtractor:
    """Turn a conversation into long-term memories without ever losing history.

    ``extract`` never raises into the caller: a model, repository, or index
    failure is logged at WARNING and whatever was already saved is returned,
    so a partially extracted turn still commits what it learned.
    """

    def __init__(  # noqa: PLR0913 - one keyword-only collaborator per injected port
        self,
        *,
        policy: MemoryPolicy,
        memories: MemoryRepository,
        model: BaseChatModel,
        index: MemoryIndex | None = None,
        embedder: MemoryEmbedder | None = None,
        entities: MemoryEntityResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._policy = policy
        self._memories = memories
        self._model = model
        self._index = index
        self._embedder = embedder
        self._entities = entities
        self._clock: Callable[[], datetime] = clock or utc_now

    async def extract(
        self,
        owner: MemoryOwner,
        *,
        messages: Sequence[ConversationMessage],
    ) -> tuple[Memory, ...]:
        """Store and return the durable facts stated in ``messages``."""

        if not self._policy.enabled or not messages:
            return ()
        try:
            existing = await self._existing(owner)
            facts = await self._propose(existing, messages)
        except Exception:
            logger.warning("extracting long-term memories failed", exc_info=True)
            return ()
        plan = _WritePlan(
            existing=existing,
            hashes={memory.content_hash for memory in existing.values() if memory.content_hash},
            messages=tuple(messages),
        )
        return await self._write(owner, facts, plan)

    async def _write(
        self,
        owner: MemoryOwner,
        facts: Sequence[ExtractedFact],
        plan: _WritePlan,
    ) -> tuple[Memory, ...]:
        """Store each surviving fact, returning early on the first failure."""

        saved: list[Memory] = []
        for fact in facts:
            try:
                stored = await self._store(owner, fact, plan)
            except Exception:
                logger.warning("storing an extracted memory failed", exc_info=True)
                return tuple(saved)
            if stored is not None:
                saved.append(stored)
                if stored.content_hash is not None:
                    plan.hashes.add(stored.content_hash)
        return tuple(saved)

    async def _store(
        self,
        owner: MemoryOwner,
        fact: ExtractedFact,
        plan: _WritePlan,
    ) -> Memory | None:
        """Store one fact, or return ``None`` when it is a duplicate or unscopable."""

        scope = self._scope(owner, fact.scope)
        if scope is None:
            return None
        digest = content_hash(fact.content, scope)
        if digest in plan.hashes:
            return None
        if await self._is_restatement(owner, scope, fact.content):
            return None
        anchored = await anchored_fact(
            fact,
            resolver=self._entities,
            tenant_id=owner.tenant_id,
            confidence_floor=self._policy.entity_confidence_floor,
        )
        memory = self._memory(owner, anchored, scope=scope, digest=digest, messages=plan.messages)
        await self._memories.save(memory)
        await self._index_memory(memory)
        await self._supersede(fact, memory, plan.existing)
        return memory

    def _scope(self, owner: MemoryOwner, requested: MemoryScope) -> MemoryScope | None:
        """Return the scope this owner can actually be written at, never wider.

        A project fact proposed by an owner with no ``project_id`` degrades to
        the session rather than being stored where a whole project would read
        it; an owner that cannot even address its session stores nothing.
        """

        for candidate in (requested, MemoryScope.SESSION):
            if scope_query_owner(owner, candidate) is not None:
                return candidate
        return None

    def _memory(
        self,
        owner: MemoryOwner,
        fact: ExtractedFact,
        *,
        scope: MemoryScope,
        digest: str,
        messages: tuple[ConversationMessage, ...],
    ) -> Memory:
        """Shape one extracted fact into the memory row to save.

        ``fact.entities`` is stored on ``entity_ids`` as-is: it is already
        either the resolved knowledge-graph node ids, or -- with no resolver
        wired -- the mentions the model proposed.
        """

        now = self._clock()
        resolved = recall_owner(owner)
        return Memory(
            memory_id=new_memory_id(),
            scope=scope,
            memory_type=fact.memory_type,
            owner=resolved,
            content=fact.content,
            importance=fact.importance,
            created_at=now,
            updated_at=now,
            valid_from=now,
            source_session_id=resolved.session_id,
            source_message_ids=tuple(message.message_id for message in messages),
            entity_ids=fact.entities,
            content_hash=digest,
        )

    async def _propose(
        self,
        existing: dict[str, Memory],
        messages: Sequence[ConversationMessage],
    ) -> tuple[ExtractedFact, ...]:
        """Ask the model for atomic facts and coerce them into range."""

        payload = await propose_facts(
            self._model,
            EXTRACTION_USER_TEMPLATE.format(
                existing=render_existing(existing),
                transcript=render_transcript(messages),
            ),
        )
        if payload is None:
            return ()
        return extracted_facts(
            payload,
            min_importance=self._policy.extraction_min_importance,
        )

    async def _existing(self, owner: MemoryOwner) -> dict[str, Memory]:
        """Return the memories valid now, keyed by their prompt reference."""

        now = self._clock()
        found: dict[str, Memory] = {}
        for scope in EXTRACTION_SCOPE_ORDER:
            scoped = scope_query_owner(owner, scope)
            if scoped is None:
                continue
            query = MemoryQuery(
                owner=scoped,
                scopes=(scope,),
                memory_types=RECALL_MEMORY_TYPES,
                limit=EXISTING_LIMIT,
            )
            for memory in await self._memories.search(query):
                if memory.is_valid_at(now):
                    found[reference_token(memory)] = memory
        return found

    async def _is_restatement(self, owner: MemoryOwner, scope: MemoryScope, content: str) -> bool:
        """Whether the index already holds a near-identical memory in ``scope``."""

        if self._index is None or self._embedder is None:
            return False
        scoped = scope_query_owner(owner, scope)
        if scoped is None:
            return False
        query = MemoryQuery(owner=scoped, scopes=(scope,), text=content, limit=1)
        embedding = await self._embedder(content)
        matches = await self._index.search_memories(query, embedding=embedding)
        return any(match.score >= self._policy.dedup_threshold for match in matches)

    async def _index_memory(self, memory: Memory) -> None:
        """Index a freshly saved memory when a semantic index is wired."""

        if self._index is None:
            return
        embedding = await self._embedder(memory.content) if self._embedder is not None else None
        await self._index.index_memory(memory, embedding=embedding)

    async def _supersede(
        self,
        fact: ExtractedFact,
        memory: Memory,
        existing: dict[str, Memory],
    ) -> None:
        """Close the validity interval of the memory ``fact`` replaces.

        The prior row is amended, never deleted: its content stays readable
        with ``invalid_at`` set and ``superseded_by`` pointing at the new row.
        An unrecognised reference, a cross-scope reference, or a row already
        superseded is ignored.
        """

        if fact.replaces is None:
            return
        prior = existing.get(fact.replaces)
        if prior is None or prior.scope is not memory.scope or prior.invalid_at is not None:
            return
        now = self._clock()
        if prior.valid_from is not None and now <= prior.valid_from:
            logger.debug("skipping supersession of a memory that is not older than the new one")
            return
        await self._memories.save(
            replace(prior, invalid_at=now, superseded_by=memory.memory_id, updated_at=now)
        )


__all__ = [
    "EXISTING_LIMIT",
    "EXTRACTION_SCOPE_ORDER",
    "ExtractedFact",
    "MemoryExtractor",
    "reference_token",
    "render_existing",
]
