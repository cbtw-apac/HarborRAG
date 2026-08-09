"""Scope-aware memory port shared by chat, agent, and persistence adapters.

A ``Memory`` belongs to one ``MemoryOwner`` (the isolation key of the caller
that wrote it) and is tagged with the ``MemoryScope`` at which it should be
visible. ``visible_to`` is the single source of truth for whether a stored
memory may be returned to a given caller; every adapter's ``search`` must
apply the equivalent filter at the storage layer, but this pure function is
what makes that filter's correctness testable without any storage at all.
"""

from __future__ import annotations

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
    MemoryScope.USER: ("tenant_id", "principal_id"),
    MemoryScope.SESSION: ("tenant_id", "principal_id", "session_id"),
    MemoryScope.RUN: ("tenant_id", "principal_id", "session_id", "run_id"),
}


@dataclass(frozen=True, slots=True)
class MemoryOwner:
    """Isolation key a memory is written under or a query is issued as.

    Which fields are load-bearing for a given memory depends on its
    ``MemoryScope`` -- a ``TENANT``-scoped memory only requires
    ``tenant_id`` to match, while a ``RUN``-scoped memory requires every
    field through ``run_id``.
    """

    tenant_id: str
    project_id: str | None = None
    principal_id: str | None = None
    session_id: str | None = None
    run_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "project_id", "principal_id", "session_id", "run_id"):
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


@dataclass(frozen=True, slots=True)
class Memory:
    """One canonical memory record."""

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

    def __post_init__(self) -> None:
        if not self.memory_id.strip() or not self.content.strip():
            raise ValueError("memory id and content must be non-empty")
        if not isfinite(self.importance) or not 0.0 <= self.importance <= 1.0:
            raise ValueError("memory importance must be finite and between zero and one")
        for name in ("created_at", "updated_at", "expires_at"):
            value = getattr(self, name)
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"memory {name} must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("memory updated_at must not precede created_at")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("memory expires_at must follow created_at")
        object.__setattr__(self, "metadata", FrozenMetadata(self.metadata))


@dataclass(frozen=True, slots=True)
class MemoryQuery:
    """A caller-scoped lookup: ``owner`` is always the caller's own isolation key."""

    owner: MemoryOwner
    scopes: tuple[MemoryScope, ...] = ()
    memory_types: tuple[MemoryType, ...] = ()
    text: str | None = None
    limit: int = 20

    def __post_init__(self) -> None:
        if not 1 <= self.limit <= 1000:
            raise ValueError("memory query limit must be between 1 and 1000")
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


__all__ = [
    "Memory",
    "MemoryOwner",
    "MemoryQuery",
    "MemoryRepository",
    "MemoryScope",
    "MemoryType",
    "new_memory_id",
    "scope_owner_fields",
    "visible_to",
]
