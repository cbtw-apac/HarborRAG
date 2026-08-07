"""Transport-neutral progress events emitted by the agent loop.

The agent loop knows nothing about SSE, WebSockets, or logging -- it emits
``AgentEvent`` to an injected sink, and a transport layer decides how to
render each one. Passing no sink (the default) costs nothing: ``run()``
and ``resume()`` skip emission entirely when ``events`` is ``None``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One point-in-time occurrence during an agent run.

    ``kind`` is a stable dotted name (``"run.started"``, ``"tool.completed"``,
    ...) a transport can use as a discriminator; ``data`` carries only
    JSON-serializable, already-public fields -- the same values that appear
    in ``AgentRunResult``/``AgentToolExecution``.
    """

    kind: str
    run_id: str
    data: Mapping[str, object]


AgentEventSink = Callable[[AgentEvent], Awaitable[None]]


async def emit(sink: AgentEventSink | None, event: AgentEvent) -> None:
    if sink is not None:
        await sink(event)


__all__ = ["AgentEvent", "AgentEventSink", "emit"]
