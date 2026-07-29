"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from harborrag_adapters.repositories.database.control_plane.schemas import (
    ActivityRow,
    JobEventRow,
    JobRow,
)
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobStatus

from .mapping import job_to_domain
from .session import SessionFactory


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
            return [job_to_domain(row) for row in rows]

    async def get(self, job_id: str) -> Job | None:
        """One job by id, or None."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job_id)
            return job_to_domain(row) if row else None

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

    async def count_by_status(self) -> dict[str, int]:
        """Job counts grouped by status via SQL GROUP BY, not a full-table load."""
        statement = sa.select(JobRow.status, sa.func.count()).group_by(JobRow.status)
        async with self.sessions() as session:
            rows = await session.execute(statement)
            return {status: count for status, count in rows.all()}


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
