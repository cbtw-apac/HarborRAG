from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborStorageAlreadyExistsError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.object_store.keys import (
    logical_object_key,
    physical_object_key,
    validate_object_key,
)
from harborrag_adapters.repositories.object_store.s3.body import S3BodySpoolMixin
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.object_metadata import (
    ClientError,
    S3ObjectMetadataMixin,
)
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    traced_repository_operation,
)
from harborrag_core.schemas.object_store import (
    ObjectMetadata,
    ObjectReference,
    PutObjectRequest,
)
from harborrag_core.storage import StorageOperationContext


class S3ObjectOperationsMixin(S3BodySpoolMixin, S3ObjectMetadataMixin):
    """Implements tenant-authorized S3 object reads, writes, listing, and deletion.

    Composed onto ``S3ObjectStore``, which supplies the ``_config``, ``client``,
    ``_authorize``, and ``_error_context`` members declared below.
    """

    _config: S3ObjectStoreConfig
    _telemetry: RepositoryTelemetry
    client: Any

    async def _authorize(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _error_context(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        raise NotImplementedError

    async def _run_sync(self, function: Any, *args: Any) -> Any:
        return await asyncio.to_thread(function, *args)

    @traced_repository_operation("put")
    async def put(
        self,
        request: PutObjectRequest,
        *,
        context: StorageOperationContext,
    ) -> ObjectReference:
        bucket = request.bucket or self._config.default_bucket
        if not bucket:
            raise HarborStorageValidationError(
                "S3 bucket is required",
                context=self._error_context("put", "", request.key, context),
            )
        validate_object_key(bucket, request.key)
        physical_key = physical_object_key(context.tenant_id, request.key)
        existing = await self._existing_head(bucket, physical_key)
        common = self._put_parameters(
            bucket=bucket,
            physical_key=physical_key,
            request=request,
            context=context,
            existing=existing,
        )

        body_handle: Any | None = None
        try:
            body_handle, checksum, size = await self._spool_body(request, context)
            if request.checksum_sha256 and checksum != request.checksum_sha256:
                raise HarborStorageValidationError(
                    "object checksum does not match request checksum",
                    context=self._error_context("put", bucket, request.key, context),
                )
            try:
                response = await self.client.put_object(Body=body_handle, **common)
            except ClientError as exc:
                if self._client_error_code(exc) in {
                    "409",
                    "412",
                    "ConditionalRequestConflict",
                    "PreconditionFailed",
                }:
                    raise HarborStorageAlreadyExistsError(
                        f"object {bucket}/{request.key} changed during conditional write",
                        context=self._error_context("put", bucket, request.key, context),
                    ) from exc
                raise
        finally:
            if body_handle is not None:
                await asyncio.to_thread(body_handle.close)
        return ObjectReference(
            bucket=bucket,
            key=request.key,
            uri=f"s3://{bucket}/{request.key}",
            version_id=response.get("VersionId"),
            etag=str(response.get("ETag", "")).strip('"') or None,
            checksum_sha256=checksum,
            size_bytes=size,
            content_type=request.content_type,
        )

    def _put_parameters(
        self,
        *,
        bucket: str,
        physical_key: str,
        request: PutObjectRequest,
        context: StorageOperationContext,
        existing: dict[str, Any] | None,
    ) -> dict[str, Any]:
        existing_tenant = (
            existing.get("Metadata", {}).get("tenant_id") if existing is not None else None
        )
        if existing is not None and (
            request.if_none_match or existing_tenant != str(context.tenant_id)
        ):
            raise HarborStorageAlreadyExistsError(
                f"object {bucket}/{request.key} already exists",
                context=self._error_context("put", bucket, request.key, context),
            )
        metadata = {**request.metadata, "tenant_id": str(context.tenant_id)}
        common: dict[str, Any] = {
            "Bucket": bucket,
            "Key": physical_key,
            "Metadata": metadata,
        }
        if request.content_type:
            common["ContentType"] = request.content_type
        if self._config.server_side_encryption:
            common["ServerSideEncryption"] = self._config.server_side_encryption
        if existing is None:
            common["IfNoneMatch"] = "*"
        else:
            etag = str(existing.get("ETag", "")).strip()
            if not etag:
                raise HarborStorageAlreadyExistsError(
                    f"object {bucket}/{request.key} cannot be conditionally replaced",
                    context=self._error_context("put", bucket, request.key, context),
                )
            common["IfMatch"] = etag
        return common

    @traced_repository_operation("get_bytes")
    async def get_bytes(
        self,
        bucket: str,
        key: str,
        *,
        byte_range: tuple[int, int] | None,
        context: StorageOperationContext,
    ) -> bytes:
        await self._authorize(bucket, key, context)
        kwargs: dict[str, Any] = {
            "Bucket": bucket,
            "Key": physical_object_key(context.tenant_id, key),
        }
        if byte_range:
            kwargs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        response = await self.client.get_object(**kwargs)
        async with response["Body"] as stream:
            data: bytes = await stream.read()
            return data

    async def iter_bytes(
        self,
        bucket: str,
        key: str,
        *,
        chunk_size: int,
        context: StorageOperationContext,
    ) -> AsyncIterator[bytes]:
        async with self._telemetry.operation("iter_bytes", context):
            await self._authorize(bucket, key, context)
            response = await self.client.get_object(
                Bucket=bucket,
                Key=physical_object_key(context.tenant_id, key),
            )
            async with response["Body"] as stream:
                while chunk := await stream.read(chunk_size):
                    yield chunk

    @traced_repository_operation("head")
    async def head(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> ObjectMetadata:
        response = await self._authorize(bucket, key, context)
        modified = response.get("LastModified")
        return ObjectMetadata(
            reference=ObjectReference(
                bucket=bucket,
                key=key,
                uri=f"s3://{bucket}/{key}",
                version_id=response.get("VersionId"),
                etag=str(response.get("ETag", "")).strip('"') or None,
                size_bytes=int(response.get("ContentLength", 0)),
                content_type=response.get("ContentType"),
                created_at=(
                    modified.astimezone(UTC)
                    if isinstance(modified, datetime)
                    else datetime.now(UTC)
                ),
            ),
            metadata=dict(response.get("Metadata") or {}),
            last_modified=modified,
        )

    @traced_repository_operation("exists")
    async def exists(
        self,
        bucket: str,
        key: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        try:
            await self._authorize(bucket, key, context)
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
        try:
            await self._authorize(bucket, key, context)
        except HarborStorageNotFoundError:
            return False
        await self.client.delete_object(
            Bucket=bucket,
            Key=physical_object_key(context.tenant_id, key),
        )
        return True

    @traced_repository_operation("list")
    async def list(
        self,
        bucket: str,
        prefix: str,
        *,
        limit: int,
        context: StorageOperationContext,
    ) -> list[ObjectMetadata]:
        output: list[ObjectMetadata] = []
        token: str | None = None
        while len(output) < limit:
            kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Prefix": physical_object_key(context.tenant_id, prefix),
                "MaxKeys": min(1000, limit - len(output)),
            }
            if token:
                kwargs["ContinuationToken"] = token
            page = await self.client.list_objects_v2(**kwargs)
            for item in page.get("Contents", []):
                logical_key = logical_object_key(context.tenant_id, item["Key"])
                if logical_key is None:
                    continue
                try:
                    output.append(await self.head(bucket, logical_key, context=context))
                except HarborStorageNotFoundError:
                    continue
                if len(output) >= limit:
                    break
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
        return output

    @traced_repository_operation("presign_download")
    async def presign_download(
        self,
        bucket: str,
        key: str,
        *,
        expires_seconds: int,
        context: StorageOperationContext,
    ) -> str:
        authorized = await self._authorize(bucket, key, context)
        params = {
            "Bucket": bucket,
            "Key": physical_object_key(context.tenant_id, key),
        }
        if authorized.get("VersionId"):
            params["VersionId"] = authorized["VersionId"]
        url: str = await self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=expires_seconds,
        )
        return url
