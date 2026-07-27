from __future__ import annotations

from collections.abc import Sequence
from typing import Any

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

from harborrag_adapters.repositories.errors import (
    HarborStorageCapabilityError,
    HarborStorageValidationError,
)
from harborrag_adapters.repositories.shared.tenancy import ensure_tenant
from harborrag_adapters.repositories.telemetry import (
    RepositoryTelemetry,
    StorageTelemetryHook,
    traced_repository_operation,
)
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor

qm: Any
try:
    from qdrant_client import models as _qm
except ImportError:  # pragma: no cover - optional dependency
    qm = None
else:
    qm = _qm


class QdrantVectorRepository(HarborVectorRepository):
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

    @traced_repository_operation("ensure_collection")
    async def ensure_collection(
        self,
        spec: VectorCollectionSpec,
        *,
        context: StorageOperationContext,
    ) -> None:
        if not spec.tenant_scoped:
            raise HarborStorageValidationError(
                "the Qdrant adapter requires tenant_scoped=True",
                context=self._queries.error_context(
                    "ensure_collection", spec.name, context=context
                ),
            )
        name = self._queries.collection_name(spec.name, context)
        client = self._database.raw
        if not await client.collection_exists(name):
            await client.create_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(
                    size=spec.dimension,
                    distance=QdrantMapper.distance(spec.distance, qm),
                ),
            )
            for field in spec.metadata_indexes:
                await client.create_payload_index(
                    collection_name=name,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            self._specs[self._queries.spec_key(spec.name, context)] = spec
            return
        persisted = await self._queries.require_spec(spec.name, context)
        if (persisted.dimension, persisted.distance) != (spec.dimension, spec.distance):
            raise HarborStorageValidationError(
                f"collection {spec.name!r} already exists with dimension "
                f"{persisted.dimension} and distance {persisted.distance}",
                context=self._queries.error_context("ensure_collection", spec.name),
            )
        info = await client.get_collection(name)
        existing_indexes = set(getattr(info, "payload_schema", {}) or {})
        for field in spec.metadata_indexes:
            if field in existing_indexes:
                continue
            await client.create_payload_index(
                collection_name=name,
                field_name=field,
                field_schema=qm.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        self._specs[self._queries.spec_key(spec.name, context)] = spec

    @traced_repository_operation("collection_exists")
    async def collection_exists(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> bool:
        return bool(
            await self._database.raw.collection_exists(self._queries.collection_name(name, context))
        )

    @traced_repository_operation("delete_collection")
    async def delete_collection(
        self,
        name: str,
        *,
        context: StorageOperationContext,
    ) -> None:
        physical_name = self._queries.collection_name(name, context)
        if await self._database.raw.collection_exists(physical_name):
            await self._database.raw.delete_collection(collection_name=physical_name)
        self._specs.pop(self._queries.spec_key(name, context), None)

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
