"""In-memory control-plane repository fakes.

Split out of fakes.py to keep that file under the repo's file-length gate;
these are the tenant-scoped ProjectRepositoryPort/SourceRepositoryPort/... test
doubles used by both the test suite and local/dev composition.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.member import Member
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig


def _in_scope(tenant_id: str, tenant_ids: frozenset[str] | None) -> bool:
    """Mirror the Sql* repositories' tenant filter: None means unrestricted."""
    return tenant_ids is None or tenant_id in tenant_ids


@dataclass(slots=True)
class FakeProjectRepository:
    """Dict-backed ProjectRepositoryPort for tests and local composition."""

    projects: dict[str, Project] = field(default_factory=dict)

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Project]:
        """Projects visible to ``tenant_ids`` (None: unrestricted), in insertion order."""
        return [p for p in self.projects.values() if _in_scope(p.tenant_id, tenant_ids)]

    async def get(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> Project | None:
        """Project by id within ``tenant_ids``, or None."""
        project = self.projects.get(project_id)
        if project is None or not _in_scope(project.tenant_id, tenant_ids):
            return None
        return project

    async def create(self, project: Project) -> Project:
        """Store a new project."""
        self.projects[project.id] = project
        return project

    async def update(self, project: Project) -> Project:
        """Overwrite an existing project."""
        self.projects[project.id] = project
        return project

    async def delete(self, project_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Drop the project if present within ``tenant_ids``."""
        project = self.projects.get(project_id)
        if project is not None and _in_scope(project.tenant_id, tenant_ids):
            del self.projects[project_id]


@dataclass(slots=True)
class FakeSourceRepository:
    """Dict-backed SourceRepositoryPort."""

    sources: dict[str, SourceConfig] = field(default_factory=dict)

    async def list(
        self,
        project_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[SourceConfig]:
        """Sources visible to ``tenant_ids`` (None: unrestricted), optionally filtered by project."""
        values = [s for s in self.sources.values() if _in_scope(s.tenant_id, tenant_ids)]
        if project_id is None:
            return values
        return [source for source in values if source.project_id == project_id]

    async def get(
        self, source_id: str, *, tenant_ids: frozenset[str] | None
    ) -> SourceConfig | None:
        """Source by id within ``tenant_ids``, or None."""
        source = self.sources.get(source_id)
        if source is None or not _in_scope(source.tenant_id, tenant_ids):
            return None
        return source

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Store a new source."""
        self.sources[source.id] = source
        return source

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Overwrite an existing source."""
        self.sources[source.id] = source
        return source

    async def delete(self, source_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Drop the source if present within ``tenant_ids``."""
        source = self.sources.get(source_id)
        if source is not None and _in_scope(source.tenant_id, tenant_ids):
            del self.sources[source_id]


@dataclass(slots=True)
class FakeJobRepository:
    """Dict-backed JobRepositoryPort with per-job event logs."""

    jobs: dict[str, Job] = field(default_factory=dict)
    events: dict[str, list[HarborEvent]] = field(default_factory=dict)

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[Job]:
        """Jobs visible to ``tenant_ids`` (None: unrestricted), filtered by status and/or source."""
        result = [job for job in self.jobs.values() if _in_scope(job.tenant_id, tenant_ids)]
        if status is not None:
            result = [job for job in result if job.status == status]
        if source_id is not None:
            result = [job for job in result if job.source_id == source_id]
        return result

    async def get(self, job_id: str, *, tenant_ids: frozenset[str] | None) -> Job | None:
        """Job by id within ``tenant_ids``, or None."""
        job = self.jobs.get(job_id)
        if job is None or not _in_scope(job.tenant_id, tenant_ids):
            return None
        return job

    async def save(self, job: Job) -> Job:
        """Insert or overwrite a job."""
        self.jobs[job.id] = job
        return job

    async def append_event(self, job_id: str, event: HarborEvent) -> None:
        """Append to the job's ordered event log."""
        self.events.setdefault(job_id, []).append(event)

    async def count_by_status(self, *, tenant_ids: frozenset[str] | None) -> dict[str, int]:
        """Job counts within ``tenant_ids`` grouped by status."""
        counts: dict[str, int] = {}
        for job in self.jobs.values():
            if _in_scope(job.tenant_id, tenant_ids):
                counts[job.status] = counts.get(job.status, 0) + 1
        return counts


@dataclass(slots=True)
class FakeActivityRepository:
    """List-backed ActivityRepositoryPort (append-only)."""

    entries: list[ActivityEntry] = field(default_factory=list)

    async def append(self, entry: ActivityEntry) -> None:
        """Record one audit entry."""
        self.entries.append(entry)

    async def list(
        self, limit: int = 50, *, tenant_ids: frozenset[str] | None
    ) -> list[ActivityEntry]:
        """Newest entries within ``tenant_ids`` first."""
        # Order by created_at timestamp to match SQL-backed repos and
        # avoid surprises when tests seed out-of-order timestamps.
        scoped = [e for e in self.entries if _in_scope(e.tenant_id, tenant_ids)]
        return sorted(scoped, key=lambda e: e.created_at, reverse=True)[:limit]


@dataclass(slots=True)
class FakeSettingsRepository:
    """Single-document SettingsRepositoryPort."""

    settings: WorkspaceSettings = field(
        default_factory=lambda: WorkspaceSettings(tenant_id="DEFAULT")
    )

    async def get(self) -> WorkspaceSettings:
        """The current settings document."""
        return self.settings

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Replace the settings document."""
        self.settings = settings
        return settings


@dataclass(slots=True)
class FakeProviderRepository:
    """Dict-backed ProviderRepositoryPort."""

    providers: dict[str, Provider] = field(default_factory=dict)

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Provider]:
        """Providers visible to ``tenant_ids`` (None: unrestricted)."""
        return [p for p in self.providers.values() if _in_scope(p.tenant_id, tenant_ids)]

    async def get(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> Provider | None:
        """Provider by id within ``tenant_ids``, or None."""
        provider = self.providers.get(provider_id)
        if provider is None or not _in_scope(provider.tenant_id, tenant_ids):
            return None
        return provider

    async def save(self, provider: Provider) -> Provider:
        """Insert or overwrite a provider."""
        self.providers[provider.id] = provider
        return provider

    async def delete(self, provider_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Drop the provider if present within ``tenant_ids``."""
        provider = self.providers.get(provider_id)
        if provider is not None and _in_scope(provider.tenant_id, tenant_ids):
            del self.providers[provider_id]


@dataclass(slots=True)
class FakeMemberRepository:
    """Dict-backed MemberRepositoryPort."""

    members: dict[str, Member] = field(default_factory=dict)

    async def list(self, *, tenant_ids: frozenset[str] | None) -> list[Member]:
        """Members visible to ``tenant_ids`` (None: unrestricted)."""
        return [m for m in self.members.values() if _in_scope(m.tenant_id, tenant_ids)]

    async def get_by_subject(self, subject: str) -> Member | None:
        """Member by auth subject, or None."""
        for member in self.members.values():
            if member.subject == subject:
                return member
        return None

    async def save(self, member: Member) -> Member:
        """Insert or overwrite a member."""
        self.members[member.id] = member
        return member

    async def delete(self, member_id: str, *, tenant_ids: frozenset[str] | None) -> None:
        """Drop the member if present within ``tenant_ids``."""
        member = self.members.get(member_id)
        if member is not None and _in_scope(member.tenant_id, tenant_ids):
            del self.members[member_id]
