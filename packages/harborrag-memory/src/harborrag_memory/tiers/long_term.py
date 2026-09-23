"""Long-term memory tier: durable, searchable memories scoped by owner."""

from __future__ import annotations

from dataclasses import fields

from harborrag_core.ports.memory import (
    Memory,
    MemoryOwner,
    MemoryQuery,
    MemoryRepository,
    MemoryScope,
    visible_to,
)

from ..errors import MemoryScopeError


class LongTermMemory:
    """Facade over the canonical long-term memory repository."""

    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository

    async def save(self, caller: MemoryOwner, memory: Memory) -> None:
        _authorize_write(caller, memory)
        await self._repository.save(memory)

    async def remember(self, caller: MemoryOwner, memory: Memory) -> None:
        """Alias for :meth:`save`."""

        await self.save(caller, memory)

    async def get(self, caller: MemoryOwner, memory_id: str) -> Memory | None:
        return await self._repository.get(caller, memory_id)

    async def search(self, caller: MemoryOwner, query: MemoryQuery) -> tuple[Memory, ...]:
        _require_same_owner(caller, query.owner)
        return await self._repository.search(query)

    async def delete(self, caller: MemoryOwner, memory_id: str) -> None:
        await self._repository.delete(caller, memory_id)

    async def forget(self, caller: MemoryOwner, memory_id: str) -> None:
        await self.delete(caller, memory_id)


def _authorize_write(caller: MemoryOwner, memory: Memory) -> None:
    if memory.scope is MemoryScope.GLOBAL:
        raise MemoryScopeError("global memory writes require an administrative capability")
    if caller.tenant_id != memory.owner.tenant_id or not visible_to(
        memory.scope, memory.owner, caller
    ):
        raise MemoryScopeError("caller is not authorized to write memory for this owner")
    _reject_borrowed_identity(caller, memory.owner)


def _reject_borrowed_identity(caller: MemoryOwner, owner: MemoryOwner) -> None:
    """Refuse a write attributed to an identity the caller does not hold.

    ``visible_to`` compares only the fields the *scope* keys on, so a
    ``TENANT``-scoped write is unconstrained beyond the tenant: any member could
    store a tenant-wide fact stamped with a colleague's ``user_id``. Erasure is
    by owner, so that row would then be deleted by the colleague's request and
    missed by the author's own. A write may leave a field unset -- that is how a
    genuinely tenant-wide fact is recorded -- but it may not claim a value the
    caller does not hold.
    """

    borrowed = sorted(
        field.name
        for field in fields(MemoryOwner)
        if field.name != "tenant_id"
        and getattr(owner, field.name) is not None
        and getattr(owner, field.name) != getattr(caller, field.name)
    )
    if borrowed:
        raise MemoryScopeError(
            "caller cannot write memory attributed to another owner: " + ", ".join(borrowed)
        )


def _require_same_owner(caller: MemoryOwner, requested: MemoryOwner) -> None:
    if caller != requested:
        raise MemoryScopeError("memory query owner must match the authenticated caller")
