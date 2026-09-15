"""Compatibility exports for canonical shared agent and MCP tool definitions."""

from harborrag_runtime.tools.base import ToolSpec
from harborrag_runtime.tools.catalog_factory import build_reader_tool_catalog
from harborrag_runtime.tools.references import KnowledgeReferenceStore
from harborrag_runtime.tools.retrieval_schemas import (
    GRAPH_PATH_DESCRIPTION as GRAPH_PATH_DESCRIPTION,
)
from harborrag_runtime.tools.retrieval_schemas import (
    GRAPH_SUBGRAPH_DESCRIPTION as GRAPH_SUBGRAPH_DESCRIPTION,
)
from harborrag_runtime.tools.retrieval_schemas import (
    GRAPH_TRIPLET_DESCRIPTION as GRAPH_TRIPLET_DESCRIPTION,
)
from harborrag_runtime.tools.retrieval_schemas import (
    VECTOR_SEARCH_DESCRIPTION as VECTOR_SEARCH_DESCRIPTION,
)
from harborrag_runtime.tools.retrieval_schemas import (
    graph_path_schema as graph_path_schema,
)
from harborrag_runtime.tools.retrieval_schemas import (
    graph_subgraph_schema as graph_subgraph_schema,
)
from harborrag_runtime.tools.retrieval_schemas import (
    graph_triplet_schema as graph_triplet_schema,
)
from harborrag_runtime.tools.retrieval_schemas import (
    vector_search_schema as vector_search_schema,
)

RuntimeAgentToolSpec = ToolSpec

RUNTIME_AGENT_TOOL_SPECS = tuple(
    tool.spec for tool in build_reader_tool_catalog(None, KnowledgeReferenceStore())
)
