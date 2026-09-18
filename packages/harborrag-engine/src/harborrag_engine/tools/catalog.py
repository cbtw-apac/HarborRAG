"""Canonical shared reader-tool catalog and service binding."""

from __future__ import annotations

from typing import TYPE_CHECKING

from harborrag_engine.tools.references import KnowledgeReferenceStore

from .base import BaseTool
from .composed_search import ComposedEvidenceSearchTool
from .describe_graph import DescribeGraphTool
from .document_tools import GetDocumentMetadataTool, ListDocumentsTool, VerifyCitationsTool
from .graph_search import GraphPathSearchTool, GraphSubgraphSearchTool, GraphTripletSearchTool
from .reader_tools import FetchEvidenceTool, GetDocumentContextTool, ResolveGraphNodesTool
from .source_list_tool import ListSourcesTool
from .vector_search import VectorSearchTool

if TYPE_CHECKING:
    from harborrag_core.ports.reader import ReaderServices


def build_reader_tool_catalog(
    runtime: ReaderServices | None,
    references: KnowledgeReferenceStore,
) -> list[BaseTool]:
    """Build the canonical read tools in their stable discovery order."""

    retrieval = getattr(runtime, "retrieval", None)
    knowledge = getattr(runtime, "knowledge", None)
    graph = getattr(runtime, "graph", None)
    retrieval_runtime = runtime if retrieval is None else None
    knowledge_runtime = runtime if knowledge is None else None
    graph_runtime = runtime if graph is None else None
    return [
        VectorSearchTool(runtime=retrieval_runtime, reader=retrieval),
        FetchEvidenceTool(runtime=knowledge_runtime, knowledge=knowledge, references=references),
        GetDocumentContextTool(
            runtime=knowledge_runtime, knowledge=knowledge, references=references
        ),
        ListSourcesTool(runtime=knowledge_runtime, knowledge=knowledge, references=references),
        DescribeGraphTool(),
        GraphTripletSearchTool(runtime=graph_runtime, graph_reader=graph),
        GraphSubgraphSearchTool(runtime=graph_runtime, graph_reader=graph),
        GraphPathSearchTool(runtime=graph_runtime, graph_reader=graph),
        ResolveGraphNodesTool(
            runtime=knowledge_runtime, knowledge=knowledge, references=references
        ),
        ListDocumentsTool(runtime=knowledge_runtime, knowledge=knowledge, references=references),
        GetDocumentMetadataTool(
            runtime=knowledge_runtime, knowledge=knowledge, references=references
        ),
        VerifyCitationsTool(runtime=knowledge_runtime, knowledge=knowledge, references=references),
        ComposedEvidenceSearchTool(
            runtime=runtime if retrieval is None or knowledge is None else None,
            retrieval=retrieval,
            knowledge=knowledge,
            references=references,
        ),
    ]


__all__ = ["build_reader_tool_catalog"]
