"""Static graph schema and workflow discovery tool.

``describe_graph`` never executes a user query and never touches a tenant's data --
it only explains the graph contract that ``graph_triplet_search``, ``graph_path_search``,
and ``graph_subgraph_search`` already enforce. Keeping discovery and execution as
separate tools keeps both contracts predictable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from .base import BaseMcpTool, McpToolSpec
from .describe_graph_schema import OUTPUT_SCHEMA
from .graph_catalog import describe_graph_payload

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
        output_schema=OUTPUT_SCHEMA,
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
