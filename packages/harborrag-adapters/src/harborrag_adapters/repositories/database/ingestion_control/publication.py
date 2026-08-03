from __future__ import annotations

from datetime import datetime

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.chunking import encoded_identifier
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import (
    CleanupJobState,
    DocumentRetirementResult,
    DocumentVersionState,
    PublicationResult,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .schema import DOCUMENT_VERSIONS, DOCUMENTS, PROJECTION_CLEANUP_JOBS


class DocumentVersionPublisher:
    """Atomically select the sole authoritative document version in Postgres."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def publish(
        self,
        *,
        document_id: str,
        candidate_document_version_id: str,
    ) -> PublicationResult:
        async with self._client.sessions.begin() as session:
            document_result = await session.execute(
                select(DOCUMENTS).where(DOCUMENTS.c.document_id == document_id).with_for_update()
            )
            document = document_result.mappings().one_or_none()
            if document is None:
                raise HarborNotFoundError(f"document does not exist: {document_id}")
            candidate_result = await session.execute(
                select(DOCUMENT_VERSIONS)
                .where(
                    DOCUMENT_VERSIONS.c.document_version_id == candidate_document_version_id,
                    DOCUMENT_VERSIONS.c.document_id == document_id,
                )
                .with_for_update()
            )
            candidate = candidate_result.mappings().one_or_none()
            if candidate is None:
                raise HarborNotFoundError(
                    f"document version does not exist: {candidate_document_version_id}"
                )
            current_active = document["active_document_version_id"]
            candidate_state = DocumentVersionState(candidate["status"])
            if (
                candidate_state == DocumentVersionState.ACTIVE
                and current_active == candidate_document_version_id
            ):
                return PublicationResult(
                    document_id=DocumentId(document_id),
                    active_document_version_id=DocumentVersionId(candidate_document_version_id),
                    replayed=True,
                )
            if candidate_state != DocumentVersionState.VERIFIED:
                raise HarborConflictError("only a VERIFIED document version can be published")

            now = utc_now()
            retired_version_id = (
                current_active
                if current_active is not None and current_active != candidate_document_version_id
                else None
            )
            if retired_version_id is not None:
                await session.execute(
                    update(DOCUMENT_VERSIONS)
                    .where(
                        DOCUMENT_VERSIONS.c.document_version_id == retired_version_id,
                        DOCUMENT_VERSIONS.c.document_id == document_id,
                        DOCUMENT_VERSIONS.c.status == DocumentVersionState.ACTIVE.value,
                    )
                    .values(
                        status=DocumentVersionState.RETIRED.value,
                        retired_at=now,
                        updated_at=now,
                    )
                )
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == candidate_document_version_id)
                .values(
                    status=DocumentVersionState.ACTIVE.value,
                    activated_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.document_id == document_id)
                .values(
                    active_document_version_id=candidate_document_version_id,
                    updated_at=now,
                )
            )
            cleanup_created = False
            if retired_version_id is not None:
                cleanup_created = await self._ensure_cleanup(
                    session,
                    document_id=document_id,
                    document_version_id=retired_version_id,
                    now=now,
                )
            return PublicationResult(
                document_id=DocumentId(document_id),
                active_document_version_id=DocumentVersionId(candidate_document_version_id),
                retired_document_version_id=(
                    DocumentVersionId(retired_version_id)
                    if retired_version_id is not None
                    else None
                ),
                cleanup_job_created=cleanup_created,
            )

    async def retire_removed(
        self,
        *,
        document_id: str,
    ) -> DocumentRetirementResult:
        """Retire one removed source without deleting canonical artifacts."""

        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(DOCUMENTS).where(DOCUMENTS.c.document_id == document_id).with_for_update()
            )
            document = result.mappings().one_or_none()
            if document is None:
                return DocumentRetirementResult(
                    document_id=DocumentId(document_id),
                    replayed=True,
                )
            active_version = document["active_document_version_id"]
            if active_version is None:
                return DocumentRetirementResult(
                    document_id=DocumentId(document_id),
                    replayed=True,
                )
            now = utc_now()
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(
                    DOCUMENT_VERSIONS.c.document_version_id == active_version,
                    DOCUMENT_VERSIONS.c.document_id == document_id,
                    DOCUMENT_VERSIONS.c.status == DocumentVersionState.ACTIVE.value,
                )
                .values(
                    status=DocumentVersionState.RETIRED.value,
                    retired_at=now,
                    updated_at=now,
                )
            )
            await session.execute(
                update(DOCUMENTS)
                .where(DOCUMENTS.c.document_id == document_id)
                .values(active_document_version_id=None, updated_at=now)
            )
            cleanup_created = await self._ensure_cleanup(
                session,
                document_id=document_id,
                document_version_id=active_version,
                now=now,
            )
            return DocumentRetirementResult(
                document_id=DocumentId(document_id),
                retired_document_version_id=DocumentVersionId(active_version),
                cleanup_job_created=cleanup_created,
            )

    @staticmethod
    async def _ensure_cleanup(
        session: AsyncSession,
        *,
        document_id: str,
        document_version_id: str,
        now: datetime,
    ) -> bool:
        cleanup_id = encoded_identifier(
            "projection-cleanup",
            {
                "document_id": document_id,
                "document_version_id": document_version_id,
            },
        )
        result = await session.execute(
            select(PROJECTION_CLEANUP_JOBS).where(
                PROJECTION_CLEANUP_JOBS.c.document_version_id == document_version_id
            )
        )
        cleanup = result.mappings().one_or_none()
        if cleanup is None:
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
            return True
        if cleanup["status"] not in {
            CleanupJobState.CANCELLED.value,
            CleanupJobState.COMPLETED.value,
        }:
            return False
        await session.execute(
            update(PROJECTION_CLEANUP_JOBS)
            .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup["cleanup_job_id"])
            .values(
                status=CleanupJobState.PENDING.value,
                last_error_code=None,
                completed_at=None,
                updated_at=now,
            )
        )
        return True
