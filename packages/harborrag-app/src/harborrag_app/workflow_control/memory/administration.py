"""Introspection and right-to-erasure over one caller's remembered data.

Erasure spans three stores that are keyed differently: conversation history by
``(tenant, principal, session)``, canonical memories by the scope's owner
fields (``user_id`` for USER, plus ``session_id`` for SESSION), and the vector
index by ``(owner, memory_id)``. ``MemoryOwner`` is the only place the
principal and the end-user identity coexist, so it is the bridge: memories are
found by ``user_id`` and each row's owner then names the principal and session
whose messages must go.

That has one honest consequence, surfaced in the erasure report: sessions are
discovered *through* the memories they produced. A conversation that never
produced a user-scoped memory is not reachable from a ``user_id`` alone, and
its messages are erased through the session endpoint instead. Nothing here
enumerates sessions, because no port does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import HarborNotFoundError, HarborUnavailableError
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import MemoryOwner, MemoryQuery, MemoryScope

if TYPE_CHECKING:
    from harborrag_core.ports.agent_runs import AgentRunRepository
    from harborrag_core.ports.conversation import ConversationHistoryRepository
    from harborrag_core.ports.memory import Memory, MemoryIndex, MemoryRepository

logger = logging.getLogger("harborrag.app.workflow_control.memory.administration")

_PAGE = 1_000
# Bounds a pathological purge (a store growing while it is erased) instead of
# spinning forever; 50 pages is 50k memories for one user.
_MAX_PASSES = 50
_USER_SCOPES = (MemoryScope.USER,)
_SESSION_SCOPES = (MemoryScope.SESSION,)
_NO_STORE = "long-term memory is not configured for this deployment"


@dataclass(frozen=True, slots=True)
class _Purged:
    """One purge pass's totals, plus the conversations its rows pointed at."""

    memories: int = 0
    index_points: int = 0
    conversations: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class ErasureReport:
    """What one erasure actually removed, per category."""

    memories: int = 0
    index_points: int = 0
    sessions: int = 0
    conversation_messages_cleared: int = 0
    agent_run_checkpoints: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "memories": self.memories,
            "index_points": self.index_points,
            "sessions": self.sessions,
            "conversation_messages_cleared": self.conversation_messages_cleared,
            "agent_run_checkpoints": self.agent_run_checkpoints,
        }


