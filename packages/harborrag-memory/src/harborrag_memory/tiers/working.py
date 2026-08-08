from __future__ import annotations

from typing import Any, Protocol

from harborrag_core.ports.memory import MemoryOwner


class WorkingMemoryStore(Protocol):
    """Persistence-neutral contract for one run's scratch state."""

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None: ...

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None: ...

    async def delete(self, owner: MemoryOwner) -> None: ...


class InMemoryWorkingMemoryStore:
    """Process-local ``WorkingMemoryStore`` for tests and unconfigured runtimes."""

    def __init__(self) -> None:
        # TODO: set up process-local storage for scratch state.
        pass

    async def get(self, owner: MemoryOwner) -> dict[str, Any] | None:
        # TODO: return the stored state for this owner, honoring TTL expiry.
        raise NotImplementedError("TODO: implement InMemoryWorkingMemoryStore.get")

    async def put(self, owner: MemoryOwner, state: dict[str, Any], *, ttl_seconds: int) -> None:
        # TODO: store state for this owner with the given TTL.
        raise NotImplementedError("TODO: implement InMemoryWorkingMemoryStore.put")

    async def delete(self, owner: MemoryOwner) -> None:
        # TODO: remove the stored state for this owner.
        raise NotImplementedError("TODO: implement InMemoryWorkingMemoryStore.delete")


class WorkingMemory:
    """Scratch state scoped to one agent run, expiring after its TTL."""

    def __init__(self, store: WorkingMemoryStore, *, default_ttl_seconds: int = 3600) -> None:
        # TODO: wire the working memory store dependency.
        pass

    async def scratch(self, owner: MemoryOwner) -> dict[str, Any]:
        # TODO: return the current scratch state for this owner.
        raise NotImplementedError("TODO: implement WorkingMemory.scratch")

    async def update(
        self,
        owner: MemoryOwner,
        state: dict[str, Any],
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        # TODO: persist the updated scratch state for this owner.
        raise NotImplementedError("TODO: implement WorkingMemory.update")

    async def clear(self, owner: MemoryOwner) -> None:
        # TODO: remove the scratch state for this owner.
        raise NotImplementedError("TODO: implement WorkingMemory.clear")