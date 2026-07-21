from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field

from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.element import DocumentElement
from harborrag_core.domain.job import Job, JobStatus
from harborrag_core.domain.member import Member
from harborrag_core.domain.parser import ParsedDocument
from harborrag_core.domain.project import Project
from harborrag_core.domain.provider import Provider
from harborrag_core.domain.raw_document import RawDocument
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source import SourceRecord
from harborrag_core.domain.source_config import SourceConfig


@dataclass(slots=True)
class FakeConnector:
    provider_name: str = "fake"
    documents: list[RawDocument] = field(default_factory=list)

    def discover(self) -> Iterable[SourceRecord]:
        for raw in self.documents:
            yield SourceRecord(
                id=raw.id, source_type=raw.content_type, locator=raw.source
            )

    def load(self, record: SourceRecord) -> RawDocument:
        for raw in self.documents:
            if raw.id == record.id:
                return raw
        raise KeyError(record.id)


@dataclass(slots=True)
class FakeParser:
    parser_name: str = "fake"

    def parse(self, raw: RawDocument) -> ParsedDocument:
        text = raw.text()
        return ParsedDocument(
            content=text,
            parser_name=self.parser_name,
            elements=[DocumentElement(id=f"{raw.id}:0", type="paragraph", content=text)],
        )


@dataclass(slots=True)
class FakeProjectRepository:
    """Dict-backed ProjectRepositoryPort for tests and local composition."""

    projects: dict[str, Project] = field(default_factory=dict)

    async def list(self) -> list[Project]:
        """All projects in insertion order."""
        return list(self.projects.values())

    async def get(self, project_id: str) -> Project | None:
        """Project by id, or None."""
        return self.projects.get(project_id)

    async def create(self, project: Project) -> Project:
        """Store a new project."""
        self.projects[project.id] = project
        return project

    async def update(self, project: Project) -> Project:
        """Overwrite an existing project."""
        self.projects[project.id] = project
        return project

    async def delete(self, project_id: str) -> None:
        """Drop the project if present."""
        self.projects.pop(project_id, None)


@dataclass(slots=True)
class FakeSourceRepository:
    """Dict-backed SourceRepositoryPort."""

    sources: dict[str, SourceConfig] = field(default_factory=dict)

    async def list(self, project_id: str | None = None) -> list[SourceConfig]:
        """Sources, optionally filtered by project."""
        values = list(self.sources.values())
        if project_id is None:
            return values
        return [source for source in values if source.project_id == project_id]

    async def get(self, source_id: str) -> SourceConfig | None:
        """Source by id, or None."""
        return self.sources.get(source_id)

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Store a new source."""
        self.sources[source.id] = source
        return source

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Overwrite an existing source."""
        self.sources[source.id] = source
        return source

    async def delete(self, source_id: str) -> None:
        """Drop the source if present."""
        self.sources.pop(source_id, None)


@dataclass(slots=True)
class FakeJobRepository:
    """Dict-backed JobRepositoryPort with per-job event logs."""

    jobs: dict[str, Job] = field(default_factory=dict)
    events: dict[str, list[HarborEvent]] = field(default_factory=dict)

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
    ) -> list[Job]:
        """Jobs filtered by status and/or source."""
        result = list(self.jobs.values())
        if status is not None:
            result = [job for job in result if job.status == status]
        if source_id is not None:
            result = [job for job in result if job.source_id == source_id]
        return result

    async def get(self, job_id: str) -> Job | None:
        """Job by id, or None."""
        return self.jobs.get(job_id)

    async def save(self, job: Job) -> Job:
        """Insert or overwrite a job."""
        self.jobs[job.id] = job
        return job

    async def append_event(self, job_id: str, event: HarborEvent) -> None:
        """Append to the job's ordered event log."""
        self.events.setdefault(job_id, []).append(event)


