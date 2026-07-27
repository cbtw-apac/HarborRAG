"""Project aggregate: the top-level grouping for sources and documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from harborrag_core.base import utc_now

from .validation import require_id

ProjectStatus = Literal["active", "archived"]


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
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    stats: ProjectStats = field(default_factory=ProjectStats)

    def __post_init__(self) -> None:
        require_id(self.id, label="Project")
