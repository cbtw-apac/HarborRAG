"""Transport-neutral options for one chat execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from harborrag_runtime.chat import ChatPrompt

from ..memory import ChatMode


@dataclass(frozen=True, slots=True)
class ChatExecutionOptions:
    """How one chat turn runs.

    Attributes:
        session_id: The session the turn belongs to; ``None`` asks a one-off question,
            answered without being stored.
        system: The server-owned prompt.
        graph_search: Overrides ``chat_retrieval_graph_search`` for a classic turn; an
            instant turn never observes the graph.
        user_id: The end user who owns the session; defaults to the principal.
        model: A logical model already allowed for the tenant; ``None`` is the default.
        mode: ``classic`` checks the request's scope with a model call and observes the
            graph per settings; ``instant`` does neither.
        project_ids: The projects a one-off question reads; a session reads its own.
        connections: Connection refs (``<project_id>/<connection_id>``) to narrow to.
        filters: Evidence filters for retrieval.
        trace_id: The request's trace id; a new one when ``None``.
    """

    session_id: str | None = None
    system: ChatPrompt | None = None
    graph_search: bool | None = None
    user_id: str | None = None
    model: str | None = None
    mode: ChatMode = "classic"
    project_ids: tuple[str, ...] = ()
    connections: tuple[str, ...] = ()
    filters: Mapping[str, object] = field(default_factory=dict)
    trace_id: str | None = None
