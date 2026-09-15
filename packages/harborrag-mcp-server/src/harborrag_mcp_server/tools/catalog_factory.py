"""Factory for the exact supported MCP reader catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_mcp_server.references import KnowledgeReferenceStore

from .base import BaseMcpTool
from .describe_graph import DescribeGraphTool
from .graph_search import GraphPathSearchTool, GraphSubgraphSearchTool, GraphTripletSearchTool
from .reader_tools import FetchEvidenceTool, GetDocumentContextTool, ResolveGraphNodesTool
from .source_list_tool import ListSourcesTool
from .vector_search import VectorSearchTool

if TYPE_CHECKING:
    from harborrag_runtime.sdk import HarborRAG


def build_reader_tool_catalog(
    runtime: HarborRAG | None,
    references: KnowledgeReferenceStore,
) -> list[BaseMcpTool]:
    """Build the nine tools in their stable discovery order."""

    return [
        VectorSearchTool(runtime=runtime),
        FetchEvidenceTool(runtime=runtime, references=references),
        GetDocumentContextTool(runtime=runtime, references=references),
        ListSourcesTool(runtime=runtime, references=references),
        DescribeGraphTool(runtime=runtime),
        GraphTripletSearchTool(runtime=runtime),
        GraphSubgraphSearchTool(runtime=runtime),
        GraphPathSearchTool(runtime=runtime),
        ResolveGraphNodesTool(runtime=runtime, references=references),
    ]


__all__ = ["build_reader_tool_catalog"]
