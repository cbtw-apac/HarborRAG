from __future__ import annotations

from collections.abc import Sequence
from typing import cast

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
from harborrag_adapters.repositories.vector.qdrant.health import qdrant_health
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query import QdrantQueryExecutor
from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    VectorFilter,
    VectorIndexRecord,
    VectorIndexScanPage,
    VectorIndexSpec,
    VectorSearchQuery,
    VectorSearchResult,
    VectorStoreCapabilities,
)
from harborrag_core.storage import (
    RepositoryHealth,
    StorageFamily,
    StorageOperationContext,
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
        self._specs: dict[tuple[str, str], VectorIndexSpec] = {}
        self._collection_locks = {}
        self._queries = QdrantQueryExecutor(
            client=self._database,
            config=config,
            specs=self._specs,
        )

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        return VectorStoreCapabilities(
            supports_dense_vectors=True,
            supports_sparse_vectors=True,
            supports_named_vectors=True,
            supports_hybrid_search=True,
            supports_metadata_filtering=True,
            supports_delete_by_filter=True,
            supports_index_aliases=False,
            supports_quantization=False,
            supports_pagination=True,
        )

    async def connect(self) -> None:
        await self._database.connect()

    async def close(self) -> None:
        await self._database.close()

    async def health(self) -> RepositoryHealth:
        return await qdrant_health(self._database, self._config)

    @traced_repository_operation("upsert_records")
    async def upsert_records(
        self,
        index_name: str,
        records: Sequence[VectorIndexRecord],
        *,
        context: StorageOperationContext,
    ) -> None:
        spec = await self._queries.require_spec(index_name, context)
        qdrant_points = []
        for point in records:
            ensure_tenant(
                point.tenant_id,
                context,
                error_context=self._queries.error_context(
                    "upsert_records", index_name, context=context
                ),
            )
            self._queries.assert_dimension(spec, point.vector)
            vector: (
                list[float]
                | dict[
                    str,
                    list[float] | qm.SparseVector,
                ]
            ) = point.vector
            if spec.dense_vector_name is not None:
                reserved = {
                    spec.dense_vector_name,
                    spec.sparse_vector_name,
                }
                if any(name in reserved for name in point.named_vectors):
                    raise HarborStorageCapabilityError(
                        "point named vectors must not replace collection-owned lanes",
                        context=self._queries.error_context(
                            "upsert_records", index_name, context=context
                        ),
                    )
                vector = {
                    spec.dense_vector_name: point.vector,
                    **point.named_vectors,
                }
                if spec.sparse_vector_name is not None:
                    if point.sparse_vector is None:
                        raise HarborStorageCapabilityError(
                            "the collection requires a sparse vector for every point",
                            context=self._queries.error_context(
                                "upsert_records", index_name, context=context
                            ),
                        )
                    vector[spec.sparse_vector_name] = qm.SparseVector(
                        indices=point.sparse_vector.indices,
                        values=point.sparse_vector.values,
                    )
            qdrant_points.append(
                QdrantMapper.point(
                    point,
                    cast("qm.VectorStruct", vector),
                    qm,
                )
            )
        await self._database.raw.upsert(
            collection_name=self._queries.collection_name(index_name, context),
            points=qdrant_points,
            wait=True,
        )

    @traced_repository_operation("get_records")
    async def get_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorIndexRecord]:
        return await self._queries.get(index_name, ids, context=context)

    @traced_repository_operation("delete_records")
    async def delete_records(
        self,
        index_name: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> None:
        await self._database.raw.delete(
            collection_name=self._queries.collection_name(index_name, context),
            points_selector=qm.FilterSelector(
                filter=qm.Filter(
                    must=[
                        qm.HasIdCondition(has_id=[QdrantMapper.point_id(item) for item in ids]),
                    ]
                )
            ),
            wait=True,
        )

    @traced_repository_operation("scan_records")
    async def scan_records(
        self,
        index_name: str,
        *,
        limit: int,
        cursor: str | None,
        filters: VectorFilter | None = None,
        context: StorageOperationContext,
    ) -> VectorIndexScanPage:
        return await self._queries.scan(
            index_name,
            limit=limit,
            cursor=cursor,
            filters=filters,
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

    @traced_repository_operation("sparse_search")
    async def sparse_search(
        self,
        query: SparseSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        return await self._queries.sparse_search(query, context=context)

    @traced_repository_operation("hybrid_search")
    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        return await self._queries.hybrid_search(query, context=context)
