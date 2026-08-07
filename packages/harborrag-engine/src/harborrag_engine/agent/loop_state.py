"""State threaded through one agent run/resume attempt.

Split out of ``loop.py`` purely so both ``service.py`` (which builds this
state) and ``loop.py`` (which consumes it) can import it without a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.agent_runs import AgentRunIdentity, AgentStopReason, AgentToolExecution
from harborrag_engine.conversation import ConversationIdentity

from .events import AgentEventSink
from .guard import ExecutionGuard
from .schemas import AgentRunOptions


@dataclass(frozen=True, slots=True)
class RunContext:
    """Per-attempt collaborators that do not change across loop steps."""

    identity: AgentRunIdentity
    conversation_identity: ConversationIdentity | None
    options: AgentRunOptions
    guard: ExecutionGuard
    events: AgentEventSink | None
    current_user_message: HarborChatMessage | None
    created_at: datetime


@dataclass(slots=True)
class LoopState:
    """Mutable execution state threaded through one run/resume attempt."""

    conversation: list[HarborChatMessage]
    executions: list[AgentToolExecution]
    usage: HarborChatUsage
    step: int
    version: int


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Result of attempting one loop step: continue, or stop with a reason."""

    calls_made: int
    stop_reason: AgentStopReason | None = None
    final_response: HarborChatResponse | None = None


__all__ = ["LoopState", "RunContext", "StepOutcome"]
