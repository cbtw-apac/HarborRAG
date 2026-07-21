from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from harborrag_core.schemas.documents import ChunkRecord, DocumentRecord, DocumentStatus
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text
from sqlalchemy import delete as sa_delete
from sqlalchemy import insert as sa_insert
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from harborrag_adapters.repositories.database.base import (
    ChunkRepository,
    DocumentRepository,
    HarborDatabaseBackend,
    HarborUnitOfWork,
    HarborUnitOfWorkFactory,
    OutboxRepository,
)
from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageCheckpointConflictError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.shared.sqlalchemy import SQLAlchemyDBClient, UTCDateTime
from harborrag_adapters.repositories.shared.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)

_METADATA = MetaData()

_DOCUMENTS = Table(
    "harbor_documents",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("data_source_id", String(64), nullable=True),
    Column("current_version_id", String(64), nullable=False),
    Column("external_id", Text, nullable=True),
    Column("title", Text, nullable=True),
    Column("media_type", String(255), nullable=True),
    Column("content_hash", String(128), nullable=False),
    Column("object_uri", Text, nullable=True),
    Column("status", String(32), nullable=False),
    Column("metadata", JSON, nullable=False, default=dict),
    Column("version", Integer, nullable=False, default=1),
    Column("created_at", UTCDateTime(), nullable=False),
    Column("updated_at", UTCDateTime(), nullable=False),
    Column("deleted_at", UTCDateTime(), nullable=True),
)

_CHUNKS = Table(
    "harbor_chunks",
    _METADATA,
    Column("tenant_id", String(64), primary_key=True),
    Column("id", String(64), primary_key=True),
    Column("document_id", String(64), nullable=False, index=True),
    Column("document_version_id", String(64), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("content_hash", String(128), nullable=False),
    Column("token_count", Integer, nullable=True),
    Column("metadata", JSON, nullable=False, default=dict),
    Column("created_at", UTCDateTime(), nullable=False),
)

_OUTBOX = Table(
    "harbor_outbox",
    _METADATA,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("tenant_id", String(64), nullable=False, index=True),
    Column("event_type", String(255), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", UTCDateTime(), nullable=False),
)

# Documents track per-collection embedding status in metadata["vector_collections"]
# (a list of collection names already embedded) since the canonical schema has no
# dedicated vector-status table. list_ready_without_vectors relies on this convention.
_VECTOR_COLLECTIONS_KEY = "vector_collections"


def _document_from_row(row: Any) -> DocumentRecord:
    return DocumentRecord.model_validate(
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "data_source_id": row["data_source_id"],
            "current_version_id": row["current_version_id"],
            "external_id": row["external_id"],
            "title": row["title"],
            "media_type": row["media_type"],
            "content_hash": row["content_hash"],
            "object_uri": row["object_uri"],
            "status": row["status"],
            "metadata": dict(row["metadata"] or {}),
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "deleted_at": row["deleted_at"],
        }
    )


def _chunk_from_row(row: Any) -> ChunkRecord:
    return ChunkRecord.model_validate(
        {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "document_id": row["document_id"],
            "document_version_id": row["document_version_id"],
            "chunk_index": row["chunk_index"],
            "content": row["content"],
            "content_hash": row["content_hash"],
            "token_count": row["token_count"],
            "metadata": dict(row["metadata"] or {}),
            "created_at": row["created_at"],
        }
    )


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
                sa_insert(_DOCUMENTS).values(
                    tenant_id=str(record.tenant_id),
                    id=str(record.id),
                    data_source_id=str(record.data_source_id) if record.data_source_id else None,
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
                    sa_select(_DOCUMENTS).where(
                        _DOCUMENTS.c.tenant_id == str(context.tenant_id),
                        _DOCUMENTS.c.id == str(document_id),
                    )
                )
            )
            .mappings()
            .first()
        )
        return _document_from_row(row) if row else None

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
            sa_update(_DOCUMENTS)
            .where(
                _DOCUMENTS.c.tenant_id == str(context.tenant_id),
                _DOCUMENTS.c.id == str(record.id),
                _DOCUMENTS.c.version == expected_version,
            )
            .values(
                data_source_id=str(record.data_source_id) if record.data_source_id else None,
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
            statement = sa_select(_DOCUMENTS).where(
                _DOCUMENTS.c.tenant_id == str(context.tenant_id),
                _DOCUMENTS.c.status == DocumentStatus.READY.value,
            )
            if last_updated is not None and last_id is not None:
                statement = statement.where(
                    sa_or(
                        _DOCUMENTS.c.updated_at > last_updated,
                        ((_DOCUMENTS.c.updated_at == last_updated) & (_DOCUMENTS.c.id > last_id)),
                    )
                )
            rows = (
                (
                    await self._session.execute(
                        statement.order_by(_DOCUMENTS.c.updated_at, _DOCUMENTS.c.id).limit(
                            batch_size
                        )
                    )
                )
                .mappings()
                .all()
            )
            if not rows:
                break
            for row in rows:
                record = _document_from_row(row)
                if collection not in record.metadata.get(_VECTOR_COLLECTIONS_KEY, []):
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
            metadata={"expected_version": expected_version} if expected_version is not None else {},
        )


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
                    resource_name=str(record.id),
                ),
            )
        ids = [str(record.id) for record in records]
        await self._session.execute(
            sa_delete(_CHUNKS).where(
                _CHUNKS.c.tenant_id == str(context.tenant_id),
                _CHUNKS.c.id.in_(ids),
            )
        )
        await self._session.execute(
            sa_insert(_CHUNKS),
            [
                {
                    "tenant_id": str(context.tenant_id),
                    "id": str(record.id),
                    "document_id": str(record.document_id),
                    "document_version_id": str(record.document_version_id),
                    "chunk_index": record.chunk_index,
                    "content": record.content,
                    "content_hash": record.content_hash,
                    "token_count": record.token_count,
                    "metadata": record.metadata,
                    "created_at": record.created_at,
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
                    sa_select(_CHUNKS).where(
                        _CHUNKS.c.tenant_id == str(context.tenant_id),
                        _CHUNKS.c.id.in_(list(ids)),
                    )
                )
            )
            .mappings()
            .all()
        )
        return [_chunk_from_row(row) for row in rows]

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
                    sa_select(_CHUNKS)
                    .where(
                        _CHUNKS.c.tenant_id == str(context.tenant_id),
                        _CHUNKS.c.document_id == str(document_id),
                    )
                    .order_by(_CHUNKS.c.chunk_index)
                )
            )
            .mappings()
            .all()
        )
        return [_chunk_from_row(row) for row in rows]


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
            sa_insert(_OUTBOX).values(
                tenant_id=str(context.tenant_id),
                event_type=event_type,
                payload=payload,
                created_at=datetime.now(UTC),
            )
        )


