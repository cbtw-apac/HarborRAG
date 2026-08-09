from __future__ import annotations

from abc import abstractmethod
from collections.abc import Sequence

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    VectorFilter,
    VectorIndexRecord,
    VectorIndexScanPage,
    VectorIndexSpec,
    VectorSearchQuery,
    VectorSearchResult,
    VectorStoreCapabilities,
)
from harborrag_core.storage import StorageOperationContext


class HarborVectorRepository(RepositoryLifecycle):
    """Defines provider-independent vector persistence and retrieval behavior."""

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Return features supported by the selected vector backend."""

    @abstractmethod
    async def ensure_index(
        self,
        spec: VectorIndexSpec,
        *,
        context: StorageOperationContext,
    ) -> None:
        """Create or validate a collection through an idempotent operation."""

    @abstractmethod
    async def index_exists(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Return whether a tenant-scoped collection exists."""

    @abstractmethod
    async def delete_index(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete a tenant-scoped collection."""

    @abstractmethod
    async def upsert_records(
        self,
        index_name: str,
        records: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Insert or replace already-generated vector points."""

    @abstractmethod
    async def get_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorIndexRecord]:
        """Load vector points by stable HarborRAG identifiers."""

    @abstractmethod
    async def delete_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> None:
        """Delete vector points by stable HarborRAG identifiers."""

    @abstractmethod
    async def scan_records(
        self,
        index_name: str,
        *,
        limit: int,
        cursor: str | None,
        filters: VectorFilter | None = None,
        context: StorageOperationContext,
    ) -> VectorIndexScanPage:
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
    async def sparse_search(
        self,
        query: SparseSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        """Execute normalized sparse similarity search."""

    @abstractmethod
    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        """Execute hybrid retrieval when the backend supports it."""
