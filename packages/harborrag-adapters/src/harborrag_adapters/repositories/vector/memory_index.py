"""Qdrant-backed semantic index for canonical long-term memories.

Implements ``harborrag_core.ports.memory.MemoryIndex`` against a dedicated
logical index (``memories``), never the document/evidence collection:
retention and erasure differ (a memory is deleted on a user's request, an
evidence point when its document version is superseded), so the two must not
share a collection's lifecycle.

**Dense-only, by necessity.** ``QdrantVectorRepository.upsert_records``
requires a sparse vector for *every* point once a collection declares a
sparse lane, and the only text encoder this index is given is a
``MemoryEmbedder`` (dense floats). Declaring a sparse lane without a sparse
encoder would therefore break every write, not merely weaken ranking, so the
collection is created with a single unnamed dense lane. When a sparse encoder
becomes injectable, add the lane in ``memory_index_spec`` and switch the
search call to ``hybrid_search``; ``MemoryMatch.score`` is already documented
as an unbounded fused relevance to allow that.

Two further deliberate limits: the index stores no memory content (Postgres
stays the system of record and recall hydrates ids through it), and
``MemoryQuery.as_of`` is not applied here -- the index only excludes rows
that were invalidated at all, and point-in-time validity is evaluated by the
SQL repository that owns the bitemporal columns.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from harborrag_adapters.repositories.errors import (
    HarborStorageError,
    HarborStorageValidationError,
    HarborVectorDimensionError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.vector.base import HarborVectorRepository
from harborrag_adapters.repositories.vector.memory_index_mapping import (
    MEMORY_INDEX,
    match_of,
    memory_index_spec,
    memory_payload,
    query_scopes,
    ranked_matches,
    scope_filter,
)
from harborrag_core.indexing import (
    VectorDistance,
    VectorIndexRecord,
    VectorSearchQuery,
)
from harborrag_core.ports.memory import (
    Memory,
    MemoryEmbedder,
    MemoryMatch,
    MemoryOwner,
    MemoryQuery,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.storage import StorageFamily, StorageOperationContext


class QdrantMemoryIndex:
    """Index and semantically recall memory ids inside one tenant's collection."""

    def __init__(
        self,
        repository: HarborVectorRepository,
        *,
        dimensions: int,
        embedder: MemoryEmbedder | None = None,
        distance: VectorDistance = VectorDistance.COSINE,
        instance_name: str = "default",
    ) -> None:
        if dimensions < 1:
            raise ValueError("memory index dimensions must be positive")
        self._repository = repository
        self._dimensions = dimensions
        self._embedder = embedder
        self._distance = distance
        self._instance_name = instance_name
        self._ensured: set[str] = set()
        self._ensure_lock = asyncio.Lock()

    async def index_memory(
        self,
        memory: Memory,
        *,
        embedding: Sequence[float] | None = None,
    ) -> None:
        """Upsert one memory's vector and scope payload, content excluded."""

        tenant_id = memory.owner.tenant_id
        vector = await self._vector("index_memory", tenant_id, memory.content, embedding)
        context = self._context(tenant_id, "memory_index_write")
        await self._ensure_index(tenant_id, context)
        record = VectorIndexRecord(
            id=memory.memory_id,
            tenant_id=TenantId(tenant_id),
            vector=list(vector),
            payload=memory_payload(memory),
        )
        async with self._guard("index_memory", tenant_id):
            await self._repository.upsert_records(MEMORY_INDEX, (record,), context=context)

    async def search_memories(
        self,
        query: MemoryQuery,
        *,
        embedding: Sequence[float] | None = None,
    ) -> tuple[MemoryMatch, ...]:
        """Recall memory ids the caller may see, best score first.

        One search per requested scope: the portable ``VectorFilter`` is a
        flat conjunction, so an OR of per-scope conjunctions cannot be
        expressed in a single query without weakening the isolation
        predicate. Hits are fused by memory id, keeping the best score.
        """

        tenant_id = query.owner.tenant_id
        vector = await self._vector("search_memories", tenant_id, query.text, embedding)
        context = self._context(tenant_id, "memory_index_search")
        await self._ensure_index(tenant_id, context)
        scores: dict[str, float] = {}
        for scope in query_scopes(query):
            filters = scope_filter(scope, query)
            if filters is None:
                continue
            search = VectorSearchQuery(
                index_name=MEMORY_INDEX,
                vector=list(vector),
                top_k=query.limit,
                filters=filters,
            )
            async with self._guard("search_memories", tenant_id):
                results = await self._repository.search(search, context=context)
            for result in results:
                match = match_of(result)
                if match is None:
                    continue
                previous = scores.get(match.memory_id)
                if previous is None or match.score > previous:
                    scores[match.memory_id] = match.score
        return ranked_matches(scores, limit=query.limit)

    async def delete_memory(self, owner: MemoryOwner, memory_id: str) -> None:
        """Erase one memory's point from its own tenant's collection."""

        tenant_id = owner.tenant_id
        context = self._context(tenant_id, "memory_index_delete")
        await self._ensure_index(tenant_id, context)
        async with self._guard("delete_memory", tenant_id):
            await self._repository.delete_records(MEMORY_INDEX, (memory_id,), context=context)

    async def _ensure_index(self, tenant_id: str, context: StorageOperationContext) -> None:
        if tenant_id in self._ensured:
            return
        async with self._ensure_lock:
            if tenant_id in self._ensured:
                return
            spec = memory_index_spec(dimension=self._dimensions, distance=self._distance)
            async with self._guard("ensure_index", tenant_id):
                await self._repository.ensure_index(spec, context=context)
            self._ensured.add(tenant_id)

    async def _vector(
        self,
        operation: str,
        tenant_id: str,
        text: str | None,
        embedding: Sequence[float] | None,
    ) -> Sequence[float]:
        if embedding is None:
            if self._embedder is None or text is None:
                raise HarborStorageValidationError(
                    "memory index requires an embedding or an embedder and text",
                    context=self._error_context(operation, tenant_id),
                )
            async with self._guard(operation, tenant_id):
                embedding = await self._embedder(text)
        if len(embedding) != self._dimensions:
            raise HarborVectorDimensionError(
                f"memory embedding dimension {len(embedding)} does not match {self._dimensions}",
                context=self._error_context(operation, tenant_id),
            )
        return embedding

    @asynccontextmanager
    async def _guard(self, operation: str, tenant_id: str) -> AsyncIterator[None]:
        """Let normalized storage errors through, wrap anything provider-shaped."""

        try:
            yield
        except HarborStorageError:
            raise
        except Exception as error:
            raise HarborStorageError(
                f"memory index {operation} failed",
                context=self._error_context(operation, tenant_id),
                original=error,
            ) from error

    def _context(self, tenant_id: str, operation_kind: str) -> StorageOperationContext:
        return StorageOperationContext.system(tenant_id, operation_kind=operation_kind)

    def _error_context(self, operation: str, tenant_id: str) -> StorageErrorContext:
        return StorageErrorContext(
            family=StorageFamily.VECTOR,
            backend="qdrant",
            instance_name=self._instance_name,
            operation=operation,
            tenant_id=tenant_id,
            resource_name=MEMORY_INDEX,
        )


__all__ = ["MEMORY_INDEX", "QdrantMemoryIndex"]
