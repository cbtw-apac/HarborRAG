"""Triplet, path, and subgraph retrieval tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from harborrag_core.chunking import RelationType
from harborrag_core.contracts.errors import HarborValidationError
from harborrag_core.retrieval import (
    GraphDirection,
    GraphPathQuery,
    GraphSubgraphQuery,
    GraphTripletQuery,
)
from harborrag_mcp_server.policy import McpToolPolicy
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

_MAX_RESULTS = McpToolPolicy().max_results
_RELATION_VALUES = [relation.value for relation in RelationType]
_DIRECTION_VALUES = [direction.value for direction in GraphDirection]


@dataclass(slots=True)
class GraphTripletSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_triplet_search",
        "Find active tenant-scoped subject-predicate-object graph records.",
        {
            "type": "object",
            "required": ["tenant_id"],
            "properties": {
                "tenant_id": TENANT_PROPERTY,
                "subject": {"type": "string", "minLength": 1},
                "predicate": {"type": "string", "enum": _RELATION_VALUES},
                "object": {"type": "string", "minLength": 1},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "default": 10,
                },
            },
            "anyOf": [
                {"required": ["subject"]},
                {"required": ["predicate"]},
                {"required": ["object"]},
            ],
            "additionalProperties": False,
        },
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
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "triplets": [item.model_dump(mode="json") for item in response.triplets],
            "diagnostics": response.diagnostics,
        }


@dataclass(slots=True)
class GraphPathSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_path_search",
        "Find active tenant-scoped paths between two graph nodes.",
        {
            "type": "object",
            "required": ["tenant_id", "start_node", "end_node"],
            "properties": {
                "tenant_id": TENANT_PROPERTY,
                "start_node": {"type": "string", "minLength": 1},
                "end_node": {"type": "string", "minLength": 1},
                "relationship_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": _RELATION_VALUES},
                    "uniqueItems": True,
                    "default": [],
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 4,
                },
                "max_paths": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "default": 10,
                },
                "direction": {
                    "type": "string",
                    "enum": _DIRECTION_VALUES,
                    "default": GraphDirection.OUTGOING.value,
                },
            },
            "additionalProperties": False,
        },
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
                relationship_types=tuple(
                    RelationType(item) for item in string_list(arguments, "relationship_types")
                ),
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
                direction=_direction(arguments, GraphDirection.OUTGOING),
            )
            request = GraphPathRequest(access=access(arguments, principal_id), query=query)
        except (HarborValidationError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        if self.runtime is None:
            return {"ok": False, "error": "graph retrieval backend is not configured"}
        try:
            response = await self.runtime.graph.find_paths(request)
        except Exception:
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "paths": [item.model_dump(mode="json") for item in response.paths],
            "diagnostics": response.diagnostics,
        }


@dataclass(slots=True)
class GraphSubgraphSearchTool(BaseMcpTool):
    runtime: HarborRAG | None = None
    spec = McpToolSpec(
        "graph_subgraph_search",
        "Expand an active tenant-scoped subgraph around one graph node.",
        {
            "type": "object",
            "required": ["tenant_id", "start_node"],
            "properties": {
                "tenant_id": TENANT_PROPERTY,
                "start_node": {"type": "string", "minLength": 1},
                "relationship_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": _RELATION_VALUES},
                    "uniqueItems": True,
                    "default": [],
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 8,
                    "default": 2,
                },
                "max_nodes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_RESULTS,
                    "default": _MAX_RESULTS,
                },
                "direction": {
                    "type": "string",
                    "enum": _DIRECTION_VALUES,
                    "default": GraphDirection.BOTH.value,
                },
            },
            "additionalProperties": False,
        },
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
                relationship_types=tuple(
                    RelationType(item) for item in string_list(arguments, "relationship_types")
                ),
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
            return {"ok": False, "error": "graph retrieval backend failed"}
        return {
            "ok": True,
            "nodes": [item.model_dump(mode="json") for item in response.nodes],
            "relations": [item.model_dump(mode="json") for item in response.relations],
            "diagnostics": response.diagnostics,
        }


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
