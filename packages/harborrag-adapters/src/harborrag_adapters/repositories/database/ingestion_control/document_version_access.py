from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from harborrag_adapters.repositories.backends.sqlalchemy import SQLAlchemyDBClient
from harborrag_core.base import utc_now
from harborrag_core.contracts import HarborConflictError, HarborNotFoundError
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    ActiveSourceDocument,
    CleanupJobState,
    DocumentVersionSnapshot,
    DocumentVersionState,
)
from harborrag_core.schemas.ids import DocumentId, DocumentVersionId

from .document_version_mapping import replay_state_from_row, snapshot_from_row
from .row_values import DatabaseRow, required_mapping, required_text
from .schema import (
    DOCUMENT_VERSIONS,
    DOCUMENTS,
    PROJECTION_CLEANUP_JOBS,
    SOURCE_ITEMS,
)


class DocumentVersionReader:
    """Resolve published and durable document-version views."""

    def __init__(self, client: SQLAlchemyDBClient) -> None:
        self._client = client

    async def active_versions(
        self,
        document_ids: Sequence[str],
    ) -> dict[str, ActiveDocumentVersion]:
        if not document_ids:
            return {}
        async with self._client.sessions() as session:
            result = await session.execute(
                select(
                    DOCUMENTS.c.document_id,
                    DOCUMENTS.c.active_document_version_id,
                ).where(DOCUMENTS.c.document_id.in_(tuple(document_ids)))
            )
            return {
                row["document_id"]: ActiveDocumentVersion(
                    document_id=row["document_id"],
                    document_version_id=row["active_document_version_id"],
                )
                for row in result.mappings().all()
                if row["active_document_version_id"] is not None
            }

    async def get_version(
        self,
        document_version_id: str,
    ) -> DocumentVersionSnapshot | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(DOCUMENT_VERSIONS).where(
                    DOCUMENT_VERSIONS.c.document_version_id == document_version_id
                )
            )
            row = result.mappings().one_or_none()
            return snapshot_from_row(row) if row is not None else None

    async def active_snapshot(
        self,
        document_id: str,
    ) -> DocumentVersionSnapshot | None:
        async with self._client.sessions() as session:
            result = await session.execute(
                select(DOCUMENT_VERSIONS)
                .join(
                    DOCUMENTS,
                    DOCUMENTS.c.active_document_version_id
                    == DOCUMENT_VERSIONS.c.document_version_id,
                )
                .where(DOCUMENTS.c.document_id == document_id)
            )
            row = result.mappings().one_or_none()
            return snapshot_from_row(row) if row is not None else None

    async def resolve_active_sources(
        self,
        *,
        source_scope_id: str,
        source_item_ids: Sequence[str],
    ) -> dict[str, ActiveSourceDocument]:
        if not source_item_ids:
            return {}
        async with self._client.sessions() as session:
            result = await session.execute(
                select(
                    SOURCE_ITEMS.c.source_item_id,
                    SOURCE_ITEMS.c.source_scope_id,
                    SOURCE_ITEMS.c.descriptor,
                    DOCUMENTS.c.document_id,
                    DOCUMENTS.c.active_document_version_id,
                )
                .join(DOCUMENTS, DOCUMENTS.c.document_id == SOURCE_ITEMS.c.document_id)
                .where(
                    SOURCE_ITEMS.c.source_scope_id == source_scope_id,
                    SOURCE_ITEMS.c.source_item_id.in_(tuple(source_item_ids)),
                    SOURCE_ITEMS.c.is_active.is_(True),
                    DOCUMENTS.c.active_document_version_id.is_not(None),
                )
            )
            return {
                row["source_item_id"]: _active_source_from_row(row)
                for row in result.mappings().all()
            }

    async def active_relation_document_ids(
        self,
        *,
        processing_fingerprint: str,
        anchor_document_id: str | None = None,
        limit: int = 100_000,
    ) -> tuple[str, ...]:
        """Select active documents whose source edges may need endpoint repair."""

        if not processing_fingerprint.strip():
            raise ValueError("processing fingerprint must be non-empty")
        if not 1 <= limit <= 100_000:
            raise ValueError("relation repair limit must be between 1 and 100000")
        statement = (
            select(DOCUMENTS.c.document_id)
            .join(SOURCE_ITEMS, SOURCE_ITEMS.c.document_id == DOCUMENTS.c.document_id)
            .join(
                DOCUMENT_VERSIONS,
                DOCUMENT_VERSIONS.c.document_version_id == DOCUMENTS.c.active_document_version_id,
            )
            .where(
                SOURCE_ITEMS.c.is_active.is_(True),
                DOCUMENT_VERSIONS.c.processing_fingerprint == processing_fingerprint,
            )
        )
        if anchor_document_id is not None:
            anchor_items = SOURCE_ITEMS.alias("relation_repair_anchor")
            anchor_scopes = select(anchor_items.c.source_scope_id).where(
                anchor_items.c.document_id == anchor_document_id,
                anchor_items.c.is_active.is_(True),
            )
            statement = statement.where(SOURCE_ITEMS.c.source_scope_id.in_(anchor_scopes))
        async with self._client.sessions() as session:
            result = await session.execute(
                statement.distinct().order_by(DOCUMENTS.c.document_id).limit(limit)
            )
            return tuple(result.scalars().all())


