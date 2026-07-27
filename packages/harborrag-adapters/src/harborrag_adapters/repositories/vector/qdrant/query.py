from __future__ import annotations

import hashlib
from collections.abc import MutableMapping, Sequence
from typing import Any

from harborrag_core.schemas.storage import StorageFamily, StorageOperationContext
from harborrag_core.schemas.vector import (
    VectorCollectionSpec,
    VectorPoint,
    VectorScanPage,
    VectorSearchQuery,
    VectorSearchResult,
)

from harborrag_adapters.repositories.errors import (
    HarborStorageCapabilityError,
    HarborStorageNotFoundError,
    HarborVectorDimensionError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper

qm: Any
try:
    from qdrant_client import models as _qm
except ImportError:  # pragma: no cover - optional dependency
    qm = None
else:
    qm = _qm


class QdrantQueryExecutor:
    """Executes tenant-scoped Qdrant reads against repository-owned schemas."""

    def __init__(
        self,
        *,
        client: QdrantDBClient,
        config: QdrantVectorConfig,
        specs: MutableMapping[tuple[str, str], VectorCollectionSpec],
    ) -> None:
        if qm is None:
            raise ImportError("qdrant-client is not installed")
        self._client = client
        self._config = config
        self._specs = specs

    async def get(
        self,
        collection: str,
        ids: Sequence[str],
        *,
        context: StorageOperationContext,
    ) -> list[VectorPoint]:
        records = await self._client.raw.retrieve(
            collection_name=self.collection_name(collection, context),
            ids=[QdrantMapper.point_id(str(context.tenant_id), item) for item in ids],
            with_payload=True,
            with_vectors=True,
        )
        output: list[VectorPoint] = []
        for record in records:
            payload = dict(record.payload or {})
            if payload.pop("_harbor_tenant_id", None) != str(context.tenant_id):
                continue
            vector = record.vector if isinstance(record.vector, list) else []
            logical_id = payload.pop("_harbor_point_id", str(record.id))
            output.append(
                VectorPoint(
                    id=logical_id,
                    tenant_id=context.tenant_id,
                    vector=vector,
                    payload=payload,
                )
            )
        return output

    async def scan(
        self,
        collection: str,
        *,
        limit: int,
        cursor: str | None,
        context: StorageOperationContext,
    ) -> VectorScanPage:
        records, next_offset = await self._client.raw.scroll(
            collection_name=self.collection_name(collection, context),
            scroll_filter=QdrantMapper.filter(None, context, qm),
            limit=limit,
            offset=cursor,
            with_payload=True,
            with_vectors=True,
        )
        points = [self._point(record, context) for record in records]
        return VectorScanPage(
            points=points,
            next_cursor=str(next_offset) if next_offset is not None else None,
        )

    async def search(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        spec = await self.require_spec(query.collection, context)
        self.assert_dimension(spec, query.vector)
        # Payload is always requested internally to recover the logical point id
        # from _harbor_point_id; it is stripped from the result below when the
        # caller did not ask for it via query.include_payload.
        if query.score_threshold is None:
            response = await self._query_points(
                query,
                context=context,
                limit=query.top_k,
                offset=query.offset,
            )
            return [self._search_result(point, query, spec) for point in response.points]

        # Qdrant thresholds use metric-specific raw scores while HarborRAG exposes
        # normalized scores. Page through provider results until the requested
        # normalized page is complete or the provider is exhausted; never apply a
        # silent heuristic cap that can truncate deep pages.
        output: list[VectorSearchResult] = []
        matching_seen = 0
        provider_offset = 0
        batch_size = max(100, query.top_k)
        while len(output) < query.top_k:
            response = await self._query_points(
                query,
                context=context,
                limit=batch_size,
                offset=provider_offset,
            )
            points = list(response.points)
            if not points:
                break
            for point in points:
                result = self._search_result(point, query, spec)
                if result.score < query.score_threshold:
                    continue
                if matching_seen >= query.offset:
                    output.append(result)
                    if len(output) >= query.top_k:
                        break
                matching_seen += 1
            provider_offset += len(points)
            if len(points) < batch_size:
                break
        return output

    async def require_spec(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> VectorCollectionSpec:
        key = self.spec_key(collection, context)
        try:
            return self._specs[key]
        except KeyError:
            physical_name = self.collection_name(collection, context)
            if not await self._client.raw.collection_exists(physical_name):
                raise HarborStorageNotFoundError(
                    f"collection schema {collection!r} does not exist",
                    context=self.error_context("schema_lookup", collection),
                ) from None
            info = await self._client.raw.get_collection(physical_name)
            vectors = info.config.params.vectors
            if isinstance(vectors, dict):
                raise HarborStorageCapabilityError(
                    f"collection schema {collection!r} uses unsupported named vectors",
                    context=self.error_context("schema_lookup", collection),
                ) from None
            spec = VectorCollectionSpec(
                name=collection,
                dimension=int(vectors.size),
                distance=QdrantMapper.vector_distance(vectors.distance, qm),
            )
            self._specs[key] = spec
            return spec

    async def _query_points(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
        limit: int,
        offset: int,
    ) -> Any:
        return await self._client.raw.query_points(
            collection_name=self.collection_name(query.collection, context),
            query=query.vector,
            query_filter=QdrantMapper.filter(query.filters, context, qm),
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=query.include_vectors,
        )

    @staticmethod
    def _search_result(
        point: Any,
        query: VectorSearchQuery,
        spec: VectorCollectionSpec,
    ) -> VectorSearchResult:
        normalized = QdrantMapper.normalize_score(float(point.score), spec.distance)
        payload = dict(point.payload or {})
        payload.pop("_harbor_tenant_id", None)
        logical_id = payload.pop("_harbor_point_id", str(point.id))
        vector = point.vector if query.include_vectors and isinstance(point.vector, list) else None
        return VectorSearchResult(
            id=logical_id,
            score=normalized,
            raw_score=float(point.score),
            payload=payload if query.include_payload else {},
            vector=vector,
        )

    def assert_dimension(self, spec: VectorCollectionSpec, vector: Sequence[float]) -> None:
        if len(vector) != spec.dimension:
            raise HarborVectorDimensionError(
                f"vector dimension {len(vector)} does not match {spec.dimension}",
                context=self.error_context("validate_dimension", spec.name),
            )

    def collection_name(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> str:
        tenant = self._tenant_key(context)
        return f"{self._config.collection_prefix}t_{tenant}_{collection}"

    def spec_key(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> tuple[str, str]:
        return self._tenant_key(context), collection

    @staticmethod
    def _tenant_key(context: StorageOperationContext) -> str:
        return hashlib.sha256(str(context.tenant_id).encode("utf-8")).hexdigest()[:24]

    def error_context(
        self,
        operation: str,
        resource_name: str | None = None,
        *,
        context: StorageOperationContext | None = None,
    ) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.VECTOR,
            backend="qdrant",
            instance_name=self._config.instance_name,
            operation=operation,
            tenant_id=str(context.tenant_id) if context is not None else None,
            resource_name=resource_name,
        )

    @staticmethod
    def _point(record: Any, context: StorageOperationContext) -> VectorPoint:
        payload = dict(record.payload or {})
        payload.pop("_harbor_tenant_id", None)
        logical_id = payload.pop("_harbor_point_id", str(record.id))
        vector = record.vector if isinstance(record.vector, list) else []
        return VectorPoint(
            id=logical_id,
            tenant_id=context.tenant_id,
            vector=vector,
            payload=payload,
        )
