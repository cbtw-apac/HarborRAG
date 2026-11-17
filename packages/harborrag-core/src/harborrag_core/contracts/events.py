from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class HarborEvent:
    name: str
    trace_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    seq: int | None = None  # per-task append sequence, set once persisted (SSE replay cursor)


def event_prefix(task_id: str) -> str:
    """Return the routing prefix of a run's stored and bus event names."""
    return f"task.{task_id}."