class DocumentVersionReplay:
    """Restore replayable versions without replacing immutable artifacts."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        reader: DocumentVersionReader,
    ) -> None:
        self._client = client
        self._reader = reader

    async def prepare(self, document_version_id: str) -> DocumentVersionState:
        async with self._client.sessions.begin() as session:
            result = await session.execute(
                select(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .with_for_update()
            )
            row = result.mappings().one_or_none()
            if row is None:
                raise HarborNotFoundError(f"document version does not exist: {document_version_id}")
            current = DocumentVersionState(row["status"])
            if current not in {
                DocumentVersionState.FAILED,
                DocumentVersionState.RETIRED,
            }:
                return current
            cleanup = await self._locked_cleanup(session, document_version_id)
            if cleanup is not None and cleanup["status"] == CleanupJobState.RUNNING.value:
                raise HarborConflictError("document version cleanup is currently running")
            restored = replay_state_from_row(row)
            now = utc_now()
            await session.execute(
                update(DOCUMENT_VERSIONS)
                .where(DOCUMENT_VERSIONS.c.document_version_id == document_version_id)
                .values(status=restored.value, updated_at=now)
            )
            if cleanup is not None and cleanup["status"] in {
                CleanupJobState.PENDING.value,
                CleanupJobState.FAILED.value,
            }:
                await session.execute(
                    update(PROJECTION_CLEANUP_JOBS)
                    .where(PROJECTION_CLEANUP_JOBS.c.cleanup_job_id == cleanup["cleanup_job_id"])
                    .values(
                        status=CleanupJobState.CANCELLED.value,
                        last_error_code=None,
                        completed_at=now,
                        updated_at=now,
                    )
                )
            return restored

    async def resume_failed(self, document_version_id: str) -> DocumentVersionState:
        snapshot = await self._reader.get_version(document_version_id)
        if snapshot is None:
            raise HarborNotFoundError(f"document version does not exist: {document_version_id}")
        if snapshot.state != DocumentVersionState.FAILED:
            return snapshot.state
        return await self.prepare(document_version_id)

    @staticmethod
    async def _locked_cleanup(
        session: AsyncSession,
        document_version_id: str,
    ) -> RowMapping | None:
        result = await session.execute(
            select(PROJECTION_CLEANUP_JOBS)
            .where(PROJECTION_CLEANUP_JOBS.c.document_version_id == document_version_id)
            .with_for_update()
        )
        return result.mappings().one_or_none()


def _active_source_from_row(row: DatabaseRow) -> ActiveSourceDocument:
    descriptor = required_mapping(row, "descriptor")
    raw_title = descriptor.get("title") or descriptor.get("filename")
    title = str(raw_title).strip() if raw_title is not None else ""
    return ActiveSourceDocument(
        source_item_id=required_text(row, "source_item_id"),
        source_scope_id=required_text(row, "source_scope_id"),
        document_id=DocumentId(required_text(row, "document_id")),
        document_version_id=DocumentVersionId(required_text(row, "active_document_version_id")),
        title=title or None,
    )
