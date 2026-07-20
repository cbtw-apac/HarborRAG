from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence

from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    VectorCollectionSpec,
    VectorPoint,
    VectorScanPage,
    VectorSearchQuery,
    VectorSearchResult,
    VectorStoreCapabilities,
)

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle


class HarborVectorRepository(RepositoryLifecycle):
    """Defines provider-independent vector persistence and retrieval behavior."""

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Return features supported by the selected vector backend."""

    @abstractmethod
    async def ensure_collection(
        self,
        spec: VectorCollectionSpec,
        *,
        context: StorageOperationContext,
    ) -> None:
        """Create or validate a collection through an idempotent operation."""

    @abstractmethod
    async def collection_exists(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Return whether a tenant-scoped collection exists."""

    @abstractmethod
    async def delete_collection(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete a tenant-scoped collection."""

    @abstractmethod
    async def upsert(
        self,
        collection: str,
        points: Sequence[VectorPoint],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Insert or replace already-generated vector points."""

    @abstractmethod
    async def get(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorPoint]:
        """Load vector points by stable HarborRAG identifiers."""

    @abstractmethod
    async def delete(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete vector points by stable HarborRAG identifiers."""

    @abstractmethod
    async def scan(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        context: StorageOperationContext,
    ) -> VectorScanPage:
        """Scan vector points with a provider-independent cursor."""

    @abstractmethod
    async def search(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        """Execute normalized dense similarity search."""

    @abstractmethod
    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        """Execute hybrid retrieval when the backend supports it."""
