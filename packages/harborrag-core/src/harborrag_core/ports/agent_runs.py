"""Agent-run checkpoint port shared by the agent loop and persistence adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Protocol, cast
from unicodedata import category
from uuid import uuid4

from harborrag_core.domain.validation import require_identity_fields
from harborrag_core.invariants import require
from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.models.cost import ModelCost


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

    def __post_init__(self) -> None:
        # The isolation keys only. ``principal_id`` stays out of every
        # predicate by design, so it is provenance rather than a boundary.
        require_identity_fields(
            tenant_id=self.tenant_id,
            session_id=self.session_id,
            run_id=self.run_id,
            user_id=self.user_id,
        )


@dataclass(frozen=True, slots=True)
class AgentEvidenceReference:
    """Canonical source chunk returned by one successful agent tool call."""

    tool: str
    chunk_id: str
    document_id: str
    score: float | None = None
    document_title: str | None = None
    section_path: tuple[str, ...] = ()
    location: str | None = None
    canonical_marker: str | None = None
    content: str | None = None
    content_truncated: bool = False

    def __post_init__(self) -> None:
        if self.content is not None and len(self.content) > 8000:
            object.__setattr__(self, "content", self.content[:8000])
            object.__setattr__(self, "content_truncated", True)
        object.__setattr__(self, "document_title", _reference_text(self.document_title, 256))
        object.__setattr__(
            self,
            "section_path",
            tuple(
                value
                for part in self.section_path[:16]
                if (value := _reference_text(part, 128)) is not None
            ),
        )
        object.__setattr__(self, "location", _reference_text(self.location, 120))
        if self.canonical_marker is None:
            object.__setattr__(self, "canonical_marker", self._derived_marker())

    @property
    def marker(self) -> str:
        """Exact model-visible provenance retained across checkpoint upgrades."""

        require(self.canonical_marker is not None, "agent evidence marker must be derived")
        return cast("str", self.canonical_marker)

    def _derived_marker(self) -> str:
        """Build the initial readable label for newly observed evidence."""

        title = _marker_text(self.document_title or self.document_id or "Untitled document", 120)
        section_value = " > ".join(part for part in self.section_path if part.strip())
        section = _marker_text(section_value, 220) if section_value else ""
        location = section or _marker_text(self.location or "source passage", 120)
        reference = sha256(f"{self.document_id}\0{self.chunk_id}".encode()).hexdigest()[:12]
        return f'[Source: "{title}" — {location} (ref {reference})]'


def _marker_text(value: str, limit: int) -> str:
    """Keep untrusted source labels on one bounded marker line."""

    safe_value = "".join(
        character for character in value if not category(character).startswith("C")
    )
    normalized = re.sub(r"\s+", " ", safe_value).strip()
    normalized = normalized.replace("[", "(").replace("]", ")").replace('"', "'")
    return normalized[:limit] or "unknown"


def _reference_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    safe = "".join(
        character for character in value[: limit + 1] if not category(character).startswith("C")
    )
    normalized = re.sub(r"\s+", " ", safe).strip()
    return normalized[:limit] if normalized else None


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
    evidence: tuple[AgentEvidenceReference, ...] = ()


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
    cost: ModelCost = field(default_factory=ModelCost)
    # The options that shaped the run. A resume restores them so the transcript
    # stays the product of one set of tool definitions and one budget: passing
    # graph_search=True to resume a run that started without it would hand the
    # model tools the earlier steps never had. ``None`` means the checkpoint
    # predates the field, and the resumer's own value is used.
    logical_model: str | None = None
    graph_search: bool | None = None
    max_steps: int | None = None
    max_total_tokens: int | None = None
    timeout_seconds: float | None = None

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
    "AgentEvidenceReference",
    "AgentRunIdentity",
    "AgentRunRepository",
    "AgentRunStatus",
    "AgentStopReason",
    "AgentToolExecution",
    "new_run_id",
]
