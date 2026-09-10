"""Execute the agent's memory tools against one server-bound owner.

The owner -- tenant, project, user, session -- is fixed when these tools are
constructed, from the authenticated run that is executing. Nothing a model
says can move it: the schemas expose no owner field, and ``reject_owner_fields``
turns a spoofed one into an error instead of letting a transport that skipped
schema validation slip it past. That is the same posture the retrieval tools
take with ``tenant_id`` inside ``filters``, applied to every owner field.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import TYPE_CHECKING

from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    MemoryType,
    new_memory_id,
    scope_owner_fields,
)
from harborrag_memory import MemoryPolicy, MemoryRecall

if TYPE_CHECKING:
    from harborrag_core.ports.memory import MemoryEmbedder, MemoryIndex, MemoryRepository

logger = logging.getLogger("harborrag.runtime.agent.tools")

# Every field that identifies *whose* memory this is. A model supplying any of
# them is trying to read or write as somebody else, so it is an error and not a
# silently dropped key.
OWNER_ARGUMENT_NAMES = frozenset(
    {"tenant_id", "project_id", "user_id", "principal_id", "session_id", "run_id", "owner"}
)

READABLE_SCOPES = (
    MemoryScope.TENANT,
    MemoryScope.PROJECT,
    MemoryScope.USER,
    MemoryScope.SESSION,
)
"""Scopes recall may be narrowed to, broadest first."""

WRITABLE_SCOPES = (MemoryScope.PROJECT, MemoryScope.USER, MemoryScope.SESSION)
"""Scopes the agent may write, broadest first.

