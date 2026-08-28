"""Static graph contract catalog backing the ``describe_graph`` tool.

Every mapping here is keyed by a canonical enum member rather than a free-standing
string, so a completeness test can diff the mapping's keys against the enum's members
and fail when a new node kind, entity type, or projected relation ships without
documentation. Reserved (non-projected) relation types are intentionally absent: the
graph projector never emits them, so advertising them as selectable would return an
empty result indistinguishable from a genuine miss.
"""

from __future__ import annotations

from typing import Any

from harborrag_core.chunking import PROJECTED_RELATION_TYPES, RelationType
from harborrag_core.ingestion import GraphEntityType, KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import GRAPH_SCHEMA_VERSION
from harborrag_core.retrieval import GraphDirection
from harborrag_mcp_server.policy import McpToolPolicy

from .base import McpToolSpec
from .graph_search import GraphPathSearchTool, GraphSubgraphSearchTool, GraphTripletSearchTool
from .vector_search import VectorSearchTool


def _tool_defaults(spec: McpToolSpec) -> dict[str, Any]:
    """Pull each property's compiled-in ``default`` straight from the tool's own schema.

    Reading the live spec (rather than restating literals here) means describe_graph can
    never drift from what the tool actually advertises: a schema change is reflected the
    next time this runs, with nothing to keep in sync by hand.
    """
    properties = spec.input_schema.get("properties", {})
    if not isinstance(properties, dict):
        return {}
    return {
        name: property_schema["default"]
        for name, property_schema in properties.items()
        if isinstance(property_schema, dict) and "default" in property_schema
    }


def _schema_maximum(spec: McpToolSpec, property_name: str) -> int:
    properties = spec.input_schema["properties"]
    value = properties[property_name]["maximum"]
    assert isinstance(value, int)
    return value


TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "vector_search": _tool_defaults(VectorSearchTool.spec),
    "graph_triplet_search": _tool_defaults(GraphTripletSearchTool.spec),
    "graph_path_search": _tool_defaults(GraphPathSearchTool.spec),
    "graph_subgraph_search": _tool_defaults(GraphSubgraphSearchTool.spec),
}

# Both graph_path_search and graph_subgraph_search cap max_depth at the same compiled-in
# ceiling; MAXIMUM_DEPTH asserts that rather than restating either literal.
_PATH_MAX_DEPTH = _schema_maximum(GraphPathSearchTool.spec, "max_depth")
_SUBGRAPH_MAX_DEPTH = _schema_maximum(GraphSubgraphSearchTool.spec, "max_depth")
assert _PATH_MAX_DEPTH == _SUBGRAPH_MAX_DEPTH, (
    "graph_path_search and graph_subgraph_search must share one max_depth ceiling"
)
MAXIMUM_DEPTH = _PATH_MAX_DEPTH
MAXIMUM_RESULTS = McpToolPolicy().max_results

NODE_KIND_MEANINGS: dict[KnowledgeNodeKind, str] = {
    KnowledgeNodeKind.TENANT: "Tenant isolation root.",
    KnowledgeNodeKind.DATA_SOURCE: "Configured connector/source scope.",
    KnowledgeNodeKind.SOURCE_ENTITY: "Provider-specific source object.",
    KnowledgeNodeKind.DOCUMENT_VERSION: "Indexed version of a source document.",
    KnowledgeNodeKind.STRUCTURE: "Section, table, or comment structure.",
    KnowledgeNodeKind.CHUNK: "Citation-ready indexed evidence.",
}

ENTITY_TYPE_MEANINGS: dict[GraphEntityType, str] = {
    GraphEntityType.TENANT: "Tenant isolation root.",
    GraphEntityType.DATA_SOURCE: "Configured connector/source scope.",
    GraphEntityType.GENERIC_SOURCE_ITEM: "Provider-agnostic fallback source object.",
    GraphEntityType.DOCUMENT_VERSION: "Indexed version of a source document.",
    GraphEntityType.SECTION: "Structural section within a document version.",
    GraphEntityType.TABLE: "Structural table within a document version.",
    GraphEntityType.COMMENT: "Structural comment attached to a document version.",
    GraphEntityType.CHUNK: "Citation-ready indexed evidence.",
    GraphEntityType.CONFLUENCE_SPACE: "Confluence space.",
    GraphEntityType.CONFLUENCE_PAGE: "Confluence page.",
    GraphEntityType.CONFLUENCE_ATTACHMENT: "Confluence attachment.",
    GraphEntityType.JIRA_PROJECT: "Jira project.",
    GraphEntityType.JIRA_ISSUE: "Jira issue or subissue.",
    GraphEntityType.JIRA_ATTACHMENT: "Jira attachment.",
    GraphEntityType.GITHUB_OWNER: "GitHub owner (user or organization).",
    GraphEntityType.GITHUB_REPOSITORY: "GitHub repository.",
    GraphEntityType.GITHUB_DIRECTORY: "GitHub repository directory.",
    GraphEntityType.GITHUB_FILE: "GitHub repository file.",
    GraphEntityType.GITHUB_REF: "GitHub ref (branch or tag).",
    GraphEntityType.GITHUB_COMMIT: "GitHub commit.",
    GraphEntityType.SHAREPOINT_SITE: "SharePoint site.",
    GraphEntityType.SHAREPOINT_DRIVE: "SharePoint drive.",
    GraphEntityType.SHAREPOINT_FOLDER: "SharePoint folder.",
    GraphEntityType.SHAREPOINT_FILE: "SharePoint file.",
    GraphEntityType.LOCAL_ROOT: "Local filesystem ingestion root.",
    GraphEntityType.LOCAL_DIRECTORY: "Local filesystem directory.",
    GraphEntityType.LOCAL_FILE: "Local filesystem file.",
}

