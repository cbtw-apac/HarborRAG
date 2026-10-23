"""Static graph schema discovery tool.

``describe_graph`` never executes a user query and never touches a tenant's data --
it only explains the graph contract that ``graph_triplet_search``, ``graph_path_search``,
and ``graph_subgraph_search`` already enforce. Keeping discovery and execution as
separate tools keeps both contracts predictable and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from harborrag_core.contracts.tools import ToolBehavior, ToolInvocationContext

from .base import BaseTool, ToolSpec
from .describe_graph_schema import OUTPUT_SCHEMA
from .graph_catalog import describe_graph_payload

_INPUT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


@dataclass(slots=True)
class DescribeGraphTool(BaseTool):
    """Static graph schema discovery; requires no tenant."""

    runtime: object | None = None
    spec = ToolSpec(
        "describe_graph",
        (
            "Statically describe HarborRAG's graph contract: structural, semantic, and "
            "ontology schema versions, the graph's node kinds and projected relation "
            "types, and the property catalog for common nodes, document-owned nodes, "
            "Chunk, Entity, and RELATES. Call this first, with no arguments, before using "
            "any other graph tool if you are not yet familiar with the graph model. This "
            "tool never executes a query and requires no tenant."
        ),
        _INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        behavior=ToolBehavior(read_only=True, idempotent=True),
    )

    async def call(
        self,
        arguments: dict[str, object],
        *,
        principal_id: str,
        context: ToolInvocationContext | None = None,
    ) -> dict[str, object]:
        if context is not None and principal_id != context.access.principal_id:
            raise PermissionError("tool principal does not match invocation context")
        del principal_id, arguments
        return describe_graph_payload()
