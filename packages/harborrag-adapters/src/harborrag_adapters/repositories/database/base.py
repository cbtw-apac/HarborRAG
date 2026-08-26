from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Self

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.chunking import ChunkRecord
from harborrag_core.schemas.documents import DocumentRecord
from harborrag_core.schemas.ids import DocumentId
from harborrag_core.storage import StorageOperationContext


class DocumentRepository(ABC):
    """Persists the canonical document aggregate inside a transaction."""

    @abstractmethod
    async def create(
        self,
        record: DocumentRecord,
        *,
        context: StorageOperationContext,
    ) -> DocumentRecord: ...

    @abstractmethod
    async def get(
        self,
        document_id: DocumentId,
        *,
        context: StorageOperationContext,
    ) -> DocumentRecord | None: ...

    @abstractmethod
    async def save(
        self,
        record: DocumentRecord,
        *,
        expected_version: int,
        context: StorageOperationContext,
    ) -> DocumentRecord: ...

    @abstractmethod
    async def list_ready_without_vectors(
        self,
        collection: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[DocumentRecord]: ...


class ChunkRepository(ABC):
    """Persists chunks and their document lineage inside a transaction."""

    @abstractmethod
    async def bulk_upsert(
        self,
        records: Sequence[ChunkRecord],
        *,
        context: StorageOperationContext,
    ) -> None: ...

    @abstractmethod
    async def get_many(
        self,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[ChunkRecord]: ...

    @abstractmethod
    async def list_by_document(
        self,
        document_id: DocumentId,
        *,
        context: StorageOperationContext,
    ) -> list[ChunkRecord]: ...


class OutboxRepository(ABC):
    """Appends cross-store events to the current database transaction."""

    @abstractmethod
    async def add(
        self,
        event_type: str,
        payload: dict[str, object],
        *,
        context: StorageOperationContext,
    ) -> None: ...


class HarborUnitOfWork(ABC):
    """Defines one explicit transaction over canonical database repositories."""

    documents: DocumentRepository
    chunks: ChunkRepository
    outbox: OutboxRepository

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...

    @abstractmethod
    async def commit(self) -> None: ...

    @abstractmethod
    async def rollback(self) -> None: ...


class HarborUnitOfWorkFactory(ABC):
    """Creates isolated inactive unit-of-work instances."""

    @abstractmethod
    def __call__(self) -> HarborUnitOfWork: ...


class HarborDatabaseBackend(RepositoryLifecycle):
    """Owns durable HarborRAG domain persistence for one provider."""

    unit_of_work_factory: HarborUnitOfWorkFactory | None = None