@dataclass(slots=True)
class FakeActivityRepository:
    """List-backed ActivityRepositoryPort (append-only)."""

    entries: list[ActivityEntry] = field(default_factory=list)

    async def append(self, entry: ActivityEntry) -> None:
        """Record one audit entry."""
        self.entries.append(entry)

    async def list(self, limit: int = 50) -> list[ActivityEntry]:
        """Newest entries first."""
        return list(reversed(self.entries))[:limit]


@dataclass(slots=True)
class FakeSettingsRepository:
    """Single-document SettingsRepositoryPort."""

    settings: WorkspaceSettings = field(default_factory=WorkspaceSettings)

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

    async def list(self) -> list[Provider]:
        """All providers."""
        return list(self.providers.values())

    async def get(self, provider_id: str) -> Provider | None:
        """Provider by id, or None."""
        return self.providers.get(provider_id)

    async def save(self, provider: Provider) -> Provider:
        """Insert or overwrite a provider."""
        self.providers[provider.id] = provider
        return provider

    async def delete(self, provider_id: str) -> None:
        """Drop the provider if present."""
        self.providers.pop(provider_id, None)


@dataclass(slots=True)
class FakeMemberRepository:
    """Dict-backed MemberRepositoryPort."""

    members: dict[str, Member] = field(default_factory=dict)

    async def list(self) -> list[Member]:
        """All members."""
        return list(self.members.values())

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

    async def delete(self, member_id: str) -> None:
        """Drop the member if present."""
        self.members.pop(member_id, None)


@dataclass(slots=True)
class FakeJobQueue:
    """In-memory JobQueuePort: FIFO claim, retryable re-queue, cancel."""

    jobs: dict[str, Job] = field(default_factory=dict)
    pending: list[str] = field(default_factory=list)

    async def enqueue(self, job: Job) -> Job:
        """Add a job to the tail of the queue."""
        self.jobs[job.id] = job
        self.pending.append(job.id)
        return job

    async def claim_next(self, lease_seconds: int) -> Job | None:
        """Claim the head of the queue; lease_seconds is unused by the fake."""
        while self.pending:
            job = self.jobs[self.pending.pop(0)]
            if job.status != "queued":
                continue
            job.status = "running"
            job.attempts += 1
            return job
        return None

    async def mark_done(self, job_id: str) -> None:
        """Running -> succeeded."""
        self.jobs[job_id].status = "succeeded"

    async def mark_failed(self, job_id: str, error: str, retryable: bool) -> None:
        """Record error; re-queue when retryable, else mark failed."""
        job = self.jobs[job_id]
        job.last_error = error
        if retryable:
            job.status = "queued"
            self.pending.append(job_id)
        else:
            job.status = "failed"

    async def cancel(self, job_id: str) -> None:
        """Any state -> cancelled; never claimable afterwards."""
        self.jobs[job_id].status = "cancelled"


@dataclass(slots=True)
class FakeSecrets:
    """In-memory SecretsPort; refs are sequential and reveal nothing."""

    values: dict[str, str] = field(default_factory=dict)
    counter: int = 0

    async def put(self, value: str) -> str:
        """Store the value under a fresh opaque ref."""
        self.counter += 1
        ref = f"secret://fake/{self.counter}"
        self.values[ref] = value
        return ref

    async def resolve(self, ref: str) -> str:
        """Return the stored value; KeyError for unknown/deleted refs."""
        return self.values[ref]

    async def delete(self, ref: str) -> None:
        """Forget the value behind the ref."""
        self.values.pop(ref, None)


@dataclass(slots=True)
class FakeEventBus:
    """Recording EventBusPort: subscribe replays already-published events.

    Deterministic by design — no live fan-out, so tests never hang waiting
    on a stream. The real bus (M2) streams indefinitely.
    """

    events: list[HarborEvent] = field(default_factory=list)

    async def publish(self, event: HarborEvent) -> None:
        """Record the event."""
        self.events.append(event)

    def subscribe(self, name_prefix: str) -> AsyncIterator[HarborEvent]:
        """Yield recorded events whose name starts with name_prefix."""

        async def _replay() -> AsyncIterator[HarborEvent]:
            for event in list(self.events):
                if event.name.startswith(name_prefix):
                    yield event

        return _replay()
