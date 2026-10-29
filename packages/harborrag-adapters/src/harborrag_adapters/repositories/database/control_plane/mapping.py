from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from harborrag_adapters.repositories.database.control_plane.schemas import (
    JobRow,
    ProjectRow,
    SourceRow,
)
from harborrag_core.domain.connection import Connection, ConnectionStatus
from harborrag_core.domain.job import Job, JobCounters, JobStatus, JobType
from harborrag_core.domain.project import Project, ProjectStats, ProjectStatus


def utc_now() -> datetime:
    """Return a timezone-aware control-plane bookkeeping timestamp."""
    return datetime.now(UTC)


def project_to_domain(row: ProjectRow) -> Project:
    return Project(
        id=row.id,
        tenant_id=row.tenant_id,
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


def connection_to_domain(row: SourceRow) -> Connection:
    return Connection(
        id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        connection_id=row.connection_id,
        source_key=row.source_key,
        source_type=row.source_type,
        name=row.name,
        credentials=dict(row.credentials),
        config=dict(row.config_json),
        tuning=dict(row.tuning),
        secret_refs=list(row.secret_refs),
        status=cast(ConnectionStatus, row.status),
        status_detail=dict(row.status_detail) if row.status_detail is not None else None,
        schedule=row.schedule,
        version=row.version,
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def job_to_domain(row: JobRow) -> Job:
    return Job(
        id=row.id,
        tenant_id=row.tenant_id,
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
