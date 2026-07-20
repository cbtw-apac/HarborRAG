"""Repository ports for control-plane aggregates (plan §6 tables).

One Protocol per aggregate, async because every real implementation is
I/O-bound (SQLAlchemy async, ST5). App/runtime layers depend on these,
never on the adapter classes.
"""

from __future__ import annotations

from typing import Protocol

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.member import Member
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig


class ProjectRepositoryPort(Protocol):
    """CRUD for projects (plan §5.1)."""

    async def list(self) -> list[Project]:
        """All projects, unpaginated (pagination lands with the M1 routes)."""

    async def get(self, project_id: str) -> Project | None:
        """One project by id, or None."""

    async def create(self, project: Project) -> Project:
        """Persist a new project and return it."""

    async def update(self, project: Project) -> Project:
        """Persist changes to an existing project and return it."""

    async def delete(self, project_id: str) -> None:
        """Remove a project; cascading tombstones are the engine's job."""


class SourceRepositoryPort(Protocol):
    """CRUD for configured sources (plan §5.2)."""

    async def list(self, project_id: str | None = None) -> list[SourceConfig]:
        """Sources, optionally filtered to one project."""

    async def get(self, source_id: str) -> SourceConfig | None:
        """One source by id, or None."""

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Persist a new source and return it."""

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Persist changes to an existing source and return it."""

    async def delete(self, source_id: str) -> None:
        """Remove a source configuration."""


class JobRepositoryPort(Protocol):
    """Read/persist jobs and their event streams (plan §5.3, §6 job_events)."""

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
    ) -> list[Job]:
        """Jobs filtered by status and/or source."""

    async def get(self, job_id: str) -> Job | None:
        """One job by id, or None."""

    async def save(self, job: Job) -> Job:
        """Insert or update a job row."""

    async def append_event(self, job_id: str, event: HarborEvent) -> None:
        """Append to the job's ordered event log (WS reconnect replay source)."""


class ActivityRepositoryPort(Protocol):
    """Append-only audit feed (plan §5.5)."""

    async def append(self, entry: ActivityEntry) -> None:
        """Write one audit row; entries are never mutated or deleted."""

    async def list(self, limit: int = 50) -> list[ActivityEntry]:
        """Most recent entries, newest first."""


class SettingsRepositoryPort(Protocol):
    """Single-document workspace settings (plan §5.5)."""

    async def get(self) -> WorkspaceSettings:
        """The settings document (empty document if never written)."""

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Replace the settings document and return it."""


class ProviderRepositoryPort(Protocol):
    """Model provider registry (plan §5.5)."""

    async def list(self) -> list[Provider]:
        """All registered providers."""

    async def get(self, provider_id: str) -> Provider | None:
        """One provider by id, or None."""

    async def save(self, provider: Provider) -> Provider:
        """Insert or update (upsert) a provider."""

    async def delete(self, provider_id: str) -> None:
        """Remove a provider registration."""


class MemberRepositoryPort(Protocol):
    """Workspace members and their RBAC roles (plan §8.1)."""

    async def list(self) -> list[Member]:
        """All members."""

    async def get_by_subject(self, subject: str) -> Member | None:
        """Look up a member by auth subject (JWT sub), or None."""

    async def save(self, member: Member) -> Member:
        """Insert or update a membership row."""

    async def delete(self, member_id: str) -> None:
        """Remove a member."""
