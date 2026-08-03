from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from copy import deepcopy
from datetime import UTC, datetime

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_adapters.repositories.object_store.body import read_body
from harborrag_adapters.repositories.object_store.keys import validate_object_key
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)
from harborrag_core.schemas.object_store import (
    ObjectMetadata,
    ObjectReference,
    ObjectStoreCapabilities,
    PutObjectRequest,
)
from harborrag_core.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)


class MemoryObjectStore(HarborObjectStore):
    """Stores tenant-authorized objects in process memory for tests and demos."""

    def __init__(
        self,
        *,
        instance_name: str = "default",
        telemetry: StorageTelemetryHook | None = None,
    ) -> None:
        self._instance_name = instance_name
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.OBJECT_STORE,
            backend="memory",
        )
        self._objects: dict[tuple[str, str, str], bytes] = {}
        self._metadata: dict[tuple[str, str, str], ObjectMetadata] = {}
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def capabilities(self) -> ObjectStoreCapabilities:
        return ObjectStoreCapabilities(
            conditional_writes=True,
            range_downloads=True,
            streaming_upload=True,
            streaming_download=True,
        )

    async def connect(self) -> None:
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def health(self) -> RepositoryHealth:
        return RepositoryHealth(
            family=StorageFamily.OBJECT_STORE,
            backend="memory",
            instance_name=self._instance_name,
            status=HealthStatus.HEALTHY if self._connected else HealthStatus.UNKNOWN,
            details={"objects": len(self._objects)},
        )

    @traced_repository_operation("put")
    async def put(
        self,
        request: PutObjectRequest,
        *,
        context: StorageOperationContext,
    ) -> ObjectReference:
        validate_object_key(request.bucket, request.key)
        data = await read_body(request.body)
        digest = hashlib.sha256(data).hexdigest()
        if request.checksum_sha256 and digest != request.checksum_sha256:
            raise HarborStorageValidationError(
                "object checksum does not match request checksum",
                context=self._error_context("put", request.bucket, request.key, context),
            )
        identity = (str(context.tenant_id), request.bucket, request.key)
        async with self._lock:
            existing_metadata = self._metadata.get(identity)
            if existing_metadata is not None:
                if request.if_none_match:
                    raise HarborStorageAlreadyExistsError(
                        f"object {request.bucket}/{request.key} already exists",
                        context=self._error_context("put", request.bucket, request.key, context),
                    )
                if existing_metadata.metadata.get("tenant_id") != str(context.tenant_id):
                    raise HarborStorageAlreadyExistsError(
                        f"object {request.bucket}/{request.key} already exists",
                        context=self._error_context("put", request.bucket, request.key, context),
                    )
            reference = ObjectReference(
                bucket=request.bucket,
                key=request.key,
                uri=f"memory://{request.bucket}/{request.key}",
                etag=digest,
                checksum_sha256=digest,
                size_bytes=len(data),
                content_type=request.content_type,
            )
            self._objects[identity] = data
            self._metadata[identity] = ObjectMetadata(
                reference=reference,
                metadata={**request.metadata, "tenant_id": str(context.tenant_id)},
                last_modified=datetime.now(UTC),
            )
            return reference

    @traced_repository_operation("get_bytes")
    async def get_bytes(
        self,
        bucket: str,
        key: str,
        *,
        byte_range: tuple[int, int] | None,
        context: StorageOperationContext,
    ) -> bytes:
        data = self._require(bucket, key, context)
        if byte_range is None:
            return bytes(data)
        start, end = byte_range
        if start < 0 or end < start:
            raise ValueError("invalid byte range")
        return bytes(data[start : end + 1])

    async def iter_bytes(
        self,
        bucket: str,
        key: str,
        *,
        chunk_size: int,
        context: StorageOperationContext,
    ) -> AsyncIterator[bytes]:
        async with self._telemetry.operation("iter_bytes", context):
            data = self._require(bucket, key, context)
            for offset in range(0, len(data), chunk_size):
                yield bytes(data[offset : offset + chunk_size])

    @traced_repository_operation("head")
    async def head(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> ObjectMetadata:
        self._require(bucket, key, context)
        return deepcopy(self._metadata[self._identity(bucket, key, context)])

    @traced_repository_operation("exists")
    async def exists(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        return self._identity(bucket, key, context) in self._metadata

    @traced_repository_operation("delete")
    async def delete(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        async with self._lock:
            identity = self._identity(bucket, key, context)
            existed = identity in self._metadata
            self._objects.pop(identity, None)
            self._metadata.pop(identity, None)
            return existed

    @traced_repository_operation("list")
    async def list(
        self,
        bucket: str,
        prefix: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[ObjectMetadata]:
        values = [
            deepcopy(metadata)
            for (tenant_id, item_bucket, key), metadata in sorted(self._metadata.items())
            if tenant_id == str(context.tenant_id)
            and item_bucket == bucket
            and key.startswith(prefix)
        ]
        return values[:limit]

    @traced_repository_operation("presign_download")
    async def presign_download(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int,
        context: StorageOperationContext,
    ) -> str:
        del bucket, key, expires_seconds, context
        raise NotImplementedError("in-memory object storage has no presigned URL surface")

    def _require(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> bytes:
        identity = self._identity(bucket, key, context)
        data = self._objects.get(identity)
        metadata = self._metadata.get(identity)
        if (
            data is None
            or metadata is None
            or metadata.metadata.get("tenant_id") != str(context.tenant_id)
        ):
            raise HarborStorageNotFoundError(
                f"object {bucket}/{key} does not exist",
                context=self._error_context("get", bucket, key, context),
            )
        return data

    @staticmethod
    def _identity(
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> tuple[str, str, str]:
        return str(context.tenant_id), bucket, key

    def _error_context(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.OBJECT_STORE,
            backend="memory",
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=f"{bucket}/{key}",
        )
