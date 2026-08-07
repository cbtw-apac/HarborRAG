"""Working memory tier: per-agent-run scratch state, cleared on TTL expiry.

``WorkingMemoryStore`` is a Protocol, not a concrete store, so a Redis-backed
implementation can live in ``harborrag-adapters`` and satisfy it structurally
-- neither package needs to import the other. ``InMemoryWorkingMemoryStore``
below has no dependencies beyond the core owner type, so it ships here as
the zero-config default and the fixture used by tests.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from harborrag_core.ports.memory import MemoryOwner

_DEFAULT_TTL_SECONDS = 3600


def _require_run_owner(owner: MemoryOwner) -> None:
    if owner.run_id is None:
        raise ValueError("working memory requires owner.run_id")


class WorkingMemoryStore(Protocol):
    """Persistence-neutral contract for one run's scratch state."""

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None: ...

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None: ...

    async def delete(self, owner: MemoryOwner) -> None: ...


class InMemoryWorkingMemoryStore:
    """Process-local ``WorkingMemoryStore`` for tests and unconfigured runtimes."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[dict[str, Any], float | None]] = {}

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None:
        _require_run_owner(owner)
        entry = self._entries.get(_key(owner))
        if entry is None:
            return None
        state, expires_at = entry
        if expires_at is not None and expires_at <= time.monotonic():
            del self._entries[_key(owner)]
            return None
        return dict(state)

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None:
        _require_run_owner(owner)
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds > 0 else None
        self._entries[_key(owner)] = (dict(state), expires_at)

    async def delete(self, owner: MemoryOwner) -> None:
        _require_run_owner(owner)
        self._entries.pop(_key(owner), None)


def _key(owner: MemoryOwner) -> str:
    return f"{owner.tenant_id}:{owner.principal_id}:{owner.session_id}:{owner.run_id}"


class WorkingMemory:
    """Scratch state scoped to one agent run, expiring after its TTL."""

    def __init__(self, store: WorkingMemoryStore, *, default_ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._store = store
        self._default_ttl_seconds = default_ttl_seconds

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        _require_run_owner(owner)
        state = await self._store.get(owner)
        return state or {}

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        _require_run_owner(owner)
        await self._store.put(owner, state, ttl_seconds=ttl_seconds or self._default_ttl_seconds)

    async def clear(self, owner: MemoryOwner) -> None:
        _require_run_owner(owner)
        await self._store.delete(owner)


__all__ = [
    "InMemoryWorkingMemoryStore",
    "WorkingMemory",
    "WorkingMemoryStore",
]
