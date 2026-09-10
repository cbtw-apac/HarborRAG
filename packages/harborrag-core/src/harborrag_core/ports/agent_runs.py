"""Agent-run checkpoint port shared by the agent loop and persistence adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage


def new_run_id() -> str:
    """Generate one API-safe opaque agent-run identifier."""

    return f"run-{uuid4().hex}"


class AgentRunStatus(StrEnum):
    """Lifecycle state of one agent run's checkpointed record."""

    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class AgentStopReason(StrEnum):
    """Why an agent run stopped producing tool calls and returned a response."""

    FINAL_ANSWER = "final_answer"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    REPEATED_TOOL_CALL = "repeated_tool_call"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"


@dataclass(frozen=True, slots=True)
class AgentRunIdentity:
    """Isolation key for one authenticated agent run.

    The run belongs to the human named by ``user_id``, matching the
    conversation session it hangs off: one service principal fronting several
    people must not let them read, advance, or resume each other's runs.
    ``principal_id`` is retained purely as the credential that acted, so it
    stays out of every predicate and only appears in the audit trail.
    """

    tenant_id: str
    principal_id: str
    session_id: str
    run_id: str
    user_id: str


@dataclass(frozen=True, slots=True)
class AgentToolExecution:
    """Safe public trace for one tool invocation.

    ``arguments_digest`` is a stable hash of the call's arguments, not the
    arguments themselves -- it exists so repeated-call detection can be
    rebuilt from persisted executions on resume without storing raw tool
    input in the trace.
    """

    step: int
    call_id: str
    tool: str
    ok: bool
    arguments_digest: str


@dataclass(frozen=True, slots=True)
class AgentCheckpoint:
    """Full resumable state of one agent run at its last completed step.

    ``failure_retryable`` is only meaningful when ``status`` is ``FAILED``: it
    records whether the exception that ended the run was transient (provider
    connection/timeout/rate-limit, deadline) so ``resume`` can accept the run
    without re-classifying an error it no longer has.

    ``lease_owner``/``lease_expires_at`` fence concurrent executors: a
    ``RUNNING`` checkpoint whose lease has not expired belongs to a live
    worker and must not be resumed; one whose lease has lapsed is treated as
    crashed and may be claimed. Every ``save_step`` from the executing
    worker refreshes the lease; terminal statuses release it.
    """

    identity: AgentRunIdentity
    status: AgentRunStatus
    step: int
    version: int
    messages: tuple[HarborChatMessage, ...]
    executions: tuple[AgentToolExecution, ...]
    usage: HarborChatUsage
    stop_reason: AgentStopReason | None
    response: HarborChatResponse | None
    created_at: datetime
    updated_at: datetime
    failure_retryable: bool = False
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None

    def lease_active(self, now: datetime) -> bool:
        """Return whether a live executor still holds this run's lease at ``now``."""

        return self.lease_expires_at is not None and self.lease_expires_at > now

    def resumable(self, now: datetime) -> bool:
        """Return whether ``resume`` may legitimately pick this run up at ``now``.

        ``RUNNING`` with an expired (or absent) lease is the crash case;
        ``CANCELLED`` is always resumable; ``FAILED`` only when the failure
        was transient. ``COMPLETED`` runs are never resumable.
        """

        if self.status is AgentRunStatus.RUNNING:
            return not self.lease_active(now)
        if self.status is AgentRunStatus.CANCELLED:
            return True
        return self.status is AgentRunStatus.FAILED and self.failure_retryable


class AgentRunRepository(Protocol):
    """Persistence-neutral checkpoint contract for resumable agent runs."""

    async def create(self, checkpoint: AgentCheckpoint) -> None: ...

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        """Persist ``checkpoint`` if its ``version`` is the next expected one.

        Implementations must treat this as an optimistic-concurrency update:
        a stale writer (whose ``version`` no longer matches the stored row)
        must raise ``HarborConflictError`` rather than overwrite newer state.
        The lease columns are written verbatim from ``checkpoint`` so the
        executing worker refreshes its lease with every persisted step.
        """

    async def get(self, identity: AgentRunIdentity) -> AgentCheckpoint | None: ...


__all__ = [
    "AgentCheckpoint",
    "AgentRunIdentity",
    "AgentRunRepository",
    "AgentRunStatus",
    "AgentStopReason",
    "AgentToolExecution",
    "new_run_id",
]
