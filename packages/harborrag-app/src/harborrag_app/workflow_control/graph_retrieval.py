"""Graph retrieval use cases sharing one access, envelope, and failure path."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic_core import to_jsonable_python

from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_core.schemas.ids import TenantId
from harborrag_core.security import AccessContext
from harborrag_runtime.sdk import (
    GraphNeighborhoodRequest,
    GraphPathRequest,
    GraphSubgraphRequest,
    GraphTripletRequest,
    HarborRAG,
)

from .errors import failure_response
from .schemas import AppResponse

logger = logging.getLogger("harborrag.app.workflow_control.graph")


class _GraphResponse(Protocol):
    # Read-only: the concrete responses are frozen dataclasses, which cannot satisfy a
    # mutable protocol attribute.
    @property
    def diagnostics(self) -> dict[str, object]: ...


type RuntimeProvider = Callable[[], HarborRAG]


class GraphRetrievalService:
    """Answer canonical graph queries behind one stable application envelope."""

    def __init__(self, runtime: RuntimeProvider) -> None:
        self._runtime = runtime

    async def triplets(
        self,
        query: GraphTripletQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._query(
            label="triplets",
            tenant_id=tenant_id,
            operation=lambda access: self._runtime().graph.search_triplets(
                GraphTripletRequest(access=access, query=query)
            ),
            payload=lambda response: {"triplets": to_jsonable_python(response.triplets)},
            principal_id=principal_id,
        )

    async def paths(
        self,
        query: GraphPathQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._query(
            label="paths",
            tenant_id=tenant_id,
            operation=lambda access: self._runtime().graph.find_paths(
                GraphPathRequest(access=access, query=query)
            ),
            payload=lambda response: {"paths": to_jsonable_python(response.paths)},
            principal_id=principal_id,
        )

    async def subgraph(
        self,
        query: GraphSubgraphQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._query(
            label="subgraph",
            tenant_id=tenant_id,
            operation=lambda access: self._runtime().graph.expand_subgraph(
                GraphSubgraphRequest(access=access, query=query)
            ),
            payload=lambda response: {
                "nodes": to_jsonable_python(response.nodes),
                "relations": to_jsonable_python(response.relations),
            },
            principal_id=principal_id,
        )

    async def neighborhood(
        self,
        query: GraphNeighborhoodQuery,
        *,
        tenant_id: str,
        principal_id: str,
    ) -> AppResponse:
        return await self._query(
            label="neighborhood",
            tenant_id=tenant_id,
            operation=lambda access: self._runtime().graph.neighborhood(
                GraphNeighborhoodRequest(access=access, query=query)
            ),
            payload=lambda response: {
                "seeds": list(response.seeds),
                "nodes": to_jsonable_python(response.nodes),
                "relations": to_jsonable_python(response.relations),
            },
            principal_id=principal_id,
        )

    async def _query[T: _GraphResponse](
        self,
        *,
        label: str,
        tenant_id: str,
        principal_id: str,
        operation: Callable[[AccessContext], Awaitable[T]],
        payload: Callable[[T], dict[str, object]],
    ) -> AppResponse:
        access = AccessContext(principal_id=principal_id, tenant_id=TenantId(tenant_id))
        try:
            response = await operation(access)
        except Exception as exc:  # noqa: BLE001 - stable application envelope
            # Plain %-args, not an f-string: the format string must stay a literal so a
            # label can never be interpreted as a format spec.
            return failure_response(
                logger, exc, "retrieve graph %s for tenant %r", label, tenant_id
            )
        return AppResponse(
            True,
            {**payload(response), "diagnostics": to_jsonable_python(response.diagnostics)},
        )


__all__ = ["GraphRetrievalService"]
