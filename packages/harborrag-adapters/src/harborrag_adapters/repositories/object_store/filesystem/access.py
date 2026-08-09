from __future__ import annotations

import asyncio
import os
import re
import stat
from pathlib import Path
from typing import BinaryIO

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageError,
    HarborStorageNotFoundError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store.keys import (
    tenant_object_prefix,
    validate_object_key,
)
from harborrag_core.schemas.object_store import ObjectMetadata, PutObjectRequest
from harborrag_core.storage import StorageFamily, StorageOperationContext

_BUCKET = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$")
_META_SUFFIX = ".harbor-meta.json"


class FilesystemAccessMixin:
    """Authorize tenant paths and coordinate filesystem write locks."""

    _root: Path
    _instance_name: str

    def _ensure_private_directory(self, path: Path) -> None:
        """Create and lock down directories beneath the configured root."""

        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        current = path
        while current == self._root or self._root in current.parents:
            os.chmod(current, 0o700)
            if current == self._root:
                break
            current = current.parent

    @staticmethod
    def _open_private_file(path: Path) -> BinaryIO:
        """Create a replacement file with an explicit owner-only mode."""

        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        return os.fdopen(descriptor, "wb")

    @staticmethod
    def _open_regular_file(path: Path) -> BinaryIO:
        """Open one existing regular file without following its final path component."""

        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("filesystem object path is not a regular file")
            return os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def _read_regular_file(cls, path: Path) -> bytes:
        with cls._open_regular_file(path) as file:
            return file.read()

    async def _authorized_path(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> Path:
        target = self._safe_path(bucket, key, context)
        meta_path = self._meta_path(target)
        try:
            metadata = ObjectMetadata.model_validate_json(
                await asyncio.to_thread(self._read_regular_file, meta_path)
            )
            file = await asyncio.to_thread(self._open_regular_file, target)
            await asyncio.to_thread(file.close)
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
        parts = key.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("invalid object key")
        tenant_root = self._tenant_root(bucket, context)
        target = tenant_root / Path(*parts)
        resolved_root = tenant_root.resolve()
        self._reject_symlinks(target, tenant_root)
        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(resolved_root):
            raise ValueError("object key escapes configured filesystem root")
        return target

    def _tenant_root(self, bucket: str, context: StorageOperationContext) -> Path:
        if not _BUCKET.fullmatch(bucket):
            raise ValueError("invalid filesystem bucket name")
        namespace = Path(*tenant_object_prefix(context.tenant_id).split("/"))
        tenant_root = self._root / bucket / namespace
        if not tenant_root.resolve().is_relative_to(self._root):
            raise ValueError("filesystem bucket escapes configured root")
        self._reject_symlinks(tenant_root, self._root)
        return tenant_root

    @staticmethod
    def _reject_symlinks(path: Path, trusted_root: Path) -> None:
        """Reject existing link components; descriptor opens cover the final race window."""

        current = trusted_root
        if current.is_symlink():
            raise ValueError("filesystem object-store paths cannot contain symbolic links")
        for part in path.relative_to(trusted_root).parts:
            current /= part
            if current.is_symlink():
                raise ValueError("filesystem object-store paths cannot contain symbolic links")

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
