"""Graph-facing operations mixed into the authoritative retrieval service."""

from __future__ import annotations

from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_core.security import AccessContext
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import (
    AuthoritativeGraphSearch,
    AuthoritativePathResult,
    AuthoritativeSubgraphResult,
    AuthoritativeTripletResult,
)


class RuntimeGraphRetrievalMixin:
    """Expose bounded graph searches while sharing retrieval authorization context."""

    _graph_search: AuthoritativeGraphSearch | None

    async def search_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeTripletResult:
        return await self._require_graph_search().triplets(
            query,
            context=self._graph_context(access, "graph-triplet-search"),
        )

    async def search_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativePathResult:
        return await self._require_graph_search().paths(
            query,
            context=self._graph_context(access, "graph-path-search"),
        )

    async def search_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeSubgraphResult:
        return await self._require_graph_search().subgraph(
            query,
            context=self._graph_context(access, "graph-subgraph-search"),
        )

    def _require_graph_search(self) -> AuthoritativeGraphSearch:
        if self._graph_search is None:
            raise HarborCapabilityError("graph retrieval is not configured")
        return self._graph_search

    @staticmethod
    def _graph_context(
        access: AccessContext,
        operation_kind: str,
    ) -> StorageOperationContext:
        return StorageOperationContext.for_access(
            access,
            operation_kind=operation_kind,
            idempotency_key=f"graph-{uuid4().hex}",
        )
