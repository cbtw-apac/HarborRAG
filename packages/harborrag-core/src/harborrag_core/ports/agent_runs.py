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
    """Isolation key for one authenticated agent run."""

    tenant_id: str
    principal_id: str
    session_id: str
    run_id: str


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
    """Full resumable state of one agent run at its last completed step."""

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


class AgentRunRepository(Protocol):
    """Persistence-neutral checkpoint contract for resumable agent runs."""

    async def create(self, checkpoint: AgentCheckpoint) -> None: ...

    async def save_step(self, checkpoint: AgentCheckpoint) -> None:
        """Persist ``checkpoint`` if its ``version`` is the next expected one.

        Implementations must treat this as an optimistic-concurrency update:
        a stale writer (whose ``version`` no longer matches the stored row)
        must raise ``HarborConflictError`` rather than overwrite newer state.
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
