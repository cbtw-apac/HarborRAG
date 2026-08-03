from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import insert, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.chunking import encoded_identifier
from harborrag_core.contracts import HarborNotFoundError
from harborrag_core.ingestion import (
    CleanupJobState,
    DocumentFailure,
    ProjectionCleanupJob,
    ProjectionManifest,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .row_mapping import cleanup_job_from_row
from .schema import (
    DOCUMENT_FAILURES,
    PROJECTION_CLEANUP_JOBS,
    PROJECTION_MANIFESTS,
    SOURCE_ITEMS,
)


class IngestionReliabilityRepository:
    """Persist safe failures and version-addressed cleanup work."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def record_failure(self, failure: DocumentFailure) -> None:
        failure_id = encoded_identifier(
            "document-failure",
            {
                "document_version_id": str(failure.document_version_id),
                "failed_stage": failure.failed_stage,
                "safe_error_code": failure.safe_error_code,
            },
        )
        now = utc_now()
        artifacts = [reference.model_dump(mode="json") for reference in failure.artifact_references]
        manifest = (
            failure.projection_manifest_reference.model_dump(mode="json")
            if failure.projection_manifest_reference is not None
            else None
        )
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(DOCUMENT_FAILURES)
                .where(DOCUMENT_FAILURES.c.failure_id == failure_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                await session.execute(
                    insert(DOCUMENT_FAILURES).values(
                        failure_id=failure_id,
                        document_id=str(failure.document_id),
                        document_version_id=str(failure.document_version_id),
                        failed_stage=failure.failed_stage,
                        failure_category=failure.category.value,
                        retryable=failure.retryable,
                        safe_error_code=failure.safe_error_code,
                        artifact_references=artifacts,
                        projection_manifest_reference=manifest,
                        first_failure_at=now,
                        last_failure_at=now,
                        attempt_count=1,
                    )
                )
                return
            await session.execute(
                update(DOCUMENT_FAILURES)
                .where(DOCUMENT_FAILURES.c.failure_id == failure_id)
                .values(
                    retryable=failure.retryable,
                    artifact_references=artifacts,
                    projection_manifest_reference=manifest,
                    last_failure_at=now,
                    attempt_count=row["attempt_count"] + 1,
                )
            )

    async def enqueue_cleanup(
        self,
        *,
        document_id: str,
        document_version_id: str,
    ) -> ProjectionCleanupJob:
        cleanup_id = encoded_identifier(
            "projection-cleanup",
            {
                "document_id": document_id,
                "document_version_id": document_version_id,
            },
        )
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(PROJECTION_CLEANUP_JOBS).where(
                    PROJECTION_CLEANUP_JOBS.c.document_version_id == document_version_id
                )
            )
            row = result.mappings().one_or_none()
            if row is None:
                await session.execute(
                    insert(PROJECTION_CLEANUP_JOBS).values(
                        cleanup_job_id=cleanup_id,
                        document_id=document_id,
                        document_version_id=document_version_id,
                        status=CleanupJobState.PENDING.value,
                        attempt_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                return ProjectionCleanupJob(
                    cleanup_job_id=cleanup_id,
                    document_id=DocumentId(document_id),
                    document_version_id=DocumentVersionId(document_version_id),
                    status=CleanupJobState.PENDING,
                    attempt_count=0,
                )
            if row["status"] in {
                CleanupJobState.CANCELLED.value,
                CleanupJobState.COMPLETED.value,
            }:
                await session.execute(
                    update(PROJECTION_CLEANUP_JOBS)
                    .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup_id)
                    .values(
                        status=CleanupJobState.PENDING.value,
                        last_error_code=None,
                        completed_at=None,
                        updated_at=now,
                    )
                )
                return ProjectionCleanupJob(
                    cleanup_job_id=cleanup_id,
                    document_id=DocumentId(document_id),
                    document_version_id=DocumentVersionId(document_version_id),
                    status=CleanupJobState.PENDING,
                    attempt_count=row["attempt_count"],
                )
            return cleanup_job_from_row(row)

    async def pending_cleanup_jobs(
        self,
        *,
        limit: int = 100,
        document_ids: Sequence[str] | None = None,
        source_scope_id: str | None = None,
    ) -> tuple[ProjectionCleanupJob, ...]:
        if not 1 <= limit <= 1_000:
            raise ValueError("cleanup job limit must be between 1 and 1000")
        if document_ids is not None and source_scope_id is not None:
            raise ValueError("cleanup jobs can be filtered by documents or source scope, not both")
        if source_scope_id is not None and not source_scope_id.strip():
            raise ValueError("cleanup source_scope_id must be non-empty")
        if document_ids is not None and not document_ids:
            return ()
        statement = select(PROJECTION_CLEANUP_JOBS).where(
            PROJECTION_CLEANUP_JOBS.c.status.in_(
                (
                    CleanupJobState.PENDING.value,
                    CleanupJobState.FAILED.value,
                )
            )
        )
        if document_ids is not None:
            statement = statement.where(
                PROJECTION_CLEANUP_JOBS.c.document_id.in_(tuple(dict.fromkeys(document_ids)))
            )
        elif source_scope_id is not None:
            statement = statement.join(
                SOURCE_ITEMS,
                SOURCE_ITEMS.c.document_id == PROJECTION_CLEANUP_JOBS.c.document_id,
            ).where(SOURCE_ITEMS.c.source_scope_id == source_scope_id)
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.distinct().order_by(PROJECTION_CLEANUP_JOBS.c.created_at).limit(limit)
            )
            return tuple(cleanup_job_from_row(row) for row in result.mappings().all())

    async def cleanup_for_version(
        self,
        document_version_id: str,
    ) -> ProjectionCleanupJob | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(PROJECTION_CLEANUP_JOBS).where(
                    PROJECTION_CLEANUP_JOBS.c.document_version_id == document_version_id
                )
            )
            row = result.mappings().one_or_none()
            return cleanup_job_from_row(row) if row is not None else None

    async def start_cleanup(self, cleanup_job_id: str) -> None:
        if not await self.claim_cleanup(cleanup_job_id):
            raise RuntimeError("projection cleanup job is not claimable")

    async def claim_cleanup(self, cleanup_job_id: str) -> bool:
        """Atomically claim pending work so only one worker deletes a version."""

        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(PROJECTION_CLEANUP_JOBS)
                .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup_job_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(
                    f"projection cleanup job does not exist: {cleanup_job_id}"
                )
            if row["status"] not in {
                CleanupJobState.PENDING.value,
                CleanupJobState.FAILED.value,
            }:
                return False
            await session.execute(
                update(PROJECTION_CLEANUP_JOBS)
                .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup_job_id)
                .values(
                    status=CleanupJobState.RUNNING.value,
                    attempt_count=row["attempt_count"] + 1,
                    last_error_code=None,
                    updated_at=now,
                )
            )
            return True

    async def complete_cleanup(self, cleanup_job_id: str) -> None:
        await self._set_cleanup_state(
            cleanup_job_id,
            status=CleanupJobState.COMPLETED,
            completed=True,
        )

    async def cancel_cleanup(
        self,
        cleanup_job_id: str,
        *,
        safe_reason_code: str,
    ) -> None:
        if not safe_reason_code.strip():
            raise ValueError("cleanup safe_reason_code must be non-empty")
        await self._set_cleanup_state(
            cleanup_job_id,
            status=CleanupJobState.CANCELLED,
            completed=True,
            last_error_code=safe_reason_code,
        )

    async def fail_cleanup(
        self,
        cleanup_job_id: str,
        *,
        safe_error_code: str,
    ) -> None:
        if not safe_error_code.strip():
            raise ValueError("cleanup safe_error_code must be non-empty")
        await self._set_cleanup_state(
            cleanup_job_id,
            status=CleanupJobState.FAILED,
            last_error_code=safe_error_code,
        )

    async def projection_manifest(
        self,
        document_version_id: str,
    ) -> ProjectionManifest | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(PROJECTION_MANIFESTS.c.manifest).where(
                    PROJECTION_MANIFESTS.c.document_version_id == document_version_id
                )
            )
            value = result.scalar_one_or_none()
            return ProjectionManifest.model_validate(value) if value is not None else None

    async def _set_cleanup_state(
        self,
        cleanup_job_id: str,
        *,
        status: CleanupJobState,
        increment_attempt: bool = False,
        completed: bool = False,
        last_error_code: str | None = None,
    ) -> None:
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(PROJECTION_CLEANUP_JOBS)
                .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup_job_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(
                    f"projection cleanup job does not exist: {cleanup_job_id}"
                )
            values: dict[str, object] = {
                "status": status.value,
                "updated_at": now,
                "last_error_code": last_error_code,
            }
            if increment_attempt:
                values["attempt_count"] = row["attempt_count"] + 1
            if completed:
                values["completed_at"] = now
            await session.execute(
                update(PROJECTION_CLEANUP_JOBS)
                .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup_job_id)
                .values(**values)
            )
