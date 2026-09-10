"""Public request/result contracts for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.models.chat import HarborChatMessage, HarborChatResponse, HarborChatUsage
from harborrag_core.ports.agent_runs import AgentStopReason, AgentToolExecution


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Final model response plus bounded execution metadata."""

    run_id: str
    response: HarborChatResponse
    executions: tuple[AgentToolExecution, ...]
    turns: int
    usage: HarborChatUsage
    stop_reason: AgentStopReason


@dataclass(frozen=True, slots=True)
class AgentRunOptions:
    tenant_id: str
    principal_id: str
    session_id: str
    graph_search: bool = False
    max_steps: int = 4
    timeout_seconds: float | None = None
    max_repeated_tool_calls: int = 2
    synthesis_timeout_seconds: float | None = 30.0
    max_total_tokens: int | None = None
    # Conversation context chosen by the caller's memory policy. When ``history``
    # is supplied the engine replays it instead of reading the last turns itself,
    # so trimming, summarization, and recall stay one decision made upstream.
    history: tuple[HarborChatMessage, ...] = field(default_factory=tuple)
    memory_summary: str | None = None
    # The human the run is for, which owns the conversation and is what every
    # recorded token is attributed to. ``principal_id`` is only the credential
    # that acted. Direct SDK callers that pass none fall back to the principal.
    user_id: str | None = None

    @property
    def owner_id(self) -> str:
        """The end user this run belongs to, defaulting to the principal."""

        return self.user_id or self.principal_id


__all__ = ["AgentRunOptions", "AgentRunResult"]
