"""Durable deletion intents retain identifiers until every store is erased."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, fields
from typing import Any
from uuid import uuid4

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.ports.control_plane import PendingEffectRepositoryPort
from harborrag_core.ports.memory import Memory, MemoryIndex, MemoryOwner, MemoryRepository

logger = logging.getLogger("harborrag.memory.erasure")
MEMORY_ERASURE = "erase_memory"
SESSION_ERASURE = "erase_memory_session"
_OWNER_FIELDS = frozenset(field.name for field in fields(MemoryOwner))


def stored_owner(payload: Mapping[str, Any]) -> MemoryOwner:
    """Rebuild the owner an erasure intent recorded.

    Args:
        payload: The ``owner`` mapping of a pending effect.

    Returns:
        The owner, ignoring fields an earlier release wrote (``run_id``).
    """
    return MemoryOwner(**{key: value for key, value in payload.items() if key in _OWNER_FIELDS})


class MemoryErasureJournal:
    """Record erasure intents durably so an interrupted deletion can be replayed.

    Args:
        memories: Canonical memory rows, or None when long-term memory is off.
        index: Vector index for memories, or None when none is configured.
        pending: Journal of pending effects, or None to erase without recovery.
    """

    def __init__(
        self,
        memories: MemoryRepository | None,
        index: MemoryIndex | None,
        pending: PendingEffectRepositoryPort | None,
    ) -> None:
        self.memories = memories
        self.index = index
        self.pending = pending

    async def begin(self, kind: str, owner: MemoryOwner, **fields: object) -> str | None:
        """Enqueue an erasure intent and return its id, or None when no journal exists.

        The enqueue failure propagates before any deletion loses the recovery identifiers.
        """
        if self.pending is None:
            return None
        effect = PendingControlPlaneEffect(
            id=f"eff_{uuid4().hex}", kind=kind, payload={"owner": asdict(owner), **fields}
        )
        await self.pending.enqueue(effect)
        return effect.id

    async def complete(self, effect_id: str | None) -> None:
        """Mark an erasure intent done; a missing id or journal is a no-op."""
        if effect_id is not None and self.pending is not None:
            await self.pending.complete(effect_id)

    async def erase_memory(self, memory: Memory) -> int:
        """Delete one memory row and its index point, returning the points removed.

        Without a journal the index is cleared first, so the canonical row survives an
        unavailable index; with one, an index failure queues the point for retry and returns 0.
        """
        effect_id = await self.begin(
            MEMORY_ERASURE,
            memory.owner,
            memory_id=memory.memory_id,
            index_required=self.index is not None,
        )
        if self.pending is None:
            count = await self._unindex(memory.owner, memory.memory_id)
            if self.memories is not None:
                await self.memories.delete(memory.owner, memory.memory_id)
            return count
        if self.memories is not None:
            await self.memories.delete(memory.owner, memory.memory_id)
        try:
            count = await self._unindex(memory.owner, memory.memory_id)
        except Exception:
            logger.warning(
                "Memory index delete failed for tenant=%s memory_id=%s; queued for retry",
                memory.owner.tenant_id,
                memory.memory_id,
            )
            return 0
        await self.complete(effect_id)
        return count

    async def replay(self, effect: PendingControlPlaneEffect) -> bool:
        """Re-run a recorded memory erasure; return False for any other effect kind.

        Raises:
            HarborUnavailableError: The stores the intent needs are not configured.
        """
        if effect.kind != MEMORY_ERASURE:
            return False
        owner = stored_owner(effect.payload["owner"])
        memory_id = effect.payload["memory_id"]
        if self.memories is None or (effect.payload.get("index_required") and self.index is None):
            raise HarborUnavailableError("memory erasure stores are not configured")
        await self.memories.delete(owner, memory_id)
        await self._unindex(owner, memory_id)
        return True

    async def _unindex(self, owner: MemoryOwner, memory_id: str) -> int:
        if self.index is None:
            return 0
        await self.index.delete_memory(owner, memory_id)
        return 1
