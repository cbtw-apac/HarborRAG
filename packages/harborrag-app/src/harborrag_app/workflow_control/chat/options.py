"""Transport-neutral options for one chat execution."""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_runtime.chat import ChatPrompt


@dataclass(frozen=True, slots=True)
class ChatExecutionOptions:
    session_id: str
    system: ChatPrompt | None = None
    graph_search: bool | None = None
    # Validated against the tenant's projects; scopes the remembered turn.
    project_id: str | None = None
    # Stable end-user identity from the transport; defaults to the principal.
    user_id: str | None = None
    # Logical model the caller asked for, already validated against what this
    # tenant may use. ``None`` resolves the catalog's default.
    model: str | None = None


__all__ = ["ChatExecutionOptions"]
