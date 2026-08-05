"""Narrow SDK façades for ingestion and retrieval."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from harborrag_core.indexing import VectorFilter, VectorFilterCondition

from .contracts import (
    GraphNeighborhoodRequest,
    GraphNeighborhoodResponse,
    GraphPathRequest,
    GraphPathResponse,
    GraphSubgraphRequest,
    GraphSubgraphResponse,
    GraphTripletRequest,
    GraphTripletResponse,
    IngestionRequest,
    IngestionResult,
    IngestionStatus,
    IngestionTaskReference,
    RetrievalRequest,
    RetrievalResponse,
)

if TYPE_CHECKING:
    from .sdk import HarborRAG


class IngestionFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def run(self, request: IngestionRequest) -> IngestionResult:
        return await self._owner._ingestion_run(request)

    async def submit(self, request: IngestionRequest) -> IngestionTaskReference:
        return await self._owner._ingestion_submit(request)

    async def status(self, task_id: str) -> IngestionStatus:
        return await self._owner._ingestion_status(task_id)

    async def pause(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "pause")

    async def resume(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "resume")

    async def cancel(self, task_id: str) -> None:
        await self._owner._ingestion_control(task_id, "cancel")


class RetrievalFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def search(self, request: RetrievalRequest) -> RetrievalResponse:
        from .retrieval import RetrievalOptions

        service = await self._owner._retrieval_service()
        report = await service.retrieve(
            request.query,
            tenant_id=str(request.access.tenant_id),
            top_k=request.top_k,
            access=request.access,
            options=RetrievalOptions(
                lane=request.lane,
                filters=_build_vector_filter(request.filters),
                observe_graph=request.observe_graph,
            ),
        )
        return RetrievalResponse(
            request_id=report.request_id,
            lane=report.lane,
            results=report.results,
            diagnostics=asdict(report.diagnostics),
        )


class GraphFacade:
    def __init__(self, owner: HarborRAG) -> None:
        self._owner = owner

    async def search_triplets(
        self,
        request: GraphTripletRequest,
    ) -> GraphTripletResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_triplets(request.query, access=request.access)
        return GraphTripletResponse(
            triplets=result.triplets,
            diagnostics=asdict(result.diagnostics),
        )

    async def find_paths(self, request: GraphPathRequest) -> GraphPathResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_paths(request.query, access=request.access)
        return GraphPathResponse(
            paths=result.paths,
            diagnostics=asdict(result.diagnostics),
        )

    async def expand_subgraph(
        self,
        request: GraphSubgraphRequest,
    ) -> GraphSubgraphResponse:
        service = await self._owner._retrieval_service()
        result = await service.search_graph_subgraph(request.query, access=request.access)
        return GraphSubgraphResponse(
            nodes=result.graph.nodes,
            relations=result.graph.relations,
            diagnostics=asdict(result.diagnostics),
        )

    async def neighborhood(
        self,
        request: GraphNeighborhoodRequest,
    ) -> GraphNeighborhoodResponse:
        service = await self._owner._retrieval_service()
        seeds, result = await service.search_graph_neighborhood(
            request.query,
            access=request.access,
        )
        return GraphNeighborhoodResponse(
            seeds=seeds,
            nodes=result.graph.nodes,
            relations=result.graph.relations,
            diagnostics=asdict(result.diagnostics),
        )


def _build_vector_filter(filters: dict[str, object]) -> VectorFilter | None:
    if not filters:
        return None
    return VectorFilter(
        must=[
            VectorFilterCondition(field=name, value=value)
            for name, value in sorted(filters.items())
        ]
    )
