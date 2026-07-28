from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select

from harborrag_adapters.repositories.database.base import (
    ChunkRepository,
    OutboxRepository,
)
from harborrag_adapters.repositories.errors import StorageErrorContext
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    traced_repository_operation,
)
from harborrag_core.schemas.documents import ChunkRecord
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext

from .schemas import CHUNKS, OUTBOX, chunk_from_row, chunk_metadata


class SQLChunkRepository(ChunkRepository):
    """Persists chunks and document lineage through one shared SQL session."""

    def __init__(
        self,
        session: Any,
        backend: str,
        instance_name: str,
        telemetry: RepositoryTelemetry,
    ) -> None:
        self._session = session
        self._backend = backend
        self._instance_name = instance_name
        self._telemetry = telemetry

    @traced_repository_operation("chunk_bulk_upsert")
    async def bulk_upsert(
        self,
        records: Sequence[ChunkRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        if not records:
            return
        for record in records:
            ensure_tenant(
                record.tenant_id,
                context,
                error_context=StorageErrorContext(
                    family=StorageFamily.DATABASE,
                    backend=self._backend,
                    instance_name=self._instance_name,
                    operation="chunk_bulk_upsert",
                    tenant_id=str(context.tenant_id),
                    resource_name=str(record.chunk_revision_id),
                ),
            )
        ids = [str(record.chunk_revision_id) for record in records]
        await self._session.execute(
            sa_delete(CHUNKS).where(
                CHUNKS.c.tenant_id == str(context.tenant_id),
                CHUNKS.c.id.in_(ids),
            )
        )
        await self._session.execute(
            sa_insert(CHUNKS),
            [
                {
                    "tenant_id": str(context.tenant_id),
                    "id": str(record.chunk_revision_id),
                    "document_id": str(record.document_id),
                    "document_version_id": str(record.document_version_id),
                    "chunk_index": record.ordinal,
                    "content": record.content,
                    "content_hash": record.content_hash,
                    "token_count": record.token_count,
                    "metadata": chunk_metadata(record),
                    "created_at": record.created_at or datetime.now(UTC),
                }
                for record in records
            ],
        )

    @traced_repository_operation("chunk_get_many")
    async def get_many(
        self,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[ChunkRecord]:
        if not ids:
            return []
        rows = (
            (
                await self._session.execute(
                    sa_select(CHUNKS).where(
                        CHUNKS.c.tenant_id == str(context.tenant_id),
                        CHUNKS.c.id.in_(list(ids)),
                    )
                )
            )
            .mappings()
            .all()
        )
        return [chunk_from_row(row) for row in rows]

    @traced_repository_operation("chunk_list_by_document")
    async def list_by_document(
        self,
        document_id: DocumentId,
        *,
        context: StorageOperationContext,
    ) -> list[ChunkRecord]:
        rows = (
            (
                await self._session.execute(
                    sa_select(CHUNKS)
                    .where(
                        CHUNKS.c.tenant_id == str(context.tenant_id),
                        CHUNKS.c.document_id == str(document_id),
                    )
                    .order_by(CHUNKS.c.chunk_index)
                )
            )
            .mappings()
            .all()
        )
        return [chunk_from_row(row) for row in rows]


class SQLOutboxRepository(OutboxRepository):
    """Appends cross-store events to the current shared SQL session."""

    def __init__(self, session: Any, telemetry: RepositoryTelemetry) -> None:
        self._session = session
        self._telemetry = telemetry

    @traced_repository_operation("outbox_add")
    async def add(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        context: StorageOperationContext,
    ) -> None:
        await self._session.execute(
            sa_insert(OUTBOX).values(
                tenant_id=str(context.tenant_id),
                event_type=event_type,
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )
