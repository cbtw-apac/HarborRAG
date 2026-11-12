"""Select the explicit relations a document owns for a repair pass."""

from __future__ import annotations

from harborrag_core.ingestion import GraphEdgeRecord, KnowledgeNodeKind
from harborrag_engine.ingestion import GraphProjectionBatch


def document_relations(graph: GraphProjectionBatch) -> tuple[GraphEdgeRecord, ...]:
    """Return the document's own explicit source-entity relations, both ends included.

    Args:
        graph: Projection batch of one document.

    Returns:
        Relations whose source and target are both source-entity nodes of the batch.
    """
    document_node_keys = {
        node.node_key for node in graph.nodes if node.node_kind == KnowledgeNodeKind.SOURCE_ENTITY
    }
    return tuple(
        relation
        for relation in graph.relations
        if relation.source_explicit
        and relation.source_node_key in document_node_keys
        and relation.target_node_key in document_node_keys
    )
