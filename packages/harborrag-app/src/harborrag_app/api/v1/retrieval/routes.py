"""Authenticated vector and graph retrieval through the runtime facade."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborCapabilityError, HarborConnectionError
from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)

from .dependencies import RetrievalServiceDependency
from .schemas import (
    GraphNeighborhoodSearchRequest,
    GraphNeighborhoodSearchResponse,
    GraphPathSearchRequest,
    GraphPathSearchResponse,
    GraphSubgraphSearchRequest,
    GraphSubgraphSearchResponse,
    GraphTripletSearchRequest,
    GraphTripletSearchResponse,
    VectorSearchRequest,
    VectorSearchResponse,
)

router = APIRouter(prefix="/retrieval", tags=["Retrieval"])

ERROR_RESPONSES = documented_error_responses(
    {
        501: "Requested retrieval capability is not configured",
        422: "Invalid retrieval request",
        503: "Retrieval service unavailable",
    }
)


@router.post(
    "/vector",
    response_model=VectorSearchResponse,
    response_model_exclude_none=True,
    responses=ERROR_RESPONSES,
)
async def vector_search(
    request: VectorSearchRequest,
    service: RetrievalServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> VectorSearchResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.retrieve(
        request.query,
        tenant_id=request.tenant,
        principal_id=principal.subject,
        top_k=request.top_k,
        filters=request.filters,
        lane=request.lane,
        observe_graph=request.observe_graph,
        include_content=request.include_content,
        include_metadata=request.include_metadata,
        score_threshold=request.score_threshold,
    )
    return VectorSearchResponse.model_validate(_response_data(response, capability="Retrieval"))


@router.post(
    "/graph/triplets",
    response_model=GraphTripletSearchResponse,
    responses=ERROR_RESPONSES,
)
async def graph_triplet_search(
    request: GraphTripletSearchRequest,
    service: RetrievalServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> GraphTripletSearchResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.retrieve_graph_triplets(
        GraphTripletQuery(
            subject=request.subject,
            predicate=request.predicate,
            object=request.object,
            limit=request.limit,
        ),
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    return GraphTripletSearchResponse.model_validate(
        _response_data(response, capability="Graph triplet retrieval")
    )


@router.post(
    "/graph/paths",
    response_model=GraphPathSearchResponse,
    responses=ERROR_RESPONSES,
)
async def graph_path_search(
    request: GraphPathSearchRequest,
    service: RetrievalServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> GraphPathSearchResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.retrieve_graph_paths(
        GraphPathQuery(
            start_node=request.start_node,
            end_node=request.end_node,
            relationship_types=tuple(request.relationship_types),
            max_depth=request.max_depth,
            max_paths=request.max_paths,
            direction=request.direction,
        ),
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    return GraphPathSearchResponse.model_validate(
        _response_data(response, capability="Graph path retrieval")
    )


@router.post(
    "/graph/subgraphs",
    response_model=GraphSubgraphSearchResponse,
    responses=ERROR_RESPONSES,
)
async def graph_subgraph_search(
    request: GraphSubgraphSearchRequest,
    service: RetrievalServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> GraphSubgraphSearchResponse:
    authorize_tenant(principal, request.tenant)
    response = await service.retrieve_graph_subgraph(
        GraphSubgraphQuery(
            start_node=request.start_node,
            relationship_types=tuple(request.relationship_types),
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            direction=request.direction,
        ),
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    return GraphSubgraphSearchResponse.model_validate(
        _response_data(response, capability="Graph subgraph retrieval")
    )


@router.post(
    "/graph/neighborhoods",
    response_model=GraphNeighborhoodSearchResponse,
    responses=ERROR_RESPONSES,
)
async def graph_neighborhood_search(
    request: GraphNeighborhoodSearchRequest,
    service: RetrievalServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> GraphNeighborhoodSearchResponse:
    """Expand the graph around a question, without requiring a node selector."""

    authorize_tenant(principal, request.tenant)
    response = await service.retrieve_graph_neighborhood(
        GraphNeighborhoodQuery(
            query=request.query,
            seed_limit=request.seed_limit,
            relationship_types=tuple(request.relationship_types),
            max_depth=request.max_depth,
            max_nodes=request.max_nodes,
            direction=request.direction,
        ),
        tenant_id=request.tenant,
        principal_id=principal.subject,
    )
    return GraphNeighborhoodSearchResponse.model_validate(
        _response_data(response, capability="Graph neighborhood retrieval")
    )


def _response_data(response: AppResponse, *, capability: str) -> dict[str, object]:
    if not response.ok:
        if response.data.get("error_type") == "HarborCapabilityError":
            raise HarborCapabilityError(f"{capability} is not configured")
        raise HarborConnectionError(f"{capability} service is unavailable")
    return response.data
