"""Graph screen endpoints: overview, bounded traversal, and the conflict queue."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from harborrag_app.api.auth.dependencies import authorize_tenant, require_role
from harborrag_app.api.auth.principal import Principal
from harborrag_app.api.capacity_dependency import require_api_capacity
from harborrag_app.api.errors import documented_error_responses
from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.contracts.errors import HarborCapabilityError, HarborConnectionError
from harborrag_core.retrieval import GraphSubgraphQuery

from .dependencies import GraphServiceDependency
from .schemas import (
    GraphConflictListResponse,
    GraphConflictResolveRequest,
    GraphConflictResponse,
    GraphOverviewResponse,
    GraphTraverseRequest,
    GraphTraverseResponse,
)

router = APIRouter(
    prefix="/graph",
    tags=["Graph"],
    dependencies=[Depends(require_api_capacity)],
)

ERROR_RESPONSES = documented_error_responses(
    {
        501: "Graph capability is not configured",
        422: "Invalid graph request",
        503: "Graph service unavailable",
    }
)

CONFLICT_ERROR_RESPONSES = documented_error_responses(
    {
        404: "Graph conflict not found",
        409: "Graph conflict is already resolved",
        422: "Invalid graph conflict request",
    }
)

TenantQuery = Annotated[
    str,
    Query(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
        description="Tenant projection namespace.",
    ),
]


@router.get(
    "/overview",
    response_model=GraphOverviewResponse,
    responses=ERROR_RESPONSES,
)
async def graph_overview(
    service: GraphServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    tenant: TenantQuery = "DEFAULT",
) -> GraphOverviewResponse:
    authorize_tenant(principal, tenant)
    inventory = await service.projection_inventory(tenant)
    return GraphOverviewResponse.model_validate(
        {
            "tenant": inventory["tenant"],
            "graph_name": inventory["graph_name"],
            "node_count": inventory["graph_nodes"],
            "relation_count": inventory["graph_relations"],
        }
    )


@router.post(
    "/traverse",
    response_model=GraphTraverseResponse,
    responses=ERROR_RESPONSES,
)
async def graph_traverse(
    request: GraphTraverseRequest,
    service: GraphServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
) -> GraphTraverseResponse:
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
    return GraphTraverseResponse.model_validate(
        _response_data(response, capability="Graph traversal")
    )


@router.get(
    "/conflicts",
    response_model=GraphConflictListResponse,
    responses=CONFLICT_ERROR_RESPONSES,
)
async def list_graph_conflicts(
    service: GraphServiceDependency,
    principal: Annotated[Principal, Depends(require_role("reader"))],
    cursor: Annotated[
        str | None, Query(description="Opaque page cursor from a prior page.")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> GraphConflictListResponse:
    response = await service.list_graph_conflicts(
        cursor=cursor, limit=limit, tenant_ids=principal.tenant_scope
    )
    return GraphConflictListResponse(
        conflicts=[GraphConflictResponse.from_domain(c) for c in response.data["conflicts"]],
        next_cursor=response.data["next_cursor"],
    )


@router.post(
    "/conflicts/{conflict_id}/resolve",
    response_model=GraphConflictResponse,
    responses=CONFLICT_ERROR_RESPONSES,
)
async def resolve_graph_conflict(
    conflict_id: str,
    request: GraphConflictResolveRequest,
    service: GraphServiceDependency,
    principal: Annotated[Principal, Depends(require_role("editor"))],
) -> GraphConflictResponse:
    response = await service.resolve_graph_conflict(
        conflict_id,
        action=request.action,
        actor=principal.subject,
        tenant_ids=principal.tenant_scope,
    )
    return GraphConflictResponse.from_domain(response.data["conflict"])


def _response_data(response: AppResponse, *, capability: str) -> dict[str, object]:
    if not response.ok:
        if response.data.get("error_type") == "HarborCapabilityError":
            raise HarborCapabilityError(f"{capability} is not configured")
        raise HarborConnectionError(f"{capability} service is unavailable")
    return response.data