``TENANT`` is readable but deliberately absent: because a requested scope only
ever *narrows* and every owner can address ``TENANT``, an agent allowed to ask
for it would always get it -- so one user's session could state a fact about
the whole organization. Widening is never a fallback, and this is the ceiling
that makes that true.
"""


def reject_owner_fields(values: dict[str, object]) -> None:
    """Raise when model-supplied arguments carry any owner identity."""

    present = sorted(OWNER_ARGUMENT_NAMES & values.keys())
    if present:
        raise ValueError(f"{', '.join(present)} is bound by the server and must not be supplied")


def _addressable(owner: MemoryOwner, scope: MemoryScope) -> bool:
    """Whether ``owner`` holds every field ``scope`` keys on."""

    return all(getattr(owner, name) is not None for name in scope_owner_fields(scope))


def memory_view(memory: Memory) -> dict[str, object]:
    """Project one recalled memory to what an agent can read and act on.

    ``entity_ids`` are knowledge-graph node keys, so a recalled memory can be
    handed straight to the graph tools. Owner fields are deliberately absent:
    the agent already knows whose memory it asked for.
    """

    valid_from: datetime | None = memory.valid_from or memory.created_at
    return {
        "memory_id": memory.memory_id,
        "scope": memory.scope.value,
        "memory_type": memory.memory_type.value,
        "content": memory.content,
        "valid_from": valid_from.isoformat() if valid_from is not None else None,
        "source_session_id": memory.source_session_id,
        "entity_ids": list(memory.entity_ids),
    }


@dataclass(frozen=True, slots=True)
class AgentMemoryTools:
    """Read and record long-term memory for exactly one bound owner."""

    owner: MemoryOwner
    memories: MemoryRepository
    policy: MemoryPolicy
    index: MemoryIndex | None = None
    embedder: MemoryEmbedder | None = None

    async def search(self, values: dict[str, object]) -> dict[str, object]:
        """Recall the bound owner's memories for a model-supplied query.

        Ranking is the same production recall the chat path uses, so what an
        agent sees here matches what a prompt would have been given: index
        first, repository substring search as the fallback, then relevance,
        recency, and importance.
        """

        reject_owner_fields(values)
        query = _text(values, "query")
        scope = _scope(values, default=None, allowed=READABLE_SCOPES)
        limit = _integer(values, "limit", default=5, maximum=10)
        policy = replace(
            self.policy,
            recall_top_k=limit,
            recall_scopes=(scope,) if scope is not None else self.policy.recall_scopes,
        )
        recall = MemoryRecall(
            policy=policy,
            memories=self.memories,
            index=self.index,
            embedder=self.embedder,
        )
        recalled = await recall.recall(self.owner, query)
        return {"ok": True, "memories": [memory_view(memory) for memory in recalled]}

    async def manage(self, values: dict[str, object]) -> dict[str, object]:
        """Record one durable fact for the bound owner, add-only.

        The requested scope degrades to the narrowest one this owner can
        address rather than widening, and an identical memory already stored in
        that scope is reported back instead of being duplicated. Indexing is
        best effort: a memory that is saved but not indexed is still recalled
        through the repository's own search.
        """

        reject_owner_fields(values)
        content = _text(values, "content")
        scope = _resolved_scope(
            self.owner,
            _scope(values, default=MemoryScope.USER, allowed=WRITABLE_SCOPES),
        )
        if scope is None:
            return {"ok": False, "error": "this caller cannot address any memory scope"}
        existing = await self._existing(scope, content)
        if existing is not None:
            return {"ok": True, "memory_id": existing.memory_id, "recorded": False}
        memory = Memory(
            memory_id=new_memory_id(),
            scope=scope,
            memory_type=MemoryType(_optional_text(values, "memory_type") or MemoryType.FACT.value),
            owner=self.owner,
            content=content,
            importance=_fraction(values, "importance", default=0.5),
            source_session_id=self.owner.session_id,
        )
        await self.memories.save(memory)
        await self._index(memory)
        return {"ok": True, "memory_id": memory.memory_id, "recorded": True}

    async def _existing(self, scope: MemoryScope, content: str) -> Memory | None:
        """The bound owner's already-stored memory with exactly this content, if any."""

        found = await self.memories.search(
            MemoryQuery(owner=self.owner, scopes=(scope,), text=content, limit=5)
        )
        return next((memory for memory in found if memory.content == content), None)

    async def _index(self, memory: Memory) -> None:
        """Add the memory to the semantic index, or log and move on."""

        if self.index is None:
            return
        try:
            embedding = await self.embedder(memory.content) if self.embedder is not None else None
            await self.index.index_memory(memory, embedding=embedding)
        except Exception:  # noqa: BLE001 - the canonical write already succeeded
            logger.warning(
                "Indexing an agent-recorded memory failed for tenant=%s scope=%s; "
                "it stays recallable through the repository",
                self.owner.tenant_id,
                memory.scope.value,
                exc_info=True,
            )


def _resolved_scope(owner: MemoryOwner, requested: MemoryScope | None) -> MemoryScope | None:
    """The narrowest addressable writable scope at or inside ``requested``.

    ``None`` when the owner can address none of them, which is the only honest
    answer: widening past ``requested`` is exactly what must not happen.
    """

    start = WRITABLE_SCOPES.index(requested or MemoryScope.USER)
    return next(
        (scope for scope in WRITABLE_SCOPES[start:] if _addressable(owner, scope)),
        None,
    )


def _scope(
    values: dict[str, object],
    *,
    default: MemoryScope | None,
    allowed: tuple[MemoryScope, ...],
) -> MemoryScope | None:
    """The requested scope, rejected unless it is one this operation allows."""

    name = _optional_text(values, "scope")
    if name is None:
        return default
    scope = MemoryScope(name)
    if scope not in allowed:
        raise ValueError(f"scope must be one of {[item.value for item in allowed]}")
    return scope


def _text(values: dict[str, object], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(values: dict[str, object], name: str) -> str | None:
    return _text(values, name) if values.get(name) is not None else None


def _integer(values: dict[str, object], name: str, *, default: int, maximum: int) -> int:
    value = values.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _fraction(values: dict[str, object], name: str, *, default: float) -> float:
    value = values.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)


__all__ = [
    "OWNER_ARGUMENT_NAMES",
    "READABLE_SCOPES",
    "WRITABLE_SCOPES",
    "AgentMemoryTools",
    "memory_view",
    "reject_owner_fields",
]
