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
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from harborrag_core.contracts.errors import (
    HarborAuthError,
    HarborNotFoundError,
    HarborUnavailableError,
)
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.domain.pending_effect import PendingControlPlaneEffect
from harborrag_core.ports.control_plane import PendingEffectRepositoryPort
from harborrag_core.ports.conversation import ConversationIdentity
from harborrag_core.ports.memory import (
    MemoryOwner,
    MemoryQuery,
    MemoryScope,
    authored_by,
    modifiable_by,
)

from .erasure import SESSION_ERASURE, ErasedMemory, MemoryErasureJournal

if TYPE_CHECKING:
    from collections.abc import Callable

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
# Scopes a user's own rows can carry that no caller-visibility query can reach
# for them: PROJECT keys on (tenant_id, project_id) and RUN needs a run_id the
# eraser does not have. Matched on the row's stored user instead.
_AUTHORED_SCOPES = (MemoryScope.PROJECT, MemoryScope.RUN, MemoryScope.TENANT)
_NO_STORE = "long-term memory is not configured for this deployment"


@dataclass(frozen=True, slots=True)
class PurgeTotals:
    """One purge's totals. ``complete`` is False when the pass limit stopped it early."""

    memories: int = 0
    index_points: int = 0
    deferred: int = 0
    complete: bool = True
    conversations: frozenset[tuple[str, str]] = frozenset()

    def __add__(self, other: PurgeTotals) -> PurgeTotals:
        return PurgeTotals(
            memories=self.memories + other.memories,
            index_points=self.index_points + other.index_points,
            deferred=self.deferred + other.deferred,
            complete=self.complete and other.complete,
            conversations=self.conversations | other.conversations,
        )


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
        pending_effects: PendingEffectRepositoryPort | None = None,
    ) -> None:
        self._conversations = conversations
        self._memories = memories
        self._index = index
        self._runs = runs
        self._erasure = MemoryErasureJournal(memories, index, pending_effects)

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

    async def delete_memory(
        self, owner: MemoryOwner, memory_id: str, *, may_delete_shared: bool = False
    ) -> None:
        """Delete one memory the caller may change; 404 when they cannot see it.

        Seeing a memory is not owning it. USER/SESSION/RUN rows are visible
        only to their own end user, so visibility settles those. A shared
        PROJECT/TENANT/GLOBAL row is visible to everyone it is shared with,
        so deleting it takes ``may_delete_shared`` (the editor role) or
        having written it -- otherwise a 403, which discloses nothing the
        caller could not already list. The editor role is tenant-scoped, and
        GLOBAL rows are visible to every tenant, so it never reaches them:
        a GLOBAL memory is deletable only by its author.
        """

        store = self._store()
        memory = await store.get(owner, memory_id)
        if memory is None:
            raise HarborNotFoundError("Memory was not found")
        elevated = may_delete_shared and memory.scope is not MemoryScope.GLOBAL
        if not elevated and not modifiable_by(memory.scope, memory.owner, owner):
            raise HarborAuthError(
                "deleting a shared memory requires the editor role or authorship",
                forbidden=True,
            )
        await self._erasure.erase_memory(memory)
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
        effect_id = await self._erasure.begin(
            SESSION_ERASURE,
            owner,
            memories_required=self._memories is not None,
            index_required=self._index is not None,
        )
        purged = await self._purge_session(owner, identity)
        if not purged.complete:
            # Leave the effect open so replay retries the whole erasure.
            raise HarborUnavailableError("session erasure is incomplete; it will be retried")
        await self._conversations.delete(identity)
        await self._erasure.complete(effect_id)
        report = ErasureReport(
            memories=purged.memories,
            index_points=purged.index_points,
            sessions=1,
            conversation_messages_cleared=1,
        )
        _log_erasure("session", owner.tenant_id, actor, owner.session_id, report)
        return report

    async def recover_erasure(self, effect: PendingControlPlaneEffect) -> bool:
        """Replay authorized durable intents even after their session/row disappeared."""

        if await self._erasure.replay(effect):
            return True
        if effect.kind != SESSION_ERASURE:
            return False
        owner = MemoryOwner(**effect.payload["owner"])
        if effect.payload.get("memories_required") and self._memories is None:
            raise HarborUnavailableError("session erasure memory store is not configured")
        if effect.payload.get("index_required") and self._index is None:
            raise HarborUnavailableError("session erasure index is not configured")
        if owner.principal_id is None or owner.session_id is None:
            raise ValueError("session erasure intent lacks its ownership key")
        identity = ConversationIdentity(
            owner.tenant_id,
            owner.principal_id,
            owner.session_id,
            owner.user_id or owner.principal_id,
        )
        if not (await self._purge_session(owner, identity)).complete:
            raise HarborUnavailableError("session erasure is incomplete; it will be retried")
        await self._conversations.delete(identity)
        return True

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
        # Anything the user authored outside their own visibility key. Without
        # this pass a project-scoped fact about the erased user survives and
        # stays recallable to everyone else in that project.
        authored = await self._purge(user_owner, _AUTHORED_SCOPES, stored_by_owner=True)
        memories += authored.memories
        points += authored.index_points
        report = ErasureReport(
            memories=memories,
            index_points=points,
            sessions=len(purged.conversations),
            conversation_messages_cleared=len(purged.conversations),
            agent_run_checkpoints=await self._erase_checkpoints(user_owner),
        )
        _log_erasure("user", owner.tenant_id, actor, target, report)
        return report

    async def _purge_session(
        self, owner: MemoryOwner, identity: ConversationIdentity
    ) -> PurgeTotals:
        """The session's own memories, then every fact its human wrote from it.

        USER and RUN facts extracted from a conversation (and PROJECT and
        TENANT facts) cite it as ``source_session_id``. Deleting the
        conversation must not leave them behind. Only rows this human authored
        are removed, never what another user wrote, even into a shared scope
        under the same session id.
        """

        own = await self._purge(owner, _SESSION_SCOPES)
        author = MemoryOwner(
            tenant_id=identity.tenant_id,
            user_id=identity.user_id,
            principal_id=identity.principal_id,
        )
        extracted = await self._purge(
            author,
            (),
            stored_by_owner=True,
            source_session_id=identity.session_id,
            authorized=lambda row: _written_by(row, author),
        )
        return own + extracted

    async def _purge(
        self,
        owner: MemoryOwner,
        scopes: tuple[MemoryScope, ...],
        *,
        stored_by_owner: bool = False,
        source_session_id: str | None = None,
        authorized: Callable[[Memory], bool] | None = None,
    ) -> PurgeTotals:
        """Delete every memory ``owner`` reaches at ``scopes`` (all when empty), plus vectors.

        ``authorized`` is a defensive guard on each returned row: a rejection
        leaves the pass unerased and the totals ``complete=False``.
        """

        if self._memories is None:
            return PurgeTotals()
        query = MemoryQuery(
            owner=owner,
            scopes=scopes,
            limit=_PAGE,
            include_invalid=True,
            stored_by_owner=stored_by_owner,
            source_session_id=source_session_id,
        )
        totals = PurgeTotals()
        for _ in range(_MAX_PASSES):
            rows = await self._memories.search(query)
            if not rows:
                return totals
            erasable = [row for row in rows if authorized is None or authorized(row)]
            if len(erasable) != len(rows):
                # The query already pins the author, so a rejection is a
                # contract breach: never report it as a clean success.
                logger.warning(
                    "Memory purge guard rejected %d rows for tenant=%s; purge incomplete",
                    len(rows) - len(erasable),
                    owner.tenant_id,
                )
                return replace(totals, complete=False)
            for row in erasable:
                totals = totals + await self._erase_row(row)
        logger.warning(
            "Memory purge stopped after %d passes for tenant=%s scopes=%s; rows may remain",
            _MAX_PASSES,
            owner.tenant_id,
            ",".join(scope.value for scope in scopes) or "all",
        )
        return replace(totals, complete=False)

    async def _erase_row(self, row: Memory) -> PurgeTotals:
        """Erase one row found by a purge query, which already established authorization.

        ``delete`` re-checks visibility, and a PROJECT-scoped row is not
        visible to an eraser that holds no project_id. The row's own owner
        always satisfies that check, and the journal keeps that owner, so
        replay stays possible after the canonical row is gone.
        """

        erased: ErasedMemory = await self._erasure.erase_memory(row)
        principal_id = row.owner.principal_id
        session_id = row.source_session_id or row.owner.session_id
        conversations: frozenset[tuple[str, str]] = frozenset()
        if principal_id is not None and session_id is not None:
            conversations = frozenset({(principal_id, session_id)})
        return PurgeTotals(1, erased.points, int(erased.deferred), True, conversations)

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


def _written_by(row: Memory, author: MemoryOwner) -> bool:
    """Whether ``author`` wrote ``row``: same signed human, or same caller for DEFAULT_USER.

    ``DEFAULT_USER`` is no user, so it identifies nobody; those rows are
    attributed by the credential that stored them instead.
    """

    if author.user_id == DEFAULT_USER:
        return (
            row.owner.tenant_id == author.tenant_id
            and row.owner.user_id == DEFAULT_USER
            and row.owner.principal_id == author.principal_id
        )
    return authored_by(row.owner, author)


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
