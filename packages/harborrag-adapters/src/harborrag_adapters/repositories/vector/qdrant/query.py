from __future__ import annotations

import re
from collections.abc import MutableMapping, Sequence
from typing import Any

from harborrag_adapters.repositories.errors import (
    HarborStorageCapabilityError,
    HarborStorageNotFoundError,
    HarborStorageValidationError,
    HarborVectorDimensionError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.vector.qdrant.client import QdrantDBClient
from harborrag_adapters.repositories.vector.qdrant.config import QdrantVectorConfig
from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_adapters.repositories.vector.qdrant.query_mapping import (
    fused_result,
    point_record,
    search_result,
    sparse_result,
    vectors,
    weighted_rrf,
)
from harborrag_adapters.repositories.vector.qdrant.schema_mapping import (
    collection_spec_from_qdrant,
)
from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    VectorIndexRecord,
    VectorIndexScanPage,
    VectorIndexSpec,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.storage import StorageFamily, StorageOperationContext

try:
    from qdrant_client import models as qm
except ImportError:  # pragma: no cover - optional dependency
    qm = None  # type: ignore[assignment]


class QdrantQueryExecutor:
    """Executes tenant-scoped Qdrant reads against repository-owned schemas."""

    def __init__(
        self,
        *,
        client: QdrantDBClient,
        config: QdrantVectorConfig,
        specs: MutableMapping[tuple[str, str], VectorIndexSpec],
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
    ) -> list[VectorIndexRecord]:
        spec = await self.require_spec(collection, context)
        records = await self._client.raw.retrieve(
            collection_name=self.collection_name(collection, context),
            ids=[QdrantMapper.point_id(item) for item in ids],
            with_payload=True,
            with_vectors=True,
        )
        output: list[VectorIndexRecord] = []
        for record in records:
            payload = dict(record.payload or {})
            vector, sparse, named = vectors(record.vector, spec)
            output.append(
                VectorIndexRecord(
                    id=str(record.id),
                    tenant_id=context.tenant_id,
                    vector=vector,
                    sparse_vector=sparse,
                    named_vectors=named,
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
    ) -> VectorIndexScanPage:
        spec = await self.require_spec(collection, context)
        records, next_offset = await self._client.raw.scroll(
            collection_name=self.collection_name(collection, context),
            scroll_filter=None,
            limit=limit,
            offset=cursor,
            with_payload=True,
            with_vectors=True,
        )
        points = [point_record(record, context, spec) for record in records]
        return VectorIndexScanPage(
            records=points,
            next_cursor=str(next_offset) if next_offset is not None else None,
        )

    async def search(
        self,
        query: VectorSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        spec = await self.require_spec(query.index_name, context)
        self.assert_dimension(spec, query.vector)
        # Payload is requested internally for authoritative candidate filtering.
        if query.score_threshold is None:
            response = await self._query_points(
                query,
                context=context,
                limit=query.top_k,
                offset=query.offset,
            )
            return [search_result(point, query, spec) for point in response.points]

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
                result = search_result(point, query, spec)
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

    async def hybrid_search(
        self,
        query: HybridSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        spec = await self.require_spec(query.index_name, context)
        self.assert_dimension(spec, query.vector)
        if spec.dense_vector_name is None or spec.sparse_vector_name is None:
            raise HarborStorageCapabilityError(
                f"collection schema {query.index_name!r} has no sparse vector lane",
                context=self.error_context("hybrid_search", query.index_name, context=context),
            )
        candidate_limit = min(
            1000,
            max(query.top_k + query.offset, query.top_k * 4),
        )
        common = {
            "collection_name": self.collection_name(query.index_name, context),
            "query_filter": QdrantMapper.filter(query.filters, qm),
            "limit": candidate_limit,
            "with_payload": True,
            "with_vectors": query.include_vectors,
        }
        dense_response = await self._client.raw.query_points(
            query=query.vector,
            using=spec.dense_vector_name,
            **common,
        )
        sparse_response = await self._client.raw.query_points(
            query=qm.SparseVector(
                indices=query.sparse_vector.indices,
                values=query.sparse_vector.values,
            ),
            using=spec.sparse_vector_name,
            **common,
        )
        fused = weighted_rrf(
            dense_response.points,
            sparse_response.points,
            dense_weight=query.dense_weight,
        )
        output: list[VectorSearchResult] = []
        for raw_score, point in fused:
            score = min(1.0, raw_score * 61.0)
            if query.score_threshold is not None and score < query.score_threshold:
                continue
            output.append(
                fused_result(
                    point,
                    score=score,
                    raw_score=raw_score,
                    query=query,
                    spec=spec,
                )
            )
        return output[query.offset : query.offset + query.top_k]

    async def sparse_search(
        self,
        query: SparseSearchQuery,
        *,
        context: StorageOperationContext,
    ) -> list[VectorSearchResult]:
        spec = await self.require_spec(query.index_name, context)
        if spec.sparse_vector_name is None:
            raise HarborStorageCapabilityError(
                f"collection schema {query.index_name!r} has no sparse vector lane",
                context=self.error_context("sparse_search", query.index_name, context=context),
            )
        response = await self._client.raw.query_points(
            collection_name=self.collection_name(query.index_name, context),
            query=qm.SparseVector(
                indices=query.sparse_vector.indices,
                values=query.sparse_vector.values,
            ),
            using=spec.sparse_vector_name,
            query_filter=QdrantMapper.filter(query.filters, qm),
            limit=query.top_k,
            offset=query.offset,
            with_payload=True,
            with_vectors=query.include_vectors,
        )
        output = [sparse_result(point, query=query, spec=spec) for point in response.points]
        if query.score_threshold is None:
            return output
        return [result for result in output if result.score >= query.score_threshold]

    async def require_spec(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> VectorIndexSpec:
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
            spec = collection_spec_from_qdrant(
                info,
                collection=collection,
                error_context=self.error_context("schema_lookup", collection),
                models=qm,
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
        spec = await self.require_spec(query.index_name, context)
        return await self._client.raw.query_points(
            collection_name=self.collection_name(query.index_name, context),
            query=query.vector,
            using=spec.dense_vector_name,
            query_filter=QdrantMapper.filter(query.filters, qm),
            limit=limit,
            offset=offset,
            with_payload=True,
            with_vectors=query.include_vectors,
        )

    def assert_dimension(self, spec: VectorIndexSpec, vector: Sequence[float]) -> None:
        if len(vector) != spec.dimension:
            raise HarborVectorDimensionError(
                f"vector dimension {len(vector)} does not match {spec.dimension}",
                context=self.error_context("validate_dimension", spec.index_name),
            )

    def collection_name(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> str:
        tenant = str(context.tenant_id)
        for label, value in (("tenant", tenant), ("index", collection)):
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is None:
                raise HarborStorageValidationError(
                    f"Qdrant {label} name must use 1-128 ASCII letters, digits, '.', '_' or '-'",
                    context=self.error_context(
                        "collection_name",
                        collection,
                        context=context,
                    ),
                )
        return f"{self._config.collection_prefix}{tenant}_{collection}"

    def spec_key(
        self,
        collection: str,
        context: StorageOperationContext,
    ) -> tuple[str, str]:
        return str(context.tenant_id), collection

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
