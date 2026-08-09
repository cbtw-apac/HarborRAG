from __future__ import annotations

import asyncio
from copy import deepcopy
from time import monotonic
from typing import Any, Protocol

from harborrag_core.ports.memory import MemoryOwner

from ..errors import MemoryScopeError

_MAX_TTL_SECONDS = 31_536_000


class WorkingMemoryStore(Protocol):
    """Persistence-neutral contract for one run's scratch state."""

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None: ...

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None: ...

    async def delete(self, owner: MemoryOwner) -> None: ...


class InMemoryWorkingMemoryStore:
    """Process-local ``WorkingMemoryStore`` for tests and unconfigured runtimes."""

    def __init__(self) -> None:
        self._values: dict[MemoryOwner, tuple[float, dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None:
        _require_run_owner(owner)
        async with self._lock:
            entry = self._values.get(owner)
            if entry is None:
                return None
            expires_at, state = entry
            if expires_at <= monotonic():
                self._values.pop(owner, None)
                return None
            return deepcopy(state)

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None:
        _require_run_owner(owner)
        _validate_ttl(ttl_seconds)
        async with self._lock:
            self._values[owner] = (monotonic() + ttl_seconds, deepcopy(state))

    async def delete(self, owner: MemoryOwner) -> None:
        _require_run_owner(owner)
        async with self._lock:
            self._values.pop(owner, None)


class WorkingMemory:
    """Scratch state scoped to one agent run, expiring after its TTL."""

    def __init__(self, store: WorkingMemoryStore, *, default_ttl_seconds: int = 3600) -> None:
        _validate_ttl(default_ttl_seconds)
        self._store = store
        self._default_ttl_seconds = default_ttl_seconds

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        _require_run_owner(owner)
        return await self._store.get(owner) or {}

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        _require_run_owner(owner)
        selected_ttl = self._default_ttl_seconds if ttl_seconds is None else ttl_seconds
        _validate_ttl(selected_ttl)
        await self._store.put(owner, deepcopy(state), ttl_seconds=selected_ttl)

    async def clear(self, owner: MemoryOwner) -> None:
        _require_run_owner(owner)
        await self._store.delete(owner)


def _require_run_owner(owner: MemoryOwner) -> None:
    if owner.principal_id is None or owner.session_id is None or owner.run_id is None:
        raise MemoryScopeError(
            "working memory requires tenant_id, principal_id, session_id, and run_id"
        )


def _validate_ttl(ttl_seconds: int) -> None:
    if not 1 <= ttl_seconds <= _MAX_TTL_SECONDS:
        raise ValueError(f"working memory TTL must be between 1 and {_MAX_TTL_SECONDS}")
