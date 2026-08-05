"""Transport-neutral options for one bounded agent execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentExecutionOptions:
    session_id: str
    graph_search: bool = False
    max_steps: int = 4


__all__ = ["AgentExecutionOptions"]
