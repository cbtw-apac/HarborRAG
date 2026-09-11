"""Narrow application-service dependency for graph screen routes."""

from __future__ import annotations

from typing import Annotated, Protocol, cast

from fastapi import Depends, Request

from harborrag_app.workflow_control.schemas import AppResponse
from harborrag_core.domain.graph_conflict import ConflictAction
from harborrag_core.retrieval import GraphSubgraphQuery


class GraphService(Protocol):
    async def projection_inventory(self, tenant: str) -> dict[str, object]: ...

    async def retrieve_graph_subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse: ...

    async def list_graph_conflicts(
        self,
        *,
        cursor: str | None,
        limit: int,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse: ...

    async def resolve_graph_conflict(
        self,
        conflict_id: str,
        *,
        action: ConflictAction,
        actor: str,
        tenant_ids: frozenset[str] | None,
    ) -> AppResponse: ...


def graph_service(request: Request) -> GraphService:
    return cast(GraphService, request.app.state.app_service)


GraphServiceDependency = Annotated[GraphService, Depends(graph_service)]
