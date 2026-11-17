"""Events published on the in-process bus and replayed to stream subscribers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class HarborEvent:
    """One named event of a run, published on the bus and replayed from storage.

    Args:
        name: Event name, prefixed with ``event_prefix`` of the run.
        trace_id: Identifier of the run the event belongs to.
        payload: Event data.
        created_at: When the event was created.
        seq: Per-task append sequence set once persisted; the replay cursor.
    """

    name: str
    trace_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    seq: int | None = None


def event_prefix(task_id: str) -> str:
    """Return the routing prefix of a run's stored and bus event names."""
    return f"task.{task_id}."
