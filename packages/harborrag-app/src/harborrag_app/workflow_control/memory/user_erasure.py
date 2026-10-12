"""Right-to-erasure for one end user, across every store that names them.

Postgres is authoritative, so the user's conversations are enumerated from the
conversation directory -- never from the memories they happened to produce.
Each store is one step. A step that fails is recorded and the others still
run, so a partial erasure removes everything it can and says exactly what is
left. The journal row is written before anything is deleted and finished
after, so a crash or a failure is visible, and re-running the same request is
safe: every step deletes only what still exists.

Qdrant and FalkorDB are projections: memory vectors go with their canonical
rows, and chat content was never projected into either.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from harborrag_core.base import utc_now
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.domain.identity import DEFAULT_USER
from harborrag_core.ports.conversation import MAX_CONVERSATION_PAGE_LIMIT, ConversationIdentity
from harborrag_core.ports.erasure import UserErasureRecord, UserErasureStatus

if TYPE_CHECKING:
    from harborrag_core.ports.agent_runs import AgentRunRepository
    from harborrag_core.ports.conversation import ConversationHistoryRepository
    from harborrag_core.ports.erasure import UserErasureJournal
    from harborrag_core.ports.usage import ModelUsageRepository

    from .administration import MemoryAdministrationService

logger = logging.getLogger("harborrag.app.workflow_control.memory.administration")

# Each pass deletes up to one full page, so one run reaches 50 * 100 sessions;
# beyond that the step fails and a re-run continues where this one stopped.
_MAX_SESSION_PASSES = 50
_COUNT_NAMES = (
    "sessions",
    "memories",
    "index_points",
    "deferred_index_points",
    "agent_run_checkpoints",
    "completion_requests",
    "model_usage_pseudonymized",
)


@dataclass(frozen=True, slots=True)
class UserErasureReport:
    """The finished journal record of one erasure, as the transport returns it."""

    record: UserErasureRecord

    def as_dict(self) -> dict[str, object]:
        return {
            "erasure_id": self.record.erasure_id,
            "status": self.record.status,
            **{name: self.record.counts.get(name, 0) for name in _COUNT_NAMES},
        }


@dataclass(slots=True)
class _Run:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_COUNT_NAMES, 0))
    failed: list[str] = field(default_factory=list)

    def add(self, name: str, value: int) -> None:
        self.counts[name] += value

    def status(self) -> UserErasureStatus:
        if self.failed:
            return "failed"
        return "deferred" if self.counts["deferred_index_points"] else "completed"


type _Step = Callable[[_Run, str, str], Awaitable[None]]


class UserErasureService:
    """Erase everything stored about one end user within one tenant."""

    def __init__(
        self,
        *,
        conversations: ConversationHistoryRepository,
        administration: MemoryAdministrationService,
        runs: AgentRunRepository | None = None,
        usage: ModelUsageRepository | None = None,
        journal: UserErasureJournal | None = None,
    ) -> None:
        self._conversations = conversations
        self._administration = administration
        self._runs = runs
        self._usage = usage
        self._journal = journal

    async def erase(self, *, tenant_id: str, user_id: str, actor: str) -> UserErasureReport:
        if user_id == DEFAULT_USER:
            # The sentinel every user-less caller shares names nobody: erasing
            # it would wipe the history of every such caller in the tenant.
            raise HarborValidationError(
                f"{DEFAULT_USER} is shared by every caller without an end-user claim "
                "and cannot be erased as one user"
            )
        started = UserErasureRecord(tenant_id=tenant_id, subject_user_id=user_id, actor=actor)
        if self._journal is not None:
            # No journal row, no deletion: a failure here propagates untouched.
            await self._journal.start(started)
        run = _Run()
        steps: tuple[tuple[str, _Step], ...] = (
            ("sessions", self._erase_sessions),
            ("memories", self._erase_memories),
            ("agent_runs", self._erase_agent_runs),
            ("completion_requests", self._erase_completions),
            ("model_usage", self._pseudonymize_usage),
        )
        for name, step in steps:
            await self._attempt(run, name, step, tenant_id, user_id)
        finished = replace(
            started,
            status=run.status(),
            counts=run.counts,
            failed_steps=tuple(run.failed),
            finished_at=utc_now(),
        )
        if self._journal is not None:
            await self._journal.finish(finished)
        _log_erasure(finished)
        return UserErasureReport(finished)

    @staticmethod
    async def _attempt(run: _Run, name: str, step: _Step, tenant_id: str, user_id: str) -> None:
        try:
            await step(run, tenant_id, user_id)
        except Exception as error:  # noqa: BLE001 - recorded, reported as failed, re-runnable
            run.failed.append(name)
            logger.warning(
                "User erasure step failed step=%s tenant=%s error_type=%s",
                name,
                tenant_id,
                type(error).__name__,
            )

    async def _erase_sessions(self, run: _Run, tenant_id: str, user_id: str) -> None:
        """Delete every conversation the human owns, re-listing from the first page.

        Following ``next_cursor`` would name a session this loop just deleted,
        which the directory rejects; the first page is always what is left.
        """

        for _ in range(_MAX_SESSION_PASSES):
            page = await self._conversations.list_conversations(
                tenant_id=tenant_id, user_id=user_id, limit=MAX_CONVERSATION_PAGE_LIMIT
            )
            if not page.conversations:
                return
            for row in page.conversations:
                # principal_id is provenance only (compare=False): every
                # predicate keys on tenant, user and session.
                identity = ConversationIdentity(tenant_id, "", row.session_id, user_id)
                if await self._conversations.delete(identity):
                    run.add("sessions", 1)
        raise RuntimeError("conversation sessions remained after the pass limit")

    async def _erase_memories(self, run: _Run, tenant_id: str, user_id: str) -> None:
        totals = await self._administration.purge_user_memories(
            tenant_id=tenant_id, user_id=user_id
        )
        run.add("memories", totals.memories)
        run.add("index_points", totals.index_points)
        run.add("deferred_index_points", totals.deferred)

    async def _erase_agent_runs(self, run: _Run, tenant_id: str, user_id: str) -> None:
        if self._runs is not None:
            removed = await self._runs.delete_for_owner(tenant_id=tenant_id, user_id=user_id)
            run.add("agent_run_checkpoints", removed)

    async def _erase_completions(self, run: _Run, tenant_id: str, user_id: str) -> None:
        removed = await self._conversations.delete_completions_for_user(
            tenant_id=tenant_id, user_id=user_id
        )
        run.add("completion_requests", removed)

    async def _pseudonymize_usage(self, run: _Run, tenant_id: str, user_id: str) -> None:
        if self._usage is not None:
            # Random per erasure and never derived from the user id, so the
            # token cannot be linked back to the person it replaces.
            changed = await self._usage.pseudonymize_user(
                tenant_id=tenant_id, user_id=user_id, pseudonym=f"erased-{uuid4().hex}"
            )
            run.add("model_usage_pseudonymized", changed)


def _log_erasure(record: UserErasureRecord) -> None:
    """One line per erasure with identifiers and counts only -- never content."""

    logger.info(
        "Memory erasure kind=user tenant=%s actor=%s target=%s erasure_id=%s status=%s %s",
        record.tenant_id,
        record.actor,
        record.subject_user_id,
        record.erasure_id,
        record.status,
        " ".join(f"{name}={record.counts.get(name, 0)}" for name in _COUNT_NAMES),
    )


__all__ = ["UserErasureReport", "UserErasureService"]
