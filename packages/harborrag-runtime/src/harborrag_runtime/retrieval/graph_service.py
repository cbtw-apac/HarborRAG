"""Graph-facing operations mixed into the authoritative retrieval service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from harborrag_core.contracts.errors import HarborCapabilityError
from harborrag_core.ports.summary_projection import SummaryReaderPort
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

from .summary_views import apply_summary_views


class RuntimeGraphRetrievalMixin:
    """Expose bounded graph searches while sharing retrieval authorization context."""

    _graph_search: AuthoritativeGraphSearch | None
    _summaries: SummaryReaderPort | None = None

    @property
    def graph_retrieval_available(self) -> bool:
        """Whether this deployment configured a knowledge graph to read.

        Public because callers that *degrade* without the graph -- memory
        entity resolution, for one -- must be able to ask before calling
        rather than catch ``HarborCapabilityError`` as control flow.
        """

        return self._graph_search is not None

    async def search_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeTripletResult:
        async with asyncio.timeout(10):
            result = await self._require_graph_search().triplets(
                query,
                context=self._graph_context(access, "graph-triplet-search"),
            )
            nodes = await apply_summary_views(
                tuple(node for value in result.triplets for node in (value.subject, value.object)),
                self._summaries,
                access,
            )
            return replace(
                result,
                triplets=tuple(
                    value.model_copy(
                        update={"subject": nodes[index * 2], "object": nodes[index * 2 + 1]}
                    )
                    for index, value in enumerate(result.triplets)
                ),
            )

    async def search_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativePathResult:
        async with asyncio.timeout(10):
            result = await self._require_graph_search().paths(
                query,
                context=self._graph_context(access, "graph-path-search"),
            )
            nodes = await apply_summary_views(
                tuple(node for value in result.paths for node in value.nodes),
                self._summaries,
                access,
            )
            by_key = {node.node_key: node for node in nodes}
            return replace(
                result,
                paths=tuple(
                    value.model_copy(
                        update={"nodes": tuple(by_key[node.node_key] for node in value.nodes)}
                    )
                    for value in result.paths
                ),
            )

    async def search_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        access: AccessContext,
    ) -> AuthoritativeSubgraphResult:
        async with asyncio.timeout(10):
            result = await self._require_graph_search().subgraph(
                query,
                context=self._graph_context(access, "graph-subgraph-search"),
            )
            nodes = await apply_summary_views(result.graph.nodes, self._summaries, access)
            return replace(result, graph=result.graph.model_copy(update={"nodes": nodes}))

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
