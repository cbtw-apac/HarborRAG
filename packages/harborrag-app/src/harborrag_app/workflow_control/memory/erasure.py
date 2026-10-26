"""Durable deletion intents retain identifiers until every store is erased."""

from __future__ import annotations

import logging
from dataclasses import asdict
from uuid import uuid4

from harborrag_core.contracts.errors import HarborUnavailableError
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.ports.control_plane import PendingEffectRepositoryPort
from harborrag_core.ports.memory import Memory, MemoryIndex, MemoryOwner, MemoryRepository

logger = logging.getLogger("harborrag.app.workflow_control.memory.administration")
MEMORY_ERASURE = "erase_memory"
SESSION_ERASURE = "erase_memory_session"


class MemoryErasureJournal:
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
        if self.pending is None:
            return None
        effect = PendingControlPlaneEffect(
            id=f"eff_{uuid4().hex}", kind=kind, payload={"owner": asdict(owner), **fields}
        )
        # Failure must propagate before any deletion loses the recovery identifiers.
        await self.pending.enqueue(effect)
        return effect.id

    async def complete(self, effect_id: str | None) -> None:
        if effect_id is not None and self.pending is not None:
            await self.pending.complete(effect_id)

    async def erase_memory(self, memory: Memory) -> int:
        effect_id = await self.begin(
            MEMORY_ERASURE,
            memory.owner,
            memory_id=memory.memory_id,
            index_required=self.index is not None,
        )
        if self.pending is None:
            # Without a journal, preserve the canonical row if the index is unavailable.
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
        if effect.kind != MEMORY_ERASURE:
            return False
        owner = MemoryOwner(**effect.payload["owner"])
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
