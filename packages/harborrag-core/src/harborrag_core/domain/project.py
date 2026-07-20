"""Project aggregate: the top-level grouping for sources and documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

ProjectStatus = Literal["active", "archived"]


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp (RFC 3339-ready, per API conventions)."""
    return datetime.now(UTC)


@dataclass(slots=True)
class ProjectStats:
    """Read-model counters shown on the project resource."""

    documents: int = 0
    chunks: int = 0
    size_bytes: int = 0
    last_sync_at: datetime | None = None


@dataclass(slots=True)
class Project:
    """A project owning a vector collection and a set of configured sources."""

    id: str
    name: str
    collection: str
    description: str = ""
    status: ProjectStatus = "active"
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    stats: ProjectStats = field(default_factory=ProjectStats)
