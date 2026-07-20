from __future__ import annotations

from typing import Any

from harborrag_core.schemas.object_store import ObjectStoreCapabilities
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)

from harborrag_adapters.repositories.errors import HarborStorageNotFoundError, StorageErrorContext
from harborrag_adapters.repositories.object_store.base import HarborObjectStore
from harborrag_adapters.repositories.object_store.keys import physical_object_key
from harborrag_adapters.repositories.object_store.s3.client import S3DBClient
from harborrag_adapters.repositories.object_store.s3.config import S3ObjectStoreConfig
from harborrag_adapters.repositories.object_store.s3.operations import S3ObjectOperationsMixin
from harborrag_adapters.repositories.telemetry import RepositoryTelemetry, StorageTelemetryHook

try:
    from botocore.exceptions import ClientError  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    ClientError = Exception


class S3ObjectStore(S3ObjectOperationsMixin, HarborObjectStore):
    """Stores tenant-tagged artifacts in S3-compatible object storage."""

    def __init__(
        self,
        config: S3ObjectStoreConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: S3DBClient | None = None,
    ) -> None:
        self._config = config
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.OBJECT_STORE,
            backend="s3",
        )
        self._database = client or S3DBClient(
            endpoint_url=config.endpoint_url,
            region=config.region,
            access_key_id=(
                config.access_key_id.get_secret_value() if config.access_key_id else None
            ),
            secret_access_key=(
                config.secret_access_key.get_secret_value() if config.secret_access_key else None
            ),
            session_token=(
                config.session_token.get_secret_value() if config.session_token else None
            ),
        )

    @property
    def capabilities(self) -> ObjectStoreCapabilities:
        return ObjectStoreCapabilities(
            multipart_upload=False,
            object_versioning=False,
            presigned_urls=True,
            conditional_writes=True,
            range_downloads=True,
            server_side_encryption=True,
            streaming_upload=True,
            streaming_download=True,
        )

    async def connect(self) -> None:
        await self._database.connect()

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.OBJECT_STORE,
            backend="s3",
            instance_name=self._config.instance_name,
            status=status,
            details=details,
        )

    async def _authorize(
        self,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> dict[str, Any]:
        try:
            response: dict[str, Any] = await self.client.head_object(
                Bucket=bucket,
                Key=physical_object_key(context.tenant_id, key),
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise self._not_found(bucket, key, context) from exc
            raise
        if response.get("Metadata", {}).get("tenant_id") != str(context.tenant_id):
            # Deliberately indistinguishable from absence to prevent cross-tenant probing.
            raise self._not_found(bucket, key, context)
        return response

    @property
    def client(self) -> Any:
        return self._database.raw

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

    def _error_context(
        self,
        operation: str,
        bucket: str,
        key: str,
        context: StorageOperationContext,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.OBJECT_STORE,
            backend="s3",
            instance_name=self._config.instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id),
            resource_name=f"{bucket}/{key}",
        )
