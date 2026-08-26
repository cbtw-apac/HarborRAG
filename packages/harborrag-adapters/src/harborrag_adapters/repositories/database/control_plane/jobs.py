"""SQLAlchemy control-plane repositories grouped by capability."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.control_plane.schemas import (
    ActivityRow,
    JobEventRow,
    JobRow,
)
from harborrag_core.contracts.errors import HarborConflictError, HarborNotFoundError
from harborrag_core.contracts.events import HarborEvent
from harborrag_core.domain.activity import ActivityEntry
from harborrag_core.domain.job import Job, JobStatus

from .mapping import job_to_domain
from .session import SessionFactory

logger = logging.getLogger("harborrag.adapters.control_plane.activity")


@dataclass(slots=True)
class SqlJobRepository:
    """JobRepositoryPort over jobs + job_events."""

    sessions: SessionFactory

    async def list(
        self,
        status: JobStatus | None = None,
        source_id: str | None = None,
        *,
        tenant_ids: frozenset[str] | None,
    ) -> list[Job]:
        """Jobs visible to ``tenant_ids`` (None: unrestricted), newest-first."""
        statement = sa.select(JobRow).order_by(JobRow.enqueued_at.desc())
        if status is not None:
            statement = statement.where(JobRow.status == status)
        if source_id is not None:
            statement = statement.where(JobRow.source_id == source_id)
        if tenant_ids is not None:
            statement = statement.where(JobRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [job_to_domain(row) for row in rows]

    async def get(self, job_id: str, *, tenant_ids: frozenset[str] | None) -> Job | None:
        """One job by id within ``tenant_ids``, or None."""
        async with self.sessions() as session:
            row = await session.get(JobRow, job_id)
            if row is None or (tenant_ids is not None and row.tenant_id not in tenant_ids):
                return None
            return job_to_domain(row)

    async def save(self, job: Job) -> Job:
        """Upsert the jobs row from the aggregate."""
        async with self.sessions.begin() as session:
            row = await session.get(JobRow, job.id)
            if row is None:
                row = JobRow(id=job.id, tenant_id=job.tenant_id, enqueued_at=job.enqueued_at)
                session.add(row)
            elif row.tenant_id != job.tenant_id:
                raise HarborConflictError("job tenant identity is immutable")
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
            # The counter update is one database statement, so concurrent
            # appenders cannot observe and allocate the same sequence number.
            result = await session.execute(
                sa.update(JobRow)
                .where(JobRow.id == job_id)
                .values(event_sequence=JobRow.event_sequence + 1)
                .returning(JobRow.event_sequence)
            )
            next_seq = result.scalar_one_or_none()
            if next_seq is None:
                raise HarborNotFoundError(f"job does not exist: {job_id}")
            session.add(
                JobEventRow(
                    job_id=job_id,
                    seq=next_seq,
                    name=event.name,
                    trace_id=event.trace_id,
                    payload_json=dict(event.payload),
                    created_at=event.created_at,
                )
            )

    async def count_by_status(self, *, tenant_ids: frozenset[str] | None) -> dict[str, int]:
        """Job counts within ``tenant_ids`` grouped by status via SQL GROUP BY."""
        statement = sa.select(JobRow.status, sa.func.count()).group_by(JobRow.status)
        if tenant_ids is not None:
            statement = statement.where(JobRow.tenant_id.in_(tenant_ids))
        async with self.sessions() as session:
            rows = await session.execute(statement)
            counts: dict[str, int] = {}
            for status, count in rows.all():
                counts[status] = count
            return counts


@dataclass(slots=True)
class SqlActivityRepository:
    """ActivityRepositoryPort over the append-only activity table."""

    sessions: SessionFactory

    async def append(self, entry: ActivityEntry) -> None:
        """Insert one audit row; a no-op if this id is already recorded.

        A pending-effect replay (see effect_recovery.py) reuses the
        original entry's id, so two recovery drains racing to replay the
        same effect both attempt this insert -- the table's primary key on
        id turns the loser's attempt into an IntegrityError rather than a
        duplicate row, and that's treated as success, not a retry-worthy
        failure.
        """
        try:
            async with self.sessions.begin() as session:
                session.add(
                    ActivityRow(
                        id=entry.id,
                        tenant_id=entry.tenant_id,
                        actor=entry.actor,
                        verb=entry.verb,
                        entity_type=entry.entity_type,
                        entity_id=entry.entity_id,
                        summary=entry.summary,
                        created_at=entry.created_at,
                    )
                )
        except IntegrityError:
            logger.info(
                "activity entry id=%s already recorded; treating append as a no-op", entry.id
            )

    async def list(
        self, limit: int = 50, *, tenant_ids: frozenset[str] | None
    ) -> list[ActivityEntry]:
        """Newest entries within ``tenant_ids`` first, bounded by limit.

        Tenant filtering happens before the limit is applied so a caller
        never gets a truncated-to-empty page because unrelated tenants'
        entries filled the window.
        """
        statement = sa.select(ActivityRow).order_by(ActivityRow.created_at.desc())
        if tenant_ids is not None:
            statement = statement.where(ActivityRow.tenant_id.in_(tenant_ids))
        statement = statement.limit(limit)
        async with self.sessions() as session:
            rows = await session.scalars(statement)
            return [
                ActivityEntry(
                    id=row.id,
                    tenant_id=row.tenant_id,
                    actor=row.actor,
                    verb=row.verb,
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    summary=row.summary,
                    created_at=row.created_at,
                )
                for row in rows
            ]
