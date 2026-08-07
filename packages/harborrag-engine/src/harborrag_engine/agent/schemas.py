"""Public request/result contracts for the agent loop."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.models.chat import HarborChatResponse, HarborChatUsage
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


__all__ = ["AgentRunOptions", "AgentRunResult"]
