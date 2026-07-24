"""SQLAlchemy implementations of the core control-plane ports (ST5).

Each repository satisfies its Protocol from harborrag_core.ports.control_plane
(mypy-checked in composition). Rows never leak out — every method maps to the
framework-free domain dataclasses. The secrets/ingestion_failures/mcp_query_log
tables exist in migration 0001 but grow repositories only when their endpoints
land (M2/M4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from harborrag_adapters.repositories.database.control_plane.models import (
    ActivityRow,
    JobEventRow,
    JobRow,
    MemberRow,
    ProjectRow,
    ProviderRow,
    SourceRow,
    WorkspaceSettingsRow,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobCounters, JobStatus, JobType
from harborrag_core.domain.member import Member, Role
from harborrag_core.domain.project import Project, ProjectStats, ProjectStatus
from harborrag_core.domain.provider import Provider, ProviderFamily
from harborrag_core.domain.settings import WorkspaceSettings
from harborrag_core.domain.source_config import SourceConfig, SourceStatus

SessionFactory = async_sessionmaker[AsyncSession]


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp for row bookkeeping."""
    return datetime.now(UTC)


def _project_to_domain(row: ProjectRow) -> Project:
    """Map a projects row to the Project aggregate."""
    return Project(
        id=row.id,
        name=row.name,
        collection=row.collection,
        description=row.description,
        status=cast(ProjectStatus, row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        stats=ProjectStats(
            documents=row.documents,
            chunks=row.chunks,
            size_bytes=row.size_bytes,
            last_sync_at=row.last_sync_at,
        ),
    )


def _source_to_domain(row: SourceRow) -> SourceConfig:
    """Map a sources row to the SourceConfig aggregate."""
    return SourceConfig(
        id=row.id,
        project_id=row.project_id,
        source_type=row.source_type,
        name=row.name,
        config=dict(row.config_json),
        schedule=row.schedule,
        status=cast(SourceStatus, row.status),
        secret_refs=list(row.secret_refs),
    )


def _job_to_domain(row: JobRow) -> Job:
    """Map a jobs row to the Job aggregate."""
    return Job(
        id=row.id,
        source_id=row.source_id,
        project_id=row.project_id,
        job_type=cast(JobType, row.job_type),
        status=cast(JobStatus, row.status),
        dry_run=row.dry_run,
        attempts=row.attempts,
        enqueued_at=row.enqueued_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        counters=JobCounters(
            documents_processed=row.documents_processed,
            chunks_created=row.chunks_created,
            errors=row.errors,
        ),
        last_error=row.last_error,
        payload=dict(row.payload_json),
    )


@dataclass(slots=True)
class SqlProjectRepository:
    """ProjectRepositoryPort over the projects table."""

    sessions: SessionFactory

    async def list(self) -> list[Project]:
        """All projects ordered by creation time."""
        async with self.sessions() as session:
            rows = await session.scalars(sa.select(ProjectRow).order_by(ProjectRow.created_at))
            return [_project_to_domain(row) for row in rows]

    async def get(self, project_id: str) -> Project | None:
        """One project by id, or None."""
        async with self.sessions() as session:
            row = await session.get(ProjectRow, project_id)
            return _project_to_domain(row) if row else None

    async def create(self, project: Project) -> Project:
        """Insert a new projects row from the aggregate."""
        async with self.sessions.begin() as session:
            session.add(
                ProjectRow(
                    id=project.id,
                    name=project.name,
                    description=project.description,
                    collection=project.collection,
                    status=project.status,
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                    documents=project.stats.documents,
                    chunks=project.stats.chunks,
                    size_bytes=project.stats.size_bytes,
                    last_sync_at=project.stats.last_sync_at,
                )
            )
        return project

    async def update(self, project: Project) -> Project:
        """Overwrite the mutable fields of an existing project."""
        async with self.sessions.begin() as session:
            row = await session.get(ProjectRow, project.id)
            if row is None:
                raise KeyError(project.id)
            row.name = project.name
            row.description = project.description
            row.collection = project.collection
            row.status = project.status
            row.updated_at = _utc_now()
            row.documents = project.stats.documents
            row.chunks = project.stats.chunks
            row.size_bytes = project.stats.size_bytes
            row.last_sync_at = project.stats.last_sync_at
        return project

    async def delete(self, project_id: str) -> None:
        """Delete the project row (index tombstones are the engine's job)."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(ProjectRow).where(ProjectRow.id == project_id))


@dataclass(slots=True)
class SqlSourceRepository:
    """SourceRepositoryPort over the sources table."""

    sessions: SessionFactory

    async def list(self, project_id: str | None = None) -> list[SourceConfig]:
        """Sources, optionally scoped to a project."""
        statement = sa.select(SourceRow).order_by(SourceRow.id)
        if project_id is not None:
            statement = statement.where(SourceRow.project_id == project_id)
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [_source_to_domain(row) for row in rows]

    async def get(self, source_id: str) -> SourceConfig | None:
        """One source by id, or None."""
        async with self.sessions() as session:
            row = await session.get(SourceRow, source_id)
            return _source_to_domain(row) if row else None

    async def create(self, source: SourceConfig) -> SourceConfig:
        """Insert a new sources row; config must already carry secret_refs."""
        async with self.sessions.begin() as session:
            session.add(
                SourceRow(
                    id=source.id,
                    project_id=source.project_id,
                    source_type=source.source_type,
                    name=source.name,
                    config_json=dict(source.config),
                    secret_refs=list(source.secret_refs),
                    schedule=source.schedule,
                    status=source.status,
                )
            )
        return source

    async def update(self, source: SourceConfig) -> SourceConfig:
        """Overwrite the configurable fields of an existing source."""
        async with self.sessions.begin() as session:
            row = await session.get(SourceRow, source.id)
            if row is None:
                raise KeyError(source.id)
            row.name = source.name
            row.source_type = source.source_type
            row.config_json = dict(source.config)
            row.secret_refs = list(source.secret_refs)
            row.schedule = source.schedule
            row.status = source.status
        return source

    async def delete(self, source_id: str) -> None:
        """Delete the source row."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(SourceRow).where(SourceRow.id == source_id))


@dataclass(slots=True)
class SqlJobRepository:
    """JobRepositoryPort over jobs + job_events."""

    sessions: SessionFactory

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
    ) -> list[Job]:
        """Jobs newest-first, filtered by status and/or source."""
        statement = sa.select(JobRow).order_by(JobRow.enqueued_at.desc())
        if status is not None:
            statement = statement.where(JobRow.status == status)
        if source_id is not None:
            statement = statement.where(JobRow.source_id == source_id)
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [_job_to_domain(row) for row in rows]

    async def get(self, job_id: str) -> Job | None:
        """One job by id, or None."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job_id)
            return _job_to_domain(row) if row else None

    async def save(self, job: Job) -> Job:
        """Upsert the jobs row from the aggregate."""
        async with self.sessions.begin() as session:
            row = await session.get(JobRow, job.id)
            if row is None:
                row = JobRow(id=job.id, enqueued_at=job.enqueued_at)
                session.add(row)
            row.source_id = job.source_id
            row.project_id = job.project_id
            row.job_type = job.job_type
            row.status = job.status
            row.dry_run = job.dry_run
            row.attempts = job.attempts
            row.payload_json = dict(job.payload)
            row.enqueued_at = job.enqueued_at
            row.started_at = job.started_at
            row.finished_at = job.finished_at
            row.documents_processed = job.counters.documents_processed
            row.chunks_created = job.counters.chunks_created
            row.errors = job.counters.errors
            row.last_error = job.last_error
        return job

    async def append_event(self, job_id: str, event: HarborEvent) -> None:
        """Append the event with the next per-job sequence number."""
        async with self.sessions.begin() as session:
            next_seq = await session.scalar(
                sa.select(sa.func.coalesce(sa.func.max(JobEventRow.seq), 0) + 1).where(
                    JobEventRow.job_id == job_id
                )
            )
            session.add(
                JobEventRow(
                    job_id=job_id,
                    seq=next_seq or 1,
                    name=event.name,
                    trace_id=event.trace_id,
                    payload_json=dict(event.payload),
                    created_at=event.created_at,
                )
            )


@dataclass(slots=True)
class SqlActivityRepository:
    """ActivityRepositoryPort over the append-only activity table."""

    sessions: SessionFactory

    async def append(self, entry: ActivityEntry) -> None:
        """Insert one audit row."""
        async with self.sessions.begin() as session:
            session.add(
                ActivityRow(
                    id=entry.id,
                    actor=entry.actor,
                    verb=entry.verb,
                    entity_type=entry.entity_type,
                    entity_id=entry.entity_id,
                    summary=entry.summary,
                    created_at=entry.created_at,
                )
            )

    async def list(self, limit: int = 50) -> list[ActivityEntry]:
        """Newest entries first, bounded by limit."""
        async with self.sessions() as session:
            rows = await session.scalars(
                sa.select(ActivityRow).order_by(ActivityRow.created_at.desc()).limit(limit)
            )
            return [
                ActivityEntry(
                    id=row.id,
                    actor=row.actor,
                    verb=row.verb,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    summary=row.summary,
                    created_at=row.created_at,
                )
                for row in rows
            ]


@dataclass(slots=True)
class SqlSettingsRepository:
    """SettingsRepositoryPort over the single workspace_settings row (id=1)."""

    sessions: SessionFactory

    async def get(self) -> WorkspaceSettings:
        """The settings document; empty document when never written."""
        async with self.sessions() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            return WorkspaceSettings(data=dict(row.data)) if row else WorkspaceSettings()

    async def put(self, settings: WorkspaceSettings) -> WorkspaceSettings:
        """Upsert the settings document."""
        async with self.sessions.begin() as session:
            row = await session.get(WorkspaceSettingsRow, 1)
            if row is None:
                row = WorkspaceSettingsRow(id=1, updated_at=_utc_now())
                session.add(row)
            row.data = dict(settings.data)
            row.updated_at = _utc_now()
        return settings


@dataclass(slots=True)
class SqlProviderRepository:
    """ProviderRepositoryPort over the providers table."""

    sessions: SessionFactory

    async def list(self) -> list[Provider]:
        """All providers ordered by id."""
        async with self.sessions() as session:
            rows = await session.scalars(sa.select(ProviderRow).order_by(ProviderRow.id))
            return [self._to_domain(row) for row in rows]

    async def get(self, provider_id: str) -> Provider | None:
        """One provider by id, or None."""
        async with self.sessions() as session:
            row = await session.get(ProviderRow, provider_id)
            return self._to_domain(row) if row else None

    async def save(self, provider: Provider) -> Provider:
        """Upsert the provider row."""
        async with self.sessions.begin() as session:
            row = await session.get(ProviderRow, provider.id)
            if row is None:
                row = ProviderRow(id=provider.id)
                session.add(row)
            row.name = provider.name
            row.family = provider.family
            row.config_json = dict(provider.config)
            row.secret_ref = provider.secret_ref
        return provider

    async def delete(self, provider_id: str) -> None:
        """Delete the provider row."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(ProviderRow).where(ProviderRow.id == provider_id))

    @staticmethod
    def _to_domain(row: ProviderRow) -> Provider:
        """Map a providers row to the Provider aggregate."""
        return Provider(
            id=row.id,
            name=row.name,
            family=cast(ProviderFamily, row.family),
            config=dict(row.config_json),
            secret_ref=row.secret_ref,
        )


