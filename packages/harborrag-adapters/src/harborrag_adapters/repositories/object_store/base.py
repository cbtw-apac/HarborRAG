from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator

from harborrag_adapters.repositories.lifecycle import RepositoryLifecycle
from harborrag_core.schemas.object_store import (
    ObjectMetadata,
    ObjectReference,
    ObjectStoreCapabilities,
    PutObjectRequest,
)
from harborrag_core.storage import StorageOperationContext


class HarborObjectStore(RepositoryLifecycle):
    """Defines tenant-safe binary object persistence and streaming operations."""

    @property
    @abstractmethod
    def capabilities(self) -> ObjectStoreCapabilities:
        """Return features supported by the selected object backend."""

    @abstractmethod
    async def put(
        self,
        request: PutObjectRequest,
        *,
        context: StorageOperationContext,
    ) -> ObjectReference:
        """Store one object and return its normalized reference."""

    @abstractmethod
    async def get_bytes(
        self,
        bucket: str,
        key: str,
        *,
        byte_range: tuple[int, int] | None,
        context: StorageOperationContext,
    ) -> bytes:
        """Read an object or an inclusive byte range."""

    @abstractmethod
    def iter_bytes(
        self,
        bucket: str,
        key: str,
        *,
        chunk_size: int,
        context: StorageOperationContext,
    ) -> AsyncIterator[bytes]:
        """Stream an object without buffering its entire payload."""

    @abstractmethod
    async def head(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> ObjectMetadata:
        """Return normalized object metadata."""

    @abstractmethod
    async def exists(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Return whether an object exists for the tenant."""

    @abstractmethod
    async def delete(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        """Delete one tenant-scoped object."""

    @abstractmethod
    async def list(
        self,
        bucket: str,
        prefix: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[ObjectMetadata]:
        """List tenant-scoped objects under a prefix."""

    @abstractmethod
    async def presign_download(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int,
        context: StorageOperationContext,
    ) -> str:
        """Create a time-limited download URL when supported."""
