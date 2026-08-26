from __future__ import annotations

import hashlib
import tempfile
from typing import Any

from harborrag_adapters.repositories.errors import HarborObjectTooLargeError, StorageErrorContext
from harborrag_adapters.repositories.object_store.body import iter_body
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_core.schemas.object_store import PutObjectRequest
from harborrag_core.storage import StorageOperationContext


class S3BodySpoolMixin:
    """Spool streaming bodies before one atomic conditional S3 write."""

    _config: S3ObjectStoreConfig

    def _error_context(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        raise NotImplementedError

    async def _run_sync(self, function: Any, *args: Any) -> Any:
        """Run a blocking spool operation using the composing store's executor."""
        raise NotImplementedError

    async def _spool_body(
        self,
        request: PutObjectRequest,
        context: StorageOperationContext,
    ) -> tuple[Any, str, int]:
        handle = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in iter_body(request.body):
                size += len(chunk)
                if size > 5_000_000_000:
                    raise HarborObjectTooLargeError(
                        "conditional S3 uploads are limited to the 5 GB PutObject limit",
                        context=self._error_context(
                            "put",
                            request.bucket or self._config.default_bucket or "",
                            request.key,
                            context,
                        ),
                    )
                digest.update(chunk)
                await self._run_sync(handle.write, chunk)
            await self._run_sync(handle.seek, 0)
            return handle, digest.hexdigest(), size
        except BaseException:
            await self._run_sync(handle.close)
            raise
