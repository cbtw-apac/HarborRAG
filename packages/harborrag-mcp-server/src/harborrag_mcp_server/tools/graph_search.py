"""Triplet, path, and subgraph retrieval tools.

Schemas come from ``harborrag_runtime.agent.tool_specs`` so the MCP surface and the
in-process agent surface cannot drift; only the policy bounds and the tenant property
description are applied here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.retrieval import (
    GraphDirection,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
    compact_node,
    compact_path,
    compact_relation,
    compact_triplet,
)
from harborrag_mcp_server.policy import McpToolPolicy
from harborrag_runtime.agent.tool_specs import (
    GRAPH_PATH_DESCRIPTION,
    GRAPH_SUBGRAPH_DESCRIPTION,
    GRAPH_TRIPLET_DESCRIPTION,
    graph_path_schema,
    graph_subgraph_schema,
    graph_triplet_schema,
)
from harborrag_runtime.contracts import (
    GraphPathRequest,
    GraphSubgraphRequest,
    GraphTripletRequest,
)

from .base import BaseMcpTool, McpToolSpec
from .retrieval_inputs import (
    TENANT_PROPERTY,
    access,
    integer,
    optional_text,
    string_list,
    text,
)

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.mcp.tools.graph_search")
_MAX_RESULTS = McpToolPolicy().max_results


@dataclass(slots=True)
class GraphTripletSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_triplet_search",
        GRAPH_TRIPLET_DESCRIPTION,
        graph_triplet_schema(max_results=_MAX_RESULTS, tenant=TENANT_PROPERTY),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            predicate_value = optional_text(arguments, "predicate")
            request = GraphTripletRequest(
                access=access(arguments, principal_id),
                query=GraphTripletQuery(
                    subject=optional_text(arguments, "subject"),
                    predicate=(
                        RelationType(predicate_value) if predicate_value is not None else None
                    ),
                    object=optional_text(arguments, "object"),
                    limit=integer(
                        arguments,
                        "limit",
                        10,
                        minimum=1,
                        maximum=_MAX_RESULTS,
                    ),
                ),
            )
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if self.runtime is None:
            return {"ok": False, "error": "graph retrieval backend is not configured"}
        try:
            response = await self.runtime.graph.search_triplets(request)
        except Exception:
            logger.exception("graph_triplet_search backend raised during call")
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "triplets": [compact_triplet(item) for item in response.triplets],
            "diagnostics": response.diagnostics,
        }


@dataclass(slots=True)
class GraphPathSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_path_search",
        GRAPH_PATH_DESCRIPTION,
        graph_path_schema(max_results=_MAX_RESULTS, tenant=TENANT_PROPERTY),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            query = GraphPathQuery(
                start_node=text(arguments, "start_node"),
                end_node=text(arguments, "end_node"),
                relationship_types=_relations(arguments),
                max_depth=integer(
                    arguments,
                    "max_depth",
                    4,
                    minimum=1,
                    maximum=8,
                ),
                max_paths=integer(
                    arguments,
                    "max_paths",
                    10,
                    minimum=1,
                    maximum=_MAX_RESULTS,
                ),
                direction=_direction(arguments, GraphDirection.BOTH),
            )
            request = GraphPathRequest(access=access(arguments, principal_id), query=query)
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if self.runtime is None:
            return {"ok": False, "error": "graph retrieval backend is not configured"}
        try:
            response = await self.runtime.graph.find_paths(request)
        except Exception:
            logger.exception("graph_path_search backend raised during call")
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "paths": [compact_path(item) for item in response.paths],
            "diagnostics": response.diagnostics,
        }


@dataclass(slots=True)
class GraphSubgraphSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_subgraph_search",
        GRAPH_SUBGRAPH_DESCRIPTION,
        graph_subgraph_schema(max_results=_MAX_RESULTS, tenant=TENANT_PROPERTY),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        try:
            query = GraphSubgraphQuery(
                start_node=text(arguments, "start_node"),
                relationship_types=_relations(arguments),
                max_depth=integer(
                    arguments,
                    "max_depth",
                    2,
                    minimum=1,
                    maximum=8,
                ),
                max_nodes=integer(
                    arguments,
                    "max_nodes",
                    _MAX_RESULTS,
                    minimum=1,
                    maximum=_MAX_RESULTS,
                ),
                direction=_direction(arguments, GraphDirection.BOTH),
            )
            request = GraphSubgraphRequest(
                access=access(arguments, principal_id),
                query=query,
            )
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if self.runtime is None:
            return {"ok": False, "error": "graph retrieval backend is not configured"}
        try:
            response = await self.runtime.graph.expand_subgraph(request)
        except Exception:
            logger.exception("graph_subgraph_search backend raised during call")
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "nodes": [compact_node(item) for item in response.nodes],
            "relations": [compact_relation(item) for item in response.relations],
            "diagnostics": response.diagnostics,
        }


def _relations(arguments: dict[str, object]) -> tuple[RelationType, ...]:
    return tuple(RelationType(item) for item in string_list(arguments, "relationship_types"))


def _direction(
    arguments: dict[str, object],
    default: GraphDirection,
) -> GraphDirection:
    value = arguments.get("direction", default.value)
    if not isinstance(value, str):
        raise HarborValidationError("direction must be incoming, outgoing, or both")
    try:
        return GraphDirection(value)
    except ValueError:
        raise HarborValidationError("direction must be incoming, outgoing, or both") from None
