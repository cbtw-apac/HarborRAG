"""Scope-aware memory port shared by chat, agent, and persistence adapters.

A ``Memory`` belongs to one ``MemoryOwner`` (the isolation key of the caller
that wrote it) and is tagged with the ``MemoryScope`` at which it should be
visible. ``visible_to`` is the single source of truth for whether a stored
memory may be returned to a given caller; every adapter's ``search`` must
apply the equivalent filter at the storage layer, but this pure function is
what makes that filter's correctness testable without any storage at all.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from math import isfinite
from typing import Any, Protocol
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.chunking.metadata import FrozenMetadata


def new_memory_id() -> str:
    """Generate one API-safe opaque memory identifier."""

    return f"mem-{uuid4().hex}"


class MemoryType(StrEnum):
    """What kind of thing a memory records, independent of where it applies."""

    CONVERSATION = "conversation"
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"
    EPISODE = "episode"
    SUMMARY = "summary"
    WORKING = "working"


class MemoryScope(StrEnum):
    """How broadly a memory applies, narrowest to broadest isolation key."""

    RUN = "run"
    SESSION = "session"
    USER = "user"
    PROJECT = "project"
    TENANT = "tenant"
    GLOBAL = "global"


_SCOPE_OWNER_FIELDS: dict[MemoryScope, tuple[str, ...]] = {
    MemoryScope.GLOBAL: (),
    MemoryScope.TENANT: ("tenant_id",),
    MemoryScope.PROJECT: ("tenant_id", "project_id"),
    MemoryScope.USER: ("tenant_id", "user_id"),
    MemoryScope.SESSION: ("tenant_id", "user_id", "session_id"),
    MemoryScope.RUN: ("tenant_id", "user_id", "session_id", "run_id"),
}

_OWNER_OPTIONAL_FIELDS = ("project_id", "user_id", "principal_id", "session_id", "run_id")


@dataclass(frozen=True, slots=True)
class MemoryOwner:
    """Isolation key a memory is written under or a query is issued as.

    Which fields are load-bearing for a given memory depends on its
    ``MemoryScope`` -- a ``TENANT``-scoped memory only requires
    ``tenant_id`` to match, while a ``RUN``-scoped memory requires every
    field through ``run_id``. ``user_id`` is the stable identity of the human
    the memory is about and keys the ``USER``/``SESSION``/``RUN`` scopes;
    ``principal_id`` is the authenticated credential that wrote it and stays
    the ownership key for conversation sessions.
    """

    tenant_id: str
    project_id: str | None = None
    user_id: str | None = None
    principal_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", *_OWNER_OPTIONAL_FIELDS):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"memory owner {name} must be non-empty")


def scope_owner_fields(scope: MemoryScope) -> tuple[str, ...]:
    """Owner fields that must match for a memory at ``scope`` to be visible."""

    return _SCOPE_OWNER_FIELDS[scope]


def visible_to(memory_scope: MemoryScope, memory_owner: MemoryOwner, caller: MemoryOwner) -> bool:
    """Whether a memory stored at ``memory_owner``/``memory_scope`` is visible to ``caller``.

    Every field ``memory_scope`` requires (see ``scope_owner_fields``) must be
    present on both owners and equal -- a caller missing a required field
    (e.g. searching without a ``run_id``) never matches a ``RUN``-scoped
    memory, and neither does a caller in a different tenant, project,
    session, or run.
    """

    return all(
        getattr(memory_owner, name) is not None
        and getattr(memory_owner, name) == getattr(caller, name)
        for name in scope_owner_fields(memory_scope)
    )


def _require_aware(prefix: str, name: str, value: datetime | None) -> None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{prefix} {name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class Memory:
    """One canonical memory record.

    ``expires_at`` is retention (the row is garbage after it); ``valid_from``
    / ``invalid_at`` are the bitemporal window during which the fact held
    true in the world, so a superseded fact stays readable as history with
    ``superseded_by`` pointing at its replacement. ``source_session_id`` /
    ``source_message_ids`` record the conversation the memory was extracted
    from; ``content_hash`` lets extraction dedupe restatements.
    """

    memory_id: str
    scope: MemoryScope
    memory_type: MemoryType
    owner: MemoryOwner
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None
    valid_from: datetime | None = None
    invalid_at: datetime | None = None
    superseded_by: str | None = None
    source_session_id: str | None = None
    source_message_ids: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if not self.memory_id.strip() or not self.content.strip():
            raise ValueError("memory id and content must be non-empty")
        if not isfinite(self.importance) or not 0.0 <= self.importance <= 1.0:
            raise ValueError("memory importance must be finite and between zero and one")
        for name in ("created_at", "updated_at", "expires_at", "valid_from", "invalid_at"):
            _require_aware("memory", name, getattr(self, name))
        if self.updated_at < self.created_at:
            raise ValueError("memory updated_at must not precede created_at")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("memory expires_at must follow created_at")
        if (
            self.valid_from is not None
            and self.invalid_at is not None
            and self.invalid_at <= self.valid_from
        ):
            raise ValueError("memory invalid_at must follow valid_from")
        for name in ("superseded_by", "source_session_id", "content_hash"):
            value = getattr(self, name)
            if value is not None and not value.strip():
                raise ValueError(f"memory {name} must be non-empty when supplied")
        object.__setattr__(self, "metadata", FrozenMetadata(self.metadata))

    def is_valid_at(self, now: datetime) -> bool:
        """Whether the fact held true at ``now`` per ``valid_from``/``invalid_at``.

        Retention (``expires_at``) is deliberately not part of this check.
        """

        if self.valid_from is not None and now < self.valid_from:
            return False
        return self.invalid_at is None or now < self.invalid_at


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """A caller-scoped lookup: ``owner`` is always the caller's own isolation key.

    By default only memories valid now (``Memory.is_valid_at``) are returned;
    ``as_of`` evaluates validity at that instant instead, and
    ``include_invalid`` disables the validity filter altogether. Expired
    (``expires_at``) memories are never returned.
    """

    owner: MemoryOwner
    scopes: tuple[MemoryScope, ...] = ()
    memory_types: tuple[MemoryType, ...] = ()
    text: str | None = None
    limit: int = 20
    include_invalid: bool = False
    as_of: datetime | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("memory query limit must be between 1 and 1000")
        _require_aware("memory query", "as_of", self.as_of)
        if self.text is not None and not self.text.strip():
            raise ValueError("memory query text must be non-empty when supplied")
        if len(set(self.scopes)) != len(self.scopes):
            raise ValueError("memory query scopes must be unique")
        if len(set(self.memory_types)) != len(self.memory_types):
            raise ValueError("memory query types must be unique")


class MemoryRepository(Protocol):
    """Persistence-neutral contract for the canonical long-term memory store."""

    async def save(self, memory: Memory) -> None: ...

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None: ...

    async def search(self, query: MemoryQuery) -> tuple[Memory, ...]: ...

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryMatch:
    """One index hit: the memory's identity plus its fused relevance.

    The index never returns memory content -- the canonical store
    (``MemoryRepository``) stays the system of record, so recall hydrates
    these ids through it. ``score`` is the fused hybrid relevance the index
    computed, higher is better, and is deliberately unbounded: different
    backends fuse lanes on different scales.
    """

    memory_id: str
    score: float

    def __post_init__(self) -> None:
        if not self.memory_id.strip():
            raise ValueError("memory match id must be non-empty")
        if not isfinite(self.score):
            raise ValueError("memory match score must be finite")


@dataclass(frozen=True, slots=True)
class ResolvedEntity:
    """One entity mention resolved onto a knowledge-graph node.

    ``mention`` is the surface form that was proposed (typically by an
    extraction model reading a conversation), ``entity_id`` the curated graph
    node it denotes, and ``confidence`` how sure the resolver is, 0..1. This
    is a *link*, never a write: conversation memory is only ever joined to the
    document knowledge graph by id, never merged into it.
    """

    mention: str
    entity_id: str
    confidence: float

    def __post_init__(self) -> None:
        if not self.mention.strip() or not self.entity_id.strip():
            raise ValueError("resolved entity mention and entity id must be non-empty")
        if not isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise ValueError("resolved entity confidence must be finite and between zero and one")


class MemoryEntityResolver(Protocol):
    """Resolve proposed entity mentions to curated knowledge-graph node ids.

    Resolution is tenant-scoped and best-effort: a mention the graph does not
    know is simply absent from the result, and callers must treat a raising
    resolver as "no anchors" rather than a failed turn.
    """

    async def resolve_entities(
        self, mentions: Sequence[str], *, tenant_id: str
    ) -> tuple[ResolvedEntity, ...]: ...


type MemoryEmbedder = Callable[[str], Awaitable[Sequence[float]]]
"""Embed one piece of memory text, injected so ports stay model-agnostic."""


class MemoryIndex(Protocol):
    """Semantic-recall contract over the canonical memories, ids and scores only.

    ``embedding`` is optional on both write and read: a caller that already
    embedded the text passes it through, otherwise the adapter is expected to
    embed the memory's content (or the query's ``text``) itself. Filtering is
    the same scope rule ``visible_to`` applies, enforced at the index.
    """

    async def index_memory(
        self,
        memory: Memory,
        *,
        embedding: Sequence[float] | None = None,
    ) -> None: ...

    async def search_memories(
        self,
        query: MemoryQuery,
        *,
        embedding: Sequence[float] | None = None,
    ) -> tuple[MemoryMatch, ...]: ...

    async def delete_memory(self, owner: MemoryOwner, memory_id: str) -> None: ...


__all__ = [
    "Memory",
    "MemoryEmbedder",
    "MemoryEntityResolver",
    "MemoryIndex",
    "MemoryMatch",
    "MemoryOwner",
    "MemoryQuery",
    "MemoryRepository",
    "MemoryScope",
    "MemoryType",
    "ResolvedEntity",
    "new_memory_id",
    "scope_owner_fields",
    "visible_to",
]
