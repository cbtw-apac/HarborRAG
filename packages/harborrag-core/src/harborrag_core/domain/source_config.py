"""SourceConfig aggregate: a configured source instance.

config carries secret_ref placeholders only — raw secret values live behind
the secrets port and never enter the domain layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SourceStatus = Literal["active", "paused", "error"]


@dataclass(slots=True)
class SourceConfig:
    """A configured source attached to a project.

    `schedule` is a cron-style string or None for manual-only sync;
    `secret_refs` lists the refs extracted from config at the DTO boundary.
    """

    id: str
    project_id: str
    source_type: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    schedule: str | None = None
    status: SourceStatus = "active"
    secret_refs: list[str] = field(default_factory=list)
