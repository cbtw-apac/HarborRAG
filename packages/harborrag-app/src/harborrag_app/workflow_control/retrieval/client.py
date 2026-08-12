"""Retrieval portion of the application-service public facade.

Split out of composition/service.py (file-length gate); mixed into AppService
alongside AgentClientMixin/ChatClientMixin/ControlPlane*Mixin, following the
same thin-delegate pattern.
"""

from __future__ import annotations

from collections.abc import Mapping

from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.sdk import RetrievalLane

from ..composition.resources import AppResources
from ..schemas import AppResponse
from .graph import GraphRetrievalService
from .query import retrieve


class RetrievalClientMixin:
    _resources: AppResources
    _settings: RuntimeSettings
    _graph: GraphRetrievalService

    async def retrieve(  # noqa: PLR0913 - explicit retrieval policy is transport-neutral
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        principal_id: str = "harborrag-cli",
        top_k: int = 10,
        filters: Mapping[str, object] | None = None,
        lane: RetrievalLane = RetrievalLane.HYBRID,
        observe_graph: bool = False,
        include_content: bool = False,
        include_metadata: bool = False,
        score_threshold: float = 0.0,
    ) -> AppResponse:
        return await retrieve(
            self._resources.runtime_sdk,
            self._settings,
            query,
            tenant_id=tenant_id,
            principal_id=principal_id,
            top_k=top_k,
            filters=filters,
            lane=lane,
            observe_graph=observe_graph,
            include_content=include_content,
            include_metadata=include_metadata,
            score_threshold=score_threshold,
        )

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.triplets(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.paths(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.subgraph(query, tenant_id=tenant_id, principal_id=principal_id)

    async def retrieve_graph_neighborhood(
        self,
        query: GraphNeighborhoodQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._graph.neighborhood(query, tenant_id=tenant_id, principal_id=principal_id)
