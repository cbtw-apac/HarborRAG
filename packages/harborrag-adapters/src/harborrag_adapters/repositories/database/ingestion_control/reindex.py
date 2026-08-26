from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import insert, select, update

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import (
    ReindexJob,
    ReindexJobState,
    ReindexProgress,
)
from harborrag_core.schemas.ids import DocumentId

from .schema import DOCUMENT_VERSIONS, DOCUMENTS, REINDEX_JOBS


class ReindexJobRepository:
    """Persist connector-free reindex intent and replay-safe aggregate progress."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def submit(
        self,
        *,
        reindex_job_id: str,
        target_processing_fingerprint: str,
        document_id: str | None = None,
    ) -> ReindexJob:
        if not reindex_job_id.strip() or not target_processing_fingerprint.strip():
            raise ValueError("reindex job and processing fingerprint must be non-empty")
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(REINDEX_JOBS)
                .where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is not None:
                if (
                    row["document_id"] != document_id
                    or row["target_processing_fingerprint"] != target_processing_fingerprint
                ):
                    raise HarborConflictError("reindex job identity is immutable")
                return self._job(dict(row))
            await session.execute(
                insert(REINDEX_JOBS).values(
                    reindex_job_id=reindex_job_id,
                    document_id=document_id,
                    status=ReindexJobState.PENDING.value,
                    target_processing_fingerprint=target_processing_fingerprint,
                    connector_call_count=0,
                    scanned_count=0,
                    processed_count=0,
                    published_count=0,
                    skipped_count=0,
                    failure_count=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        return ReindexJob(
            reindex_job_id=reindex_job_id,
            document_id=(DocumentId(document_id) if document_id is not None else None),
            status=ReindexJobState.PENDING,
            target_processing_fingerprint=target_processing_fingerprint,
        )

    async def start(self, reindex_job_id: str) -> ReindexJob:
        now = utc_now()
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(REINDEX_JOBS)
                .where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"reindex job does not exist: {reindex_job_id}")
            status = ReindexJobState(row["status"])
            if status in {
                ReindexJobState.RUNNING,
                ReindexJobState.COMPLETED,
            }:
                return self._job(dict(row))
            if status == ReindexJobState.CANCELLED:
                raise HarborConflictError("cancelled reindex jobs cannot be restarted")
            await session.execute(
                update(REINDEX_JOBS)
                .where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
                .values(
                    status=ReindexJobState.RUNNING.value,
                    last_error_code=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            return self._job(
                {
                    **row,
                    "status": ReindexJobState.RUNNING.value,
                    "last_error_code": None,
                }
            )

    async def finish(
        self,
        reindex_job_id: str,
        *,
        progress: ReindexProgress,
        last_error_code: str | None = None,
    ) -> ReindexJob:
        values = progress.model_dump()
        now = utc_now()
        status = ReindexJobState.FAILED if progress.failure_count else ReindexJobState.COMPLETED
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(REINDEX_JOBS)
                .where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"reindex job does not exist: {reindex_job_id}")
            await session.execute(
                update(REINDEX_JOBS)
                .where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
                .values(
                    status=status.value,
                    connector_call_count=0,
                    last_error_code=last_error_code,
                    completed_at=now,
                    updated_at=now,
                    **values,
                )
            )
            return self._job(
                {
                    **row,
                    **values,
                    "status": status.value,
                    "connector_call_count": 0,
                    "last_error_code": last_error_code,
                }
            )

    async def get(self, reindex_job_id: str) -> ReindexJob | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(REINDEX_JOBS).where(REINDEX_JOBS.c.reindex_job_id == reindex_job_id)
            )
            row = result.mappings().one_or_none()
            return self._job(dict(row)) if row is not None else None

    async def stale_active_document_ids(
        self,
        *,
        tenant_id: str,
        target_processing_fingerprint: str,
        document_id: str | None = None,
        limit: int = 10_000,
    ) -> tuple[str, ...]:
        if not 1 <= limit <= 100_000:
            raise ValueError("reindex scan limit must be between 1 and 100000")
        if not tenant_id.strip():
            raise ValueError("reindex tenant_id must be non-empty")
        query = (
            select(DOCUMENTS.c.document_id)
            .join(
                DOCUMENT_VERSIONS,
                DOCUMENT_VERSIONS.c.document_version_id == DOCUMENTS.c.active_document_version_id,
            )
            .where(
                DOCUMENTS.c.tenant_id == tenant_id,
                DOCUMENT_VERSIONS.c.processing_fingerprint != target_processing_fingerprint,
            )
            .order_by(DOCUMENTS.c.document_id)
            .limit(limit)
        )
        if document_id is not None:
            query = query.where(DOCUMENTS.c.document_id == document_id)
        async with self._client.sessions() as session:
            result = await session.execute(query)
            return tuple(result.scalars().all())

    @staticmethod
    def _job(row: Mapping[str, Any]) -> ReindexJob:
        document_id = row["document_id"]
        return ReindexJob(
            reindex_job_id=str(row["reindex_job_id"]),
            document_id=(DocumentId(str(document_id)) if document_id is not None else None),
            status=ReindexJobState(row["status"]),
            target_processing_fingerprint=str(row["target_processing_fingerprint"]),
            connector_call_count=int(row["connector_call_count"]),
            scanned_count=int(row["scanned_count"]),
            processed_count=int(row["processed_count"]),
            published_count=int(row["published_count"]),
            skipped_count=int(row["skipped_count"]),
            failure_count=int(row["failure_count"]),
            last_error_code=(
                str(row["last_error_code"]) if row["last_error_code"] is not None else None
            ),
        )