RELATION_MEANINGS: dict[RelationType, str] = {
    RelationType.HAS_DATA_SOURCE: "Tenant to configured data source.",
    RelationType.CONTAINS: (
        "Structural containment (source scope to entity, or entity to substructure)."
    ),
    RelationType.HAS_VERSION: "Source entity to its indexed document version.",
    RelationType.SUPPORTS: "Chunk to the structure or document version it evidences.",
    RelationType.PARENT_OF: "Normalized parent-child relation between source entities.",
    RelationType.LINKS_TO: "One source entity references another by link.",
    RelationType.HAS_ATTACHMENT: "Normalized relation from an entity to its attachment.",
    RelationType.REPLY_TO: "Comment replying to another comment.",
    RelationType.BLOCKS: "One issue blocks another (Jira).",
    RelationType.DUPLICATES: "One issue duplicates another (Jira).",
    RelationType.RELATES_TO: "Generic cross-entity relation (Jira issue links).",
    RelationType.POINTS_TO: "A ref points to a commit (GitHub).",
    RelationType.RESOLVED_AT: "An issue was resolved at a commit (GitHub/Jira linkage).",
    RelationType.INCLUDES: "A page transcludes another page's body (Confluence include macro).",
}

CONNECTOR_TOPOLOGIES: list[dict[str, object]] = [
    {
        "connector": "confluence",
        "entity_chain": [
            GraphEntityType.CONFLUENCE_SPACE.value,
            GraphEntityType.CONFLUENCE_PAGE.value,
            GraphEntityType.CONFLUENCE_ATTACHMENT.value,
        ],
    },
    {
        "connector": "jira",
        "entity_chain": [
            GraphEntityType.JIRA_PROJECT.value,
            GraphEntityType.JIRA_ISSUE.value,
            GraphEntityType.JIRA_ATTACHMENT.value,
        ],
    },
    {
        "connector": "github",
        "entity_chain": [
            GraphEntityType.GITHUB_OWNER.value,
            GraphEntityType.GITHUB_REPOSITORY.value,
            GraphEntityType.GITHUB_DIRECTORY.value,
            GraphEntityType.GITHUB_FILE.value,
            GraphEntityType.GITHUB_REF.value,
            GraphEntityType.GITHUB_COMMIT.value,
        ],
    },
    {
        "connector": "sharepoint",
        "entity_chain": [
            GraphEntityType.SHAREPOINT_SITE.value,
            GraphEntityType.SHAREPOINT_DRIVE.value,
            GraphEntityType.SHAREPOINT_FOLDER.value,
            GraphEntityType.SHAREPOINT_FILE.value,
        ],
    },
    {
        "connector": "local",
        "entity_chain": [
            GraphEntityType.LOCAL_ROOT.value,
            GraphEntityType.LOCAL_DIRECTORY.value,
            GraphEntityType.LOCAL_FILE.value,
        ],
    },
]

RECOMMENDED_WORKFLOWS: list[dict[str, object]] = [
    {
        "name": "schema_discovery",
        "use_when": "The caller does not yet understand the graph model.",
        "steps": ["describe_graph"],
    },
    {
        "name": "manual_graph_exploration",
        "use_when": "An agent needs to reason over a relationship interactively.",
        "steps": [
            "vector_search",
            "graph_subgraph_search(start_node=<chunk_id from vector_search>)",
            "graph_path_search or graph_triplet_search (optional)",
        ],
    },
]


def missing_node_kind_docs() -> list[KnowledgeNodeKind]:
    return [kind for kind in KnowledgeNodeKind if kind not in NODE_KIND_MEANINGS]


def missing_entity_type_docs() -> list[GraphEntityType]:
    return [entity for entity in GraphEntityType if entity not in ENTITY_TYPE_MEANINGS]


def missing_projected_relation_docs() -> list[RelationType]:
    return [relation for relation in PROJECTED_RELATION_TYPES if relation not in RELATION_MEANINGS]


def describe_graph_payload() -> dict[str, object]:
    return {
        "ok": True,
        "graph_schema_version": GRAPH_SCHEMA_VERSION,
        "capabilities": {
            "free_text_search": False,
            "partial_title_matching": False,
            "tenant_inventory": False,
            "vector_to_graph_handoff": True,
            "composed_context_retrieval": False,
        },
        "selector_rules": {
            "accepted": ["chunk_id", "node_key", "logical_id", "exact_title"],
            "title_matching": "case_insensitive_exact",
            "chunk_titles_available": False,
            "preferred_entry_tool": "vector_search",
        },
        "node_kinds": [
            {"name": kind.value, "meaning": meaning} for kind, meaning in NODE_KIND_MEANINGS.items()
        ],
        "entity_types": [
            {"name": entity.value, "meaning": meaning}
            for entity, meaning in ENTITY_TYPE_MEANINGS.items()
        ],
        "relation_types": [
            {"name": relation.value, "meaning": meaning}
            for relation, meaning in RELATION_MEANINGS.items()
        ],
        "direction_semantics": {
            "description": (
                "Every relation is stored once, from subject to object. Graph tools query "
                "it with a direction filter rather than storing the reverse edge."
            ),
            "accepted": [direction.value for direction in GraphDirection],
            "default": GraphDirection.BOTH.value,
        },
        "topologies": CONNECTOR_TOPOLOGIES,
        "workflows": RECOMMENDED_WORKFLOWS,
        "defaults": TOOL_DEFAULTS,
        "limits": {
            "maximum_depth": MAXIMUM_DEPTH,
            "maximum_results": MAXIMUM_RESULTS,
        },
    }
