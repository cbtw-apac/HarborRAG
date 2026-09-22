"""Transport-neutral options for one bounded agent execution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentExecutionOptions:
    session_id: str
    graph_search: bool = False
    max_steps: int = 4
    # Wall-clock budget the transport has for delivering the whole response
    # (the HTTP request timeout, or the stream timeout for SSE). ``None`` means
    # "no transport deadline is known"; the application service then applies
    # its default.
    deadline_seconds: float | None = None
    # Total prompt+completion token budget across every step; ``None`` uses
    # the application service default.
    token_budget: int | None = None
    # Validated against the tenant's projects; scopes the remembered turn.
    project_id: str | None = None
    # Stable end-user identity from the transport; defaults to the principal.
    user_id: str | None = None
    # Logical model the caller asked for, already validated against what this
    # tenant may use. ``None`` resolves the catalog's default.
    model: str | None = None


__all__ = ["AgentExecutionOptions"]