class MemoryAdministrationService:
    """Read and erase the memory of one authenticated caller."""

    def __init__(
        self,
        *,
        conversations: ConversationHistoryRepository,
        memories: MemoryRepository | None = None,
        index: MemoryIndex | None = None,
        runs: AgentRunRepository | None = None,
    ) -> None:
        self._conversations = conversations
        self._memories = memories
        self._index = index
        self._runs = runs

    def _store(self) -> MemoryRepository:
        if self._memories is None:
            raise HarborUnavailableError(_NO_STORE)
        return self._memories

    async def list_memories(
        self,
        owner: MemoryOwner,
        *,
        scopes: tuple[MemoryScope, ...] = (),
        limit: int = 20,
    ) -> tuple[Memory, ...]:
        """The memories visible to ``owner``, newest and most important first.

        ``owner`` is always built from the authenticated principal, never from
        request fields, so this can only ever return the caller's own rows.
        """

        return await self._store().search(
            MemoryQuery(owner=owner, scopes=scopes, limit=limit, include_invalid=True)
        )

    async def delete_memory(self, owner: MemoryOwner, memory_id: str) -> None:
        """Delete one memory the caller can see; 404 when they cannot see it."""

        store = self._store()
        memory = await store.get(owner, memory_id)
        if memory is None:
            raise HarborNotFoundError("Memory was not found")
        await store.delete(owner, memory_id)
        await self._unindex(memory)
        logger.info(
            "Memory deleted tenant=%s actor=%s memory_id=%s scope=%s",
            owner.tenant_id,
            owner.principal_id,
            memory_id,
            memory.scope.value,
        )

    async def erase_session(self, owner: MemoryOwner, *, actor: str) -> ErasureReport:
        """Erase one session: its messages, its session memories, its vectors."""

        if owner.session_id is None or owner.principal_id is None:
            raise HarborNotFoundError("Conversation session was not found")
        identity = ConversationIdentity(
            owner.tenant_id,
            owner.principal_id,
            owner.session_id,
            owner.user_id or owner.principal_id,
        )
        if not await self._conversations.exists(identity):
            raise HarborNotFoundError("Conversation session was not found")
        # Delete the session itself, not only its messages: an emptied session
        # still exists, so it would keep appearing in the caller's
        # conversation list after they deleted it.
        await self._conversations.delete(identity)
        purged = await self._purge(owner, _SESSION_SCOPES)
        report = ErasureReport(
            memories=purged.memories,
            index_points=purged.index_points,
            sessions=1,
            conversation_messages_cleared=1,
        )
        _log_erasure("session", owner.tenant_id, actor, owner.session_id, report)
        return report

    async def erase_user(self, owner: MemoryOwner, *, actor: str) -> ErasureReport:
        """Erase everything reachable for one end user within the tenant.

        Runs in two passes because scope visibility is asymmetric: user-scoped
        memories are reachable from ``user_id`` alone and name the sessions
        they came from, and only then can the session-scoped rows (and the
        conversations themselves) be addressed.
        """

        target = owner.user_id
        if target is None:
            raise HarborNotFoundError("User was not found")
        user_owner = MemoryOwner(tenant_id=owner.tenant_id, user_id=target)
        purged = await self._purge(user_owner, _USER_SCOPES)
        memories = purged.memories
        points = purged.index_points
        for principal_id, session_id in sorted(purged.conversations):
            session_owner = MemoryOwner(
                tenant_id=owner.tenant_id,
                user_id=target,
                principal_id=principal_id,
                session_id=session_id,
            )
            session_purge = await self._purge(session_owner, _SESSION_SCOPES)
            memories += session_purge.memories
            points += session_purge.index_points
            await self._conversations.clear_messages(
                ConversationIdentity(owner.tenant_id, principal_id, session_id, target)
            )
        report = ErasureReport(
            memories=memories,
            index_points=points,
            sessions=len(purged.conversations),
            conversation_messages_cleared=len(purged.conversations),
            agent_run_checkpoints=await self._erase_checkpoints(user_owner),
        )
        _log_erasure("user", owner.tenant_id, actor, target, report)
        return report

    async def _purge(
        self,
        owner: MemoryOwner,
        scopes: tuple[MemoryScope, ...],
    ) -> _Purged:
        """Delete every memory visible to ``owner`` at ``scopes``, plus vectors.

        Each deleted row's owner and ``source_session_id`` are collected on
        the way out, so a user erasure learns which conversations produced the
        facts it just removed without a second query -- and without missing
        the pages beyond the first.
        """

        if self._memories is None:
            return _Purged()
        deleted = 0
        unindexed = 0
        conversations: set[tuple[str, str]] = set()
        for _ in range(_MAX_PASSES):
            rows = await self._memories.search(
                MemoryQuery(
                    owner=owner,
                    scopes=scopes,
                    limit=_PAGE,
                    include_invalid=True,
                )
            )
            if not rows:
                return _Purged(deleted, unindexed, frozenset(conversations))
            for row in rows:
                await self._memories.delete(owner, row.memory_id)
                deleted += 1
                unindexed += await self._unindex(row)
                principal_id = row.owner.principal_id
                session_id = row.source_session_id or row.owner.session_id
                if principal_id is not None and session_id is not None:
                    conversations.add((principal_id, session_id))
        logger.warning(
            "Memory purge stopped after %d passes for tenant=%s scopes=%s; rows may remain",
            _MAX_PASSES,
            owner.tenant_id,
            ",".join(scope.value for scope in scopes),
        )
        return _Purged(deleted, unindexed, frozenset(conversations))

    async def _unindex(self, memory: Memory) -> int:
        """Remove one memory's vector, tolerating an index that cannot."""

        if self._index is None:
            return 0
        try:
            await self._index.delete_memory(memory.owner, memory.memory_id)
        except Exception:  # noqa: BLE001 - the canonical row is already gone
            logger.warning(
                "Memory index delete failed for tenant=%s memory_id=%s; the row was removed",
                memory.owner.tenant_id,
                memory.memory_id,
            )
            return 0
        return 1

    async def _erase_checkpoints(self, owner: MemoryOwner) -> int:
        """Delete the user's agent-run checkpoints where the store supports it.

        ``AgentRunRepository`` is create/save/get only, so a store without an
        owner-scoped delete reports zero rather than pretending.
        """

        delete = getattr(self._runs, "delete_for_owner", None)
        if delete is None:
            return 0
        try:
            return int(await delete(owner))
        except Exception:  # noqa: BLE001 - erasure must report, never fail
            logger.warning(
                "Agent-run checkpoint erasure failed for tenant=%s user=%s",
                owner.tenant_id,
                owner.user_id,
            )
            return 0


def _log_erasure(
    kind: str,
    tenant_id: str,
    actor: str,
    target: str,
    report: ErasureReport,
) -> None:
    """Record one erasure with identifiers and counts only -- never content."""

    logger.info(
        "Memory erasure kind=%s tenant=%s actor=%s target=%s %s",
        kind,
        tenant_id,
        actor,
        target,
        " ".join(f"{name}={value}" for name, value in report.as_dict().items()),
    )


__all__ = ["ErasureReport", "MemoryAdministrationService"]
