"""Structural storage boundaries shared by composition and application services."""

from collections.abc import AsyncIterator
from typing import Protocol

from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    VectorSearchQuery,
    VectorSearchResult,
    VectorStoreCapabilities,
)
from harborrag_core.schemas.object_store import (
    ObjectMetadata,
    ObjectReference,
    ObjectStoreCapabilities,
    PutObjectRequest,
)
from harborrag_core.storage import RepositoryHealth, StorageOperationContext

from .indexing import VectorIndexRepositoryPort


class StorageLifecyclePort(Protocol):
    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def health(self) -> RepositoryHealth: ...


class VectorRepositoryPort(VectorIndexRepositoryPort, StorageLifecyclePort, Protocol):
    @property
    def capabilities(self) -> VectorStoreCapabilities: ...

    async def index_exists(self, name: str, *, context: StorageOperationContext) -> bool: ...

    async def delete_index(self, name: str, *, context: StorageOperationContext) -> None: ...

    async def search(
        self, query: VectorSearchQuery, *, context: StorageOperationContext
    ) -> list[VectorSearchResult]: ...

    async def sparse_search(
        self, query: SparseSearchQuery, *, context: StorageOperationContext
    ) -> list[VectorSearchResult]: ...

    async def hybrid_search(
        self, query: HybridSearchQuery, *, context: StorageOperationContext
    ) -> list[VectorSearchResult]: ...


class ObjectStorePort(StorageLifecyclePort, Protocol):
    @property
    def capabilities(self) -> ObjectStoreCapabilities: ...

    async def ensure_buckets(self, buckets: tuple[str, ...]) -> None:
        """Provision logical namespaces; implicit-namespace backends may do nothing."""
        ...

    async def put(
        self, request: PutObjectRequest, *, context: StorageOperationContext
    ) -> ObjectReference: ...

    async def get_bytes(
        self,
        bucket: str,
        key: str,
        *,
        byte_range: tuple[int, int] | None,
        context: StorageOperationContext,
    ) -> bytes: ...

    def iter_bytes(
        self,
        bucket: str,
        key: str,
        *,
        chunk_size: int,
        context: StorageOperationContext,
    ) -> AsyncIterator[bytes]: ...

    async def head(
        self, bucket: str, key: str, *, context: StorageOperationContext
    ) -> ObjectMetadata: ...

    async def exists(self, bucket: str, key: str, *, context: StorageOperationContext) -> bool: ...

    async def delete(self, bucket: str, key: str, *, context: StorageOperationContext) -> bool: ...

    async def list(
        self, bucket: str, prefix: str, *, limit: int, context: StorageOperationContext
    ) -> list[ObjectMetadata]: ...

    async def presign_download(
        self, bucket: str, key: str, *, expires_seconds: int, context: StorageOperationContext
    ) -> str: ...
