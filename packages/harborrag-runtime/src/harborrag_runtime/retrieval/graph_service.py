"""Graph-facing operations mixed into the authoritative retrieval service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast
from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
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
    RetrievalLane,
)

from .contracts import RetrievalOptions, RuntimeRetrievalReport

if TYPE_CHECKING:

    class _RetrievalOwner(Protocol):
        async def retrieve(
            self,
            query: str,
            *,
            tenant_id: str,
            top_k: int = 10,
            options: RetrievalOptions | None = None,
            access: AccessContext | None = None,
        ) -> RuntimeRetrievalReport: ...


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

    async def search_graph_neighborhood(
        self,
        query: GraphNeighborhoodQuery,
        *,
        access: AccessContext,
    ) -> tuple[tuple[str, ...], AuthoritativeSubgraphResult]:
        """Resolve vector seeds from free text and expand their graph neighborhood."""

        graph_search = self._require_graph_search()
        report = await cast("_RetrievalOwner", self).retrieve(
            query.query,
            tenant_id=str(access.tenant_id),
            top_k=query.seed_limit,
            access=access,
            options=RetrievalOptions(lane=RetrievalLane.HYBRID, observe_graph=False),
        )
        seeds = tuple(dict.fromkeys(result.id for result in report.results))
        return seeds, await graph_search.neighborhood(
            seeds,
            query,
            context=self._graph_context(access, "graph-neighborhood-search"),
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
