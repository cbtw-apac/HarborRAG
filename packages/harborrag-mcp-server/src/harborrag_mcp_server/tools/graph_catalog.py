"""Static graph contract catalog backing the ``describe_graph`` tool.

The structural layer (node kinds, projected relation types) is read directly from its
canonical enums, so this catalog can never drift out of sync with the graph the
projector actually builds: adding a node kind or a projected relation changes the
enum, and ``describe_graph``'s output changes with it automatically.

The semantic (``semantic-v3``) and ontology (``enterprise-v1``) layers describe a
forward-looking, versioned contract for entity/relation extraction. No Pydantic model
in this repo enforces the ``Entity``/``RELATES`` property shapes yet, so those lists
are declared statically here rather than derived from a canonical source.
"""

from __future__ import annotations

from harborrag_core.chunking import PROJECTED_RELATION_TYPES
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import GRAPH_SCHEMA_VERSION

STRUCTURAL_SCHEMA_VERSION: str = GRAPH_SCHEMA_VERSION
SEMANTIC_SCHEMA_VERSION: str = "semantic-v3"
ONTOLOGY_SCHEMA_VERSION: str = "enterprise-v1"

GRAPH_NODE_KINDS: list[str] = [kind.value for kind in KnowledgeNodeKind]
GRAPH_RELATION_TYPES: list[str] = [relation.value for relation in PROJECTED_RELATION_TYPES]

COMMON_NODE_PROPERTIES: list[str] = ["node_key", "name", "description", "entity_type"]
DOCUMENT_OWNED_PROPERTIES: list[str] = ["document_id", "document_version_id", "source_scope_id"]
CHUNK_PROPERTIES: list[str] = ["chunk_id", "section_path"]
ENTITY_PROPERTIES: list[str] = ["id", "name", "type", "description", "aliases", "support_count"]
RELATES_PROPERTIES: list[str] = [
    "types",
    "description",
    "weight",
    "polarities",
    "modalities",
    "support_count",
]


def describe_graph_payload() -> dict[str, object]:
    """Build the static ``describe_graph`` response."""
    return {
        "ok": True,
        "versions": {
            "structural": STRUCTURAL_SCHEMA_VERSION,
            "semantic": SEMANTIC_SCHEMA_VERSION,
            "ontology": ONTOLOGY_SCHEMA_VERSION,
        },
        "layers": {
            "nodes": GRAPH_NODE_KINDS,
            "relations": GRAPH_RELATION_TYPES,
        },
        "properties": {
            "common_node": COMMON_NODE_PROPERTIES,
            "document_owned": DOCUMENT_OWNED_PROPERTIES,
            "Chunk": CHUNK_PROPERTIES,
            "Entity": ENTITY_PROPERTIES,
            "RELATES": RELATES_PROPERTIES,
        },
    }
