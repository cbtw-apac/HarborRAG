from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from harborrag_core.schemas.object_store import (
    ObjectMetadata,
    ObjectReference,
    ObjectStoreCapabilities,
    PutObjectRequest,
)
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_adapters.repositories.object_store.body import iter_body
from harborrag_adapters.repositories.object_store.filesystem.config import (
    FilesystemObjectStoreConfig,
)
from harborrag_adapters.repositories.object_store.keys import (
    tenant_object_prefix,
    validate_object_key,
)
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)

_BUCKET = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_META_SUFFIX = ".harbor-meta.json"
_LOCK_SUFFIX = ".harbor-lock"


class FilesystemObjectStore(HarborObjectStore):
    """Stores objects beneath a trusted filesystem root with traversal protection."""

    def __init__(
        self,
        config: FilesystemObjectStoreConfig | None = None,
        telemetry: StorageTelemetryHook | None = None,
        *,
        root: Path | None = None,
        instance_name: str = "default",
    ) -> None:
        if config is not None:
            root = config.root
            instance_name = config.instance_name
        if root is None:
            raise ValueError("filesystem object-store root is required")
        self._root = root.expanduser().resolve()
        self._instance_name = instance_name
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.OBJECT_STORE,
            backend="filesystem",
        )
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
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        self._connected = True

    async def close(self) -> None:
        self._connected = False

    async def health(self) -> RepositoryHealth:
        writable = self._root.exists() and os.access(self._root, os.W_OK)
        return RepositoryHealth(
            family=StorageFamily.OBJECT_STORE,
            backend="filesystem",
            instance_name=self._instance_name,
            status=(
                HealthStatus.HEALTHY if self._connected and writable else HealthStatus.UNHEALTHY
            ),
            details={"root": str(self._root), "writable": writable},
        )

    @traced_repository_operation("put")
    async def put(
        self,
        request: PutObjectRequest,
        *,
        context: StorageOperationContext,
    ) -> ObjectReference:
        target = self._safe_path(request.bucket, request.key, context)
        try:
            await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        except OSError as exc:
            raise self._io_error("put", request.bucket, request.key, context, exc) from exc
        lock_path = target.with_name(f".{target.name}{_LOCK_SUFFIX}")
        lock_fd = await self._acquire_lock(lock_path, request.bucket, request.key, context)
        try:
            return await self._put_locked(request, target, context)
        finally:
            await self._release_lock(lock_fd, lock_path)

    async def _put_locked(
        self,
        request: PutObjectRequest,
        target: Path,
        context: StorageOperationContext,
    ) -> ObjectReference:
        existing_meta_path = self._meta_path(target)
        try:
            if await asyncio.to_thread(existing_meta_path.is_file):
                if request.if_none_match:
                    raise self._already_exists(request, context)
                existing = ObjectMetadata.model_validate_json(
                    await asyncio.to_thread(existing_meta_path.read_text, "utf-8")
                )
                if existing.metadata.get("tenant_id") != str(context.tenant_id):
                    raise self._already_exists(request, context)
        except HarborStorageError:
            raise
        except (OSError, ValueError) as exc:
            raise self._io_error("put", request.bucket, request.key, context, exc) from exc
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        meta_temporary = target.with_name(f".{target.name}.{uuid4().hex}.meta.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            file = await asyncio.to_thread(temporary.open, "wb")
            try:
                async for chunk in iter_body(request.body):
                    digest.update(chunk)
                    size += len(chunk)
                    await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(file.flush)
                await asyncio.to_thread(os.fsync, file.fileno())
            finally:
                await asyncio.to_thread(file.close)
            checksum = digest.hexdigest()
            if request.checksum_sha256 and checksum != request.checksum_sha256:
                raise HarborStorageValidationError(
                    "object checksum does not match request checksum",
                    context=self._error_context("put", request.bucket, request.key, context),
                )
            reference = ObjectReference(
                bucket=request.bucket,
                key=request.key,
                uri=target.as_uri(),
                etag=checksum,
                checksum_sha256=checksum,
                size_bytes=size,
                content_type=request.content_type,
            )
            metadata = ObjectMetadata(
                reference=reference,
                metadata={**request.metadata, "tenant_id": str(context.tenant_id)},
                last_modified=datetime.now(UTC),
            )
            await asyncio.to_thread(
                meta_temporary.write_text,
                metadata.model_dump_json(indent=2),
                "utf-8",
            )
            await asyncio.to_thread(os.replace, temporary, target)
            await asyncio.to_thread(os.replace, meta_temporary, existing_meta_path)
            return reference
        except HarborStorageError:
            raise
        except OSError as exc:
            raise self._io_error("put", request.bucket, request.key, context, exc) from exc
        finally:
            await self._cleanup_path(temporary)
            await self._cleanup_path(meta_temporary)

    @traced_repository_operation("get_bytes")
    async def get_bytes(
        self,
        bucket: str,
        key: str,
        *,
        byte_range: tuple[int, int] | None,
        context: StorageOperationContext,
    ) -> bytes:
        target = await self._authorized_path(bucket, key, context)
        try:
            if byte_range is None:
                return await asyncio.to_thread(target.read_bytes)
            start, end = byte_range
            if start < 0 or end < start:
                raise HarborStorageValidationError(
                    "invalid byte range",
                    context=self._error_context("get", bucket, key, context),
                )
            file = await asyncio.to_thread(target.open, "rb")
            try:
                await asyncio.to_thread(file.seek, start)
                return await asyncio.to_thread(file.read, end - start + 1)
            finally:
                await asyncio.to_thread(file.close)
        except HarborStorageError:
            raise
        except FileNotFoundError as exc:
            raise self._not_found(bucket, key, context) from exc
        except OSError as exc:
            raise self._io_error("get", bucket, key, context, exc) from exc

    async def iter_bytes(
        self,
        bucket: str,
        key: str,
        *,
        chunk_size: int,
        context: StorageOperationContext,
    ) -> AsyncIterator[bytes]:
        async with self._telemetry.operation("iter_bytes", context):
            target = await self._authorized_path(bucket, key, context)
            try:
                file = await asyncio.to_thread(target.open, "rb")
                try:
                    while chunk := await asyncio.to_thread(file.read, chunk_size):
                        yield chunk
                finally:
                    await asyncio.to_thread(file.close)
            except FileNotFoundError as exc:
                raise self._not_found(bucket, key, context) from exc
            except OSError as exc:
                raise self._io_error("iter", bucket, key, context, exc) from exc

    @traced_repository_operation("head")
    async def head(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> ObjectMetadata:
        target = await self._authorized_path(bucket, key, context)
        try:
            raw = await asyncio.to_thread(self._meta_path(target).read_text, "utf-8")
            return ObjectMetadata.model_validate_json(raw)
        except FileNotFoundError as exc:
            raise self._not_found(bucket, key, context) from exc
        except (OSError, ValueError) as exc:
            raise self._io_error("head", bucket, key, context, exc) from exc

    @traced_repository_operation("exists")
    async def exists(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        try:
            await self._authorized_path(bucket, key, context)
            return True
        except HarborStorageNotFoundError:
            return False

    @traced_repository_operation("delete")
    async def delete(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        target = self._safe_path(bucket, key, context)
        if not await asyncio.to_thread(target.parent.exists):
            return False
        lock_path = target.with_name(f".{target.name}{_LOCK_SUFFIX}")
        lock_fd = await self._acquire_lock(lock_path, bucket, key, context)
        try:
            try:
                target = await self._authorized_path(bucket, key, context)
                await asyncio.to_thread(target.unlink)
                await asyncio.to_thread(self._meta_path(target).unlink, missing_ok=True)
            except (FileNotFoundError, HarborStorageNotFoundError):
                return False
            except OSError as exc:
                raise self._io_error("delete", bucket, key, context, exc) from exc
            return True
        finally:
            await self._release_lock(lock_fd, lock_path)

    @traced_repository_operation("list")
    async def list(
        self,
        bucket: str,
        prefix: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[ObjectMetadata]:
        bucket_root = self._tenant_root(bucket, context)
        try:
            if not bucket_root.exists():
                return []
            output: list[ObjectMetadata] = []
            for meta_path in sorted(bucket_root.rglob(f"*{_META_SUFFIX}")):
                target = Path(str(meta_path)[: -len(_META_SUFFIX)])
                key = target.relative_to(bucket_root).as_posix()
                if not key.startswith(prefix):
                    continue
                metadata = ObjectMetadata.model_validate_json(
                    await asyncio.to_thread(meta_path.read_text, "utf-8")
                )
                if metadata.metadata.get("tenant_id") == str(context.tenant_id):
                    output.append(metadata)
                    if len(output) >= limit:
                        break
            return output
        except (OSError, ValueError) as exc:
            raise self._io_error("list", bucket, prefix, context, exc) from exc

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
        raise NotImplementedError("filesystem object storage has no presigned URL surface")

    async def _authorized_path(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> Path:
        target = self._safe_path(bucket, key, context)
        meta_path = self._meta_path(target)
        if not target.is_file() or not meta_path.is_file():
            raise self._not_found(bucket, key, context)
        try:
            metadata = ObjectMetadata.model_validate_json(
                await asyncio.to_thread(meta_path.read_text, "utf-8")
            )
        except (OSError, ValueError) as exc:
            raise self._not_found(bucket, key, context) from exc
        if metadata.metadata.get("tenant_id") != str(context.tenant_id):
            raise self._not_found(bucket, key, context)
        return target

    def _safe_path(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> Path:
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("invalid filesystem bucket name")
        validate_object_key(bucket, key)
        normalized = key.replace("\\", "/")
        parts = normalized.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid object key")
        tenant_root = self._tenant_root(bucket, context)
        target = (tenant_root / Path(*parts)).resolve()
        if not target.is_relative_to(tenant_root):
            raise ValueError("object key escapes configured filesystem root")
        return target

    def _tenant_root(self, bucket: str, context: StorageOperationContext) -> Path:
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("invalid filesystem bucket name")
        namespace = Path(*tenant_object_prefix(context.tenant_id).split("/"))
        return (self._root / bucket / namespace).resolve()

    @staticmethod
    def _meta_path(target: Path) -> Path:
        return Path(f"{target}{_META_SUFFIX}")

    def _not_found(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> HarborStorageNotFoundError:
        return HarborStorageNotFoundError(
            f"object {bucket}/{key} does not exist",
            context=self._error_context("get", bucket, key, context),
        )

    async def _acquire_lock(
        self,
        lock_path: Path,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> int:
        deadline = asyncio.get_running_loop().time() + 30.0
        while True:
            try:
                return await asyncio.to_thread(
                    os.open,
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                if asyncio.get_running_loop().time() >= deadline:
                    error_context = self._error_context("put", bucket, key, context)
                    error_context.retryable = True
                    raise HarborStorageError(
                        f"timed out waiting to write object {bucket}/{key}",
                        context=error_context,
                    ) from None
                await asyncio.sleep(0.01)
            except OSError as exc:
                raise self._io_error("put", bucket, key, context, exc) from exc

    @staticmethod
    async def _cleanup_path(path: Path) -> None:
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            pass

    @classmethod
    async def _release_lock(cls, lock_fd: int, lock_path: Path) -> None:
        try:
            await asyncio.to_thread(os.close, lock_fd)
        except OSError:
            pass
        await cls._cleanup_path(lock_path)

    def _already_exists(
        self,
        request: PutObjectRequest,
        context: StorageOperationContext,
    ) -> HarborStorageAlreadyExistsError:
        return HarborStorageAlreadyExistsError(
            f"object {request.bucket}/{request.key} already exists",
            context=self._error_context("put", request.bucket, request.key, context),
        )

    def _io_error(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
        exc: Exception,
    ) -> HarborStorageError:
        return HarborStorageError(
            f"filesystem {operation} failed for object {bucket}/{key}",
            context=self._error_context(operation, bucket, key, context),
            original=exc,
        )

    def _error_context(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.OBJECT_STORE,
            backend="filesystem",
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=f"{bucket}/{key}",
        )
