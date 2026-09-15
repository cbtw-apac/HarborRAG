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
from .graph_search_support import (
    COMPLETION_SCHEMA,
    completion,
    direction,
)
from .graph_search_support import (
    relations as parse_relations,
)
from .output_schemas import (
    GRAPH_SEARCH_DIAGNOSTICS_SCHEMA,
    NODE_SCHEMA,
    PATH_SCHEMA,
    RELATION_SCHEMA,
    TRIPLET_SCHEMA,
)
from .retrieval_inputs import (
    TENANT_PROPERTY,
    access,
    integer,
    optional_text,
    success_or_failure_schema,
    text,
)

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG

logger = logging.getLogger("harborrag.mcp.tools.graph_search")
_MAX_RESULTS = McpToolPolicy().max_results
_MAX_PATHS = 5
_MAX_PILOT_DEPTH = 4
_MAX_SUBGRAPH_EDGES = 40
_ANNOTATIONS: dict[str, object] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@dataclass(slots=True)
class GraphTripletSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_triplet_search",
        GRAPH_TRIPLET_DESCRIPTION,
        graph_triplet_schema(max_results=_MAX_RESULTS, tenant=TENANT_PROPERTY),
        output_schema=success_or_failure_schema(
            {
                "type": "object",
                "required": ["ok", "outcome", "triplets", "diagnostics", "completion"],
                "properties": {
                    "ok": {"const": True},
                    "outcome": {
                        "type": "string",
                        "enum": ["matched", "no_match_within_bounds"],
                    },
                    "triplets": {
                        "type": "array",
                        "items": TRIPLET_SCHEMA,
                        "maxItems": _MAX_RESULTS,
                    },
                    "diagnostics": GRAPH_SEARCH_DIAGNOSTICS_SCHEMA,
                    "completion": COMPLETION_SCHEMA,
                },
                "additionalProperties": False,
            }
        ),
        annotations=_ANNOTATIONS,
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
        diagnostics = response.diagnostics
        return {
            "ok": True,
            "outcome": "matched" if response.triplets else "no_match_within_bounds",
            "triplets": [compact_triplet(item) for item in response.triplets],
            "diagnostics": diagnostics,
            "completion": completion(diagnostics),
        }


@dataclass(slots=True)
class GraphPathSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_path_search",
        GRAPH_PATH_DESCRIPTION,
        graph_path_schema(
            max_results=_MAX_PATHS,
            max_depth=_MAX_PILOT_DEPTH,
            tenant=TENANT_PROPERTY,
        ),
        output_schema=success_or_failure_schema(
            {
                "type": "object",
                "required": ["ok", "outcome", "paths", "diagnostics", "completion"],
                "properties": {
                    "ok": {"const": True},
                    "outcome": {
                        "type": "string",
                        "enum": ["matched", "no_path_within_bounds"],
                    },
                    "paths": {
                        "type": "array",
                        "items": PATH_SCHEMA,
                        "maxItems": _MAX_PATHS,
                    },
                    "diagnostics": GRAPH_SEARCH_DIAGNOSTICS_SCHEMA,
                    "completion": COMPLETION_SCHEMA,
                },
                "additionalProperties": False,
            }
        ),
        annotations=_ANNOTATIONS,
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
                relationship_types=parse_relations(arguments),
                max_depth=integer(
                    arguments,
                    "max_depth",
                    4,
                    minimum=1,
                    maximum=_MAX_PILOT_DEPTH,
                ),
                max_paths=integer(
                    arguments,
                    "max_paths",
                    _MAX_PATHS,
                    minimum=1,
                    maximum=_MAX_PATHS,
                ),
                direction=direction(arguments, GraphDirection.BOTH),
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
        diagnostics = response.diagnostics
        return {
            "ok": True,
            "outcome": "matched" if response.paths else "no_path_within_bounds",
            "paths": [compact_path(item) for item in response.paths],
            "diagnostics": diagnostics,
            "completion": completion(diagnostics),
        }


@dataclass(slots=True)
class GraphSubgraphSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_subgraph_search",
        GRAPH_SUBGRAPH_DESCRIPTION,
        graph_subgraph_schema(
            max_results=_MAX_RESULTS,
            max_depth=_MAX_PILOT_DEPTH,
            tenant=TENANT_PROPERTY,
        ),
        output_schema=success_or_failure_schema(
            {
                "type": "object",
                "required": [
                    "ok",
                    "outcome",
                    "nodes",
                    "relations",
                    "diagnostics",
                    "completion",
                ],
                "properties": {
                    "ok": {"const": True},
                    "outcome": {
                        "type": "string",
                        "enum": ["matched", "no_match_within_bounds"],
                    },
                    "nodes": {
                        "type": "array",
                        "items": NODE_SCHEMA,
                        "maxItems": _MAX_RESULTS,
                    },
                    "relations": {
                        "type": "array",
                        "items": RELATION_SCHEMA,
                        "maxItems": _MAX_SUBGRAPH_EDGES,
                    },
                    "diagnostics": GRAPH_SEARCH_DIAGNOSTICS_SCHEMA,
                    "completion": COMPLETION_SCHEMA,
                },
                "additionalProperties": False,
            }
        ),
        annotations=_ANNOTATIONS,
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
                relationship_types=parse_relations(arguments),
                max_depth=integer(
                    arguments,
                    "max_depth",
                    2,
                    minimum=1,
                    maximum=_MAX_PILOT_DEPTH,
                ),
                max_nodes=integer(
                    arguments,
                    "max_nodes",
                    _MAX_RESULTS,
                    minimum=1,
                    maximum=_MAX_RESULTS,
                ),
                direction=direction(arguments, GraphDirection.BOTH),
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
        relations = [compact_relation(item) for item in response.relations]
        edge_truncated = len(relations) > _MAX_SUBGRAPH_EDGES
        diagnostics = dict(response.diagnostics)
        diagnostics["projection_truncated"] = bool(
            diagnostics.get("projection_truncated") or edge_truncated
        )
        return {
            "ok": True,
            "outcome": "matched" if response.nodes else "no_match_within_bounds",
            "nodes": [compact_node(item) for item in response.nodes],
            "relations": relations[:_MAX_SUBGRAPH_EDGES],
            "diagnostics": diagnostics,
            "completion": completion(
                diagnostics,
                extra_reasons=("edge_limit",) if edge_truncated else (),
            ),
        }