@dataclass(slots=True)
class SqlMemberRepository:
    """MemberRepositoryPort over the members table."""

    sessions: SessionFactory

    async def list(self) -> list[Member]:
        """All members ordered by subject."""
        async with self.sessions() as session:
            rows = await session.scalars(sa.select(MemberRow).order_by(MemberRow.subject))
            return [self._to_domain(row) for row in rows]

    async def get_by_subject(self, subject: str) -> Member | None:
        """Member by auth subject (unique), or None."""
        async with self.sessions() as session:
            row = await session.scalar(sa.select(MemberRow).where(MemberRow.subject == subject))
            return self._to_domain(row) if row else None

    async def save(self, member: Member) -> Member:
        """Upsert the membership row."""
        async with self.sessions.begin() as session:
            row = await session.get(MemberRow, member.id)
            if row is None:
                row = MemberRow(id=member.id, created_at=_utc_now())
                session.add(row)
            row.subject = member.subject
            row.role = member.role
        return member

    async def delete(self, member_id: str) -> None:
        """Delete the membership row."""
        async with self.sessions.begin() as session:
            await session.execute(sa.delete(MemberRow).where(MemberRow.id == member_id))

    @staticmethod
    def _to_domain(row: MemberRow) -> Member:
        """Map a members row to the Member aggregate."""
        return Member(id=row.id, subject=row.subject, role=cast(Role, row.role))
