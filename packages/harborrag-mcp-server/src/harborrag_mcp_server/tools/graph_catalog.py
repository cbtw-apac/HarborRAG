"""Static graph contract catalog backing the ``describe_graph`` tool.

The structural layer (node kinds, projected relation types) is read directly from its
canonical enums, so this catalog can never drift out of sync with the graph the
projector actually builds: adding a node kind or a projected relation changes the
enum, and ``describe_graph``'s output changes with it automatically.

The semantic (``semantic-v3``) and ontology (``enterprise-v1``) layers describe a
forward-looking, versioned contract for entity/relation extraction. No Pydantic model
in this repo enforces the ``Entity``/``RELATES`` property shapes yet, so those
versions and property lists are declared statically in ``harborrag_core`` rather than
derived from a canonical source.
"""

from __future__ import annotations

from harborrag_core.chunking import (
    CHUNK_PROPERTIES,
    COMMON_NODE_PROPERTIES,
    DOCUMENT_OWNED_PROPERTIES,
    ENTITY_PROPERTIES,
    PROJECTED_RELATION_TYPES,
    RELATES_PROPERTIES,
)
from harborrag_core.ingestion import KnowledgeNodeKind
from harborrag_core.ingestion.projection_contracts import (
    GRAPH_SCHEMA_VERSION,
    ONTOLOGY_SCHEMA_VERSION,
    SEMANTIC_SCHEMA_VERSION,
)

STRUCTURAL_SCHEMA_VERSION: str = GRAPH_SCHEMA_VERSION

GRAPH_NODE_KINDS: tuple[str, ...] = tuple(kind.value for kind in KnowledgeNodeKind)
GRAPH_RELATION_TYPES: tuple[str, ...] = tuple(
    relation.value for relation in PROJECTED_RELATION_TYPES
)


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
            "nodes": list(GRAPH_NODE_KINDS),
            "relations": list(GRAPH_RELATION_TYPES),
        },
        "properties": {
            "common_node": list(COMMON_NODE_PROPERTIES),
            "document_owned": list(DOCUMENT_OWNED_PROPERTIES),
            "Chunk": list(CHUNK_PROPERTIES),
            "Entity": list(ENTITY_PROPERTIES),
            "RELATES": list(RELATES_PROPERTIES),
        },
    }
