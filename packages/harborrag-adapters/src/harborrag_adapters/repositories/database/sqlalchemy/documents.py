from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import insert as sa_insert
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.base import DocumentRepository
from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    traced_repository_operation,
)
from harborrag_core.schemas.documents import DocumentRecord, DocumentStatus
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.storage import StorageFamily, StorageOperationContext

from .schemas import DOCUMENTS, VECTOR_COLLECTIONS_KEY, document_from_row


class SQLDocumentRepository(DocumentRepository):
    """Persists the canonical document aggregate through one shared SQL session."""

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

    @traced_repository_operation("document_create")
    async def create(
        self,
        record: DocumentRecord,
        *,
        context: StorageOperationContext,
    ) -> DocumentRecord:
        ensure_tenant(
            record.tenant_id,
            context,
            error_context=self._error_context("create", context, str(record.id)),
        )
        try:
            await self._session.execute(
                sa_insert(DOCUMENTS).values(
                    tenant_id=str(record.tenant_id),
                    id=str(record.id),
                    data_source_id=(str(record.data_source_id) if record.data_source_id else None),
                    current_version_id=str(record.current_version_id),
                    external_id=record.external_id,
                    title=record.title,
                    media_type=record.media_type,
                    content_hash=record.content_hash,
                    object_uri=record.object_uri,
                    status=record.status.value,
                    metadata=record.metadata,
                    version=record.version,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                    deleted_at=record.deleted_at,
                )
            )
        except IntegrityError as exc:
            raise HarborStorageAlreadyExistsError(
                f"document {record.id!r} already exists",
                context=self._error_context("create", context, str(record.id)),
            ) from exc
        return record

    @traced_repository_operation("document_get")
    async def get(
        self,
        document_id: DocumentId,
        *,
        context: StorageOperationContext,
    ) -> DocumentRecord | None:
        row = (
            (
                await self._session.execute(
                    sa_select(DOCUMENTS).where(
                        DOCUMENTS.c.tenant_id == str(context.tenant_id),
                        DOCUMENTS.c.id == str(document_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        return document_from_row(row) if row else None

    @traced_repository_operation("document_save")
    async def save(
        self,
        record: DocumentRecord,
        *,
        expected_version: int,
        context: StorageOperationContext,
    ) -> DocumentRecord:
        ensure_tenant(
            record.tenant_id,
            context,
            error_context=self._error_context("save", context, str(record.id)),
        )
        now = datetime.now(UTC)
        result = await self._session.execute(
            sa_update(DOCUMENTS)
            .where(
                DOCUMENTS.c.tenant_id == str(context.tenant_id),
                DOCUMENTS.c.id == str(record.id),
                DOCUMENTS.c.version == expected_version,
            )
            .values(
                data_source_id=(str(record.data_source_id) if record.data_source_id else None),
                current_version_id=str(record.current_version_id),
                external_id=record.external_id,
                title=record.title,
                media_type=record.media_type,
                content_hash=record.content_hash,
                object_uri=record.object_uri,
                status=record.status.value,
                metadata=record.metadata,
                version=expected_version + 1,
                updated_at=now,
                deleted_at=record.deleted_at,
            )
        )
        if result.rowcount != 1:
            raise self._conflict(context, record.id, expected_version)
        return record.model_copy(update={"version": expected_version + 1, "updated_at": now})

    @traced_repository_operation("document_list_ready_without_vectors")
    async def list_ready_without_vectors(
        self,
        collection: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[DocumentRecord]:
        if limit <= 0:
            return []
        output: list[DocumentRecord] = []
        batch_size = min(1000, max(100, limit * 2))
        last_updated: datetime | None = None
        last_id: str | None = None
        while len(output) < limit:
            statement = sa_select(DOCUMENTS).where(
                DOCUMENTS.c.tenant_id == str(context.tenant_id),
                DOCUMENTS.c.status == DocumentStatus.READY.value,
            )
            if last_updated is not None and last_id is not None:
                statement = statement.where(
                    sa_or(
                        DOCUMENTS.c.updated_at > last_updated,
                        ((DOCUMENTS.c.updated_at == last_updated) & (DOCUMENTS.c.id > last_id)),
                    )
                )
            rows = (
                (
                    await self._session.execute(
                        statement.order_by(
                            DOCUMENTS.c.updated_at,
                            DOCUMENTS.c.id,
                        ).limit(batch_size)
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            for row in rows:
                record = document_from_row(row)
                if collection not in record.metadata.get(VECTOR_COLLECTIONS_KEY, []):
                    output.append(record)
                    if len(output) >= limit:
                        return output
            last_updated = rows[-1]["updated_at"]
            last_id = rows[-1]["id"]
            if len(rows) < batch_size:
                break
        return output

    def _conflict(
        self,
        context: StorageOperationContext,
        document_id: DocumentId,
        expected: int,
    ) -> HarborStorageCheckpointConflictError:
        return HarborStorageCheckpointConflictError(
            f"document {document_id!r} version conflict",
            context=self._error_context("save", context, str(document_id), expected),
        )

    def _error_context(
        self,
        operation: str,
        context: StorageOperationContext,
        resource: str,
        expected_version: int | None = None,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.DATABASE,
            backend=self._backend,
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=resource,
            metadata=(
                {"expected_version": expected_version} if expected_version is not None else {}
            ),
        )
