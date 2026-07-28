from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborStorageCapabilityError,
)
from harborrag_adapters.repositories.policies.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.collections import (
    QdrantCollectionMixin,
)
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor
from harborrag_core.schemas.storage import (
    HealthStatus,
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
)
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    VectorCollectionSpec,
    VectorPoint,
    VectorScanPage,
    VectorSearchQuery,
    VectorSearchResult,
    VectorStoreCapabilities,
)

qm: Any
try:
    from qdrant_client import models as _qm
except ImportError:  # pragma: no cover - optional dependency
    qm = None
else:
    qm = _qm


class QdrantVectorRepository(QdrantCollectionMixin, HarborVectorRepository):
    """Stores and searches tenant-scoped dense vectors through Qdrant."""

    def __init__(
        self,
        config: QdrantVectorConfig,
        telemetry: StorageTelemetryHook | None = None,
        *,
        client: QdrantDBClient | None = None,
    ) -> None:
        if qm is None:
            raise ImportError("qdrant-client is not installed")
        self._config = config
        self._telemetry = RepositoryTelemetry(
            telemetry,
            family=StorageFamily.VECTOR,
            backend="qdrant",
        )
        self._database = client or QdrantDBClient(
            deployment=config.deployment,
            url=config.url,
            path=config.path,
            api_key=config.api_key.get_secret_value() if config.api_key else None,
            prefer_grpc=config.prefer_grpc,
            operation_timeout_seconds=config.operation_timeout_seconds,
        )
        self._specs: dict[tuple[str, str], VectorCollectionSpec] = {}
        self._collection_locks = {}
        self._queries = QdrantQueryExecutor(
            client=self._database,
            config=config,
            specs=self._specs,
        )

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        return VectorStoreCapabilities(
            dense_vectors=True,
            sparse_vectors=False,
            named_vectors=False,
            hybrid_search=False,
            metadata_filtering=True,
            collection_aliases=False,
            quantization=False,
            pagination=True,
        )

    async def connect(self) -> None:
        await self._database.connect()

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        if not self._database.is_connected:
            return RepositoryHealth(
                family=StorageFamily.VECTOR,
                backend="qdrant",
                instance_name=self._config.instance_name,
                status=HealthStatus.UNKNOWN,
                details={
                    "deployment": self._database.deployment,
                    "storage": self._database.storage,
                },
            )
        try:
            await self._database.ping()
            status = HealthStatus.HEALTHY
            details: dict[str, Any] = {}
        except Exception as exc:  # pragma: no cover - integration behavior
            status = HealthStatus.UNHEALTHY
            details = {"error_type": type(exc).__name__}
        return RepositoryHealth(
            family=StorageFamily.VECTOR,
            backend="qdrant",
            instance_name=self._config.instance_name,
            status=status,
            details={
                **details,
                "deployment": self._database.deployment,
                "storage": self._database.storage,
            },
        )

    @traced_repository_operation("upsert")
    async def upsert(
        self,
        collection: str,
        points: Sequence[VectorPoint],
        *,
        context: StorageOperationContext,
    ) -> None:
        spec = await self._queries.require_spec(collection, context)
        qdrant_points = []
        for point in points:
            ensure_tenant(
                point.tenant_id,
                context,
                error_context=self._queries.error_context("upsert", collection, context=context),
            )
            self._queries.assert_dimension(spec, point.vector)
            qdrant_points.append(
                qm.PointStruct(
                    id=QdrantMapper.point_id(str(context.tenant_id), point.id),
                    vector=point.vector,
                    payload={
                        **point.payload,
                        "_harbor_tenant_id": str(context.tenant_id),
                        "_harbor_point_id": point.id,
                    },
                )
            )
        await self._database.raw.upsert(
            collection_name=self._queries.collection_name(collection, context),
            points=qdrant_points,
            wait=True,
        )

    @traced_repository_operation("activate_generation")
    async def activate_generation(
        self,
        collection: str,
        *,
        artifact_id: str,
        generation_id: str,
        activate_ids: Sequence[str],
        retire_ids: Sequence[str],
        delete_ids: Sequence[str],
        tombstone_ids: Sequence[str],
        context: StorageOperationContext,
    ) -> None:
        """Apply a validated, idempotent vector visibility plan."""

        physical_name = self._queries.collection_name(collection, context)
        client = self._database.raw
        await self._set_index_state(
            client,
            physical_name,
            activate_ids,
            artifact_id=artifact_id,
            generation_id=generation_id,
            index_state="active",
            is_active=True,
            context=context,
        )
        await self._set_index_state(
            client,
            physical_name,
            retire_ids,
            artifact_id=artifact_id,
            generation_id=None,
            index_state="retired",
            is_active=False,
            context=context,
        )
        await self._set_index_state(
            client,
            physical_name,
            tombstone_ids,
            artifact_id=artifact_id,
            generation_id=None,
            index_state="tombstoned",
            is_active=False,
            context=context,
            extra_payload={"tombstone": True},
        )
        if delete_ids:
            await self.delete(collection, delete_ids, context=context)

    @traced_repository_operation("get")
    async def get(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorPoint]:
        return await self._queries.get(collection, ids, context=context)

    @traced_repository_operation("delete")
    async def delete(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> None:
        await self._database.raw.delete(
            collection_name=self._queries.collection_name(collection, context),
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.HasIdCondition(
                            has_id=[
                                QdrantMapper.point_id(str(context.tenant_id), item) for item in ids
                            ]
                        ),
                        qm.FieldCondition(
                            key="_harbor_tenant_id",
                            match=qm.MatchValue(value=str(context.tenant_id)),
                        ),
                    ]
                )
            ),
            wait=True,
        )

    @traced_repository_operation("scan")
    async def scan(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        context: StorageOperationContext,
    ) -> VectorScanPage:
        return await self._queries.scan(
            collection,
            limit=limit,
            cursor=cursor,
            context=context,
        )

    @traced_repository_operation("search")
    async def search(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        return await self._queries.search(query, context=context)

    @traced_repository_operation("hybrid_search")
    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        del query, context
        raise HarborStorageCapabilityError(
            "this baseline Qdrant adapter exposes dense search only; add named sparse vectors "
            "to the collection schema before enabling provider-native fusion",
            context=self._queries.error_context("hybrid_search"),
        )

    async def _set_index_state(
        self,
        client: Any,
        collection: str,
        point_ids: Sequence[str],
        *,
        artifact_id: str,
        generation_id: str | None,
        index_state: str,
        is_active: bool,
        context: StorageOperationContext,
        extra_payload: dict[str, Any] | None = None,
    ) -> None:
        if not point_ids:
            return
        must: list[Any] = [
            qm.HasIdCondition(
                has_id=[
                    QdrantMapper.point_id(str(context.tenant_id), identity)
                    for identity in point_ids
                ]
            ),
            qm.FieldCondition(
                key="_harbor_tenant_id",
                match=qm.MatchValue(value=str(context.tenant_id)),
            ),
            qm.FieldCondition(
                key="artifact_id",
                match=qm.MatchValue(value=artifact_id),
            ),
        ]
        if generation_id is not None:
            must.append(
                qm.FieldCondition(
                    key="generation_id",
                    match=qm.MatchValue(value=generation_id),
                )
            )
        await client.set_payload(
            collection_name=collection,
            payload={
                "index_state": index_state,
                "is_active": is_active,
                **(extra_payload or {}),
            },
            points=qm.Filter(must=must),
            wait=True,
        )
