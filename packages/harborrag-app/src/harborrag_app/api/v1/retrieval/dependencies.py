"""Application-service dependency for retrieval routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.retrieval import GraphPathQuery, GraphSubgraphQuery, GraphTripletQuery
from harborrag_runtime.sdk import RetrievalLane


class RetrievalService(Protocol):
    async def retrieve(  # noqa: PLR0913 - mirrors the transport-neutral facade
        self,
        query: str,
        *,
        tenant_id: str | None = None,
        principal_id: str,
        top_k: int,
        filters: Mapping[str, object] | None,
        lane: RetrievalLane,
        observe_graph: bool,
        include_content: bool,
        include_metadata: bool,
        score_threshold: float,
    ) -> AppResponse: ...

    async def retrieve_graph_triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...

    async def retrieve_graph_paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...


def retrieval_service(request: Request) -> RetrievalService:
    return cast(RetrievalService, request.app.state.app_service)


RetrievalServiceDependency = Annotated[RetrievalService, Depends(retrieval_service)]
