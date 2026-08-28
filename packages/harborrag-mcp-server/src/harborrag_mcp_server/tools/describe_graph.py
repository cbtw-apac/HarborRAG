"""Static graph schema and workflow discovery tool.

``describe_graph`` never executes a user query and never touches a tenant's data --
it only explains the graph contract that ``graph_triplet_search``, ``graph_path_search``,
and ``graph_subgraph_search`` already enforce. Keeping discovery and execution as
separate tools keeps both contracts predictable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import BaseMcpTool, McpToolSpec
from .graph_catalog import describe_graph_payload

_OUTPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": [
        "ok",
        "graph_schema_version",
        "capabilities",
        "selector_rules",
        "node_kinds",
        "entity_types",
        "relation_types",
        "direction_semantics",
        "topologies",
        "workflows",
        "defaults",
        "limits",
    ],
    "properties": {
        "ok": {"type": "boolean"},
        "graph_schema_version": {"type": "string"},
        "capabilities": {"type": "object"},
        "selector_rules": {"type": "object"},
        "node_kinds": {"type": "array"},
        "entity_types": {"type": "array"},
        "relation_types": {"type": "array"},
        "direction_semantics": {"type": "object"},
        "topologies": {"type": "array"},
        "workflows": {"type": "array"},
        "defaults": {"type": "object"},
        "limits": {"type": "object"},
    },
}

_ANNOTATIONS: dict[str, object] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


@dataclass(slots=True)
class DescribeGraphTool(BaseMcpTool):
    """Static graph capability and schema discovery; requires no tenant."""

    runtime: object | None = None
    spec = McpToolSpec(
        "describe_graph",
        (
            "Statically describe HarborRAG's graph contract: node kinds, entity types, "
            "projected relations, selector rules, connector topologies, and recommended "
            "workflows. Call this first when graph selectors, relations, directions, or "
            "topology are unclear. This tool never executes a query and requires no tenant."
        ),
        {"type": "object", "additionalProperties": False},
        output_schema=_OUTPUT_SCHEMA,
        annotations=_ANNOTATIONS,
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        del arguments, principal_id
        return describe_graph_payload()