class SQLAlchemyUnitOfWork(HarborUnitOfWork):
    """Owns one SQLAlchemy session and transaction shared by cohesive repositories."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        instance_name: str,
        telemetry: RepositoryTelemetry,
    ) -> None:
        self._client = client
        self._instance_name = instance_name
        self._telemetry = telemetry
        self._session: Any = None
        self._committed = False

    async def __aenter__(self) -> SQLAlchemyUnitOfWork:
        self._session = self._client.sessions()
        await self._session.begin()
        self.documents = SQLDocumentRepository(
            self._session,
            self._client.backend,
            self._instance_name,
            self._telemetry,
        )
        self.chunks = SQLChunkRepository(
            self._session,
            self._client.backend,
            self._instance_name,
            self._telemetry,
        )
        self.outbox = SQLOutboxRepository(self._session, self._telemetry)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc, traceback
        try:
            if not self._committed:
                await self._session.rollback()
        finally:
            await self._session.close()

    async def commit(self) -> None:
        await self._session.commit()
        self._committed = True

    async def rollback(self) -> None:
        await self._session.rollback()
        self._committed = True


class SQLAlchemyUnitOfWorkFactory(HarborUnitOfWorkFactory):
    """Creates isolated SQLAlchemy units of work over one shared database client."""

    def __init__(
        self,
        client: SQLAlchemyDBClient,
        instance_name: str,
        telemetry: RepositoryTelemetry,
    ) -> None:
        self._client = client
        self._instance_name = instance_name
        self._telemetry = telemetry

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return SQLAlchemyUnitOfWork(self._client, self._instance_name, self._telemetry)


class SQLAlchemyDatabaseBackend(HarborDatabaseBackend):
    """Composes canonical document, chunk, and outbox persistence over SQLAlchemy."""

    def __init__(
        self,
        *,
        client: SQLAlchemyDBClient,
        instance_name: str,
        create_schema: bool,
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self._database = client
        self._instance_name = instance_name
        self._create_schema = create_schema
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.DATABASE,
            backend=client.backend,
        )
        self.unit_of_work_factory = SQLAlchemyUnitOfWorkFactory(
            client,
            instance_name,
            self._telemetry,
        )

    async def connect(self) -> None:
        await self._database.connect()
        if self._create_schema:
            await self._database.create_schema(_METADATA)

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.DATABASE,
            backend=self._database.backend,
            instance_name=self._instance_name,
            status=status,
            details=details,
        )
