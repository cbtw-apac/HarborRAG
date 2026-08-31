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
from .graph_catalog import EXECUTABLE_TOOL_NAMES, describe_graph_payload

_ANNOTATIONS: dict[str, object] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "for_tool": {
            "type": "string",
            "enum": list(EXECUTABLE_TOOL_NAMES),
            "description": (
                "Omit on your first call in a session -- you need the full graph "
                "contract to decide which search tool applies. Pass this only once "
                "you've already picked a tool, to get just its argument contract "
                "(defaults and the entity/relation/direction values it accepts)."
            ),
        }
    },
    "additionalProperties": False,
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
            "workflows. Call this first, with no arguments, when graph selectors, "
            "relations, directions, or topology are unclear. Once you've already picked "
            "a search tool, call again with for_tool set to just that tool's argument "
            "contract. This tool never executes a query and requires no tenant."
        ),
        _INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        annotations=_ANNOTATIONS,
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
    ) -> dict[str, object]:
        del principal_id
        for_tool = arguments.get("for_tool")
        return describe_graph_payload(for_tool if isinstance(for_tool, str) else None)
