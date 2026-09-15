"""Name the placeholder relations a repair pass replaces."""

from __future__ import annotations

from collections.abc import Sequence

from harborrag_core.ingestion import GraphEdgeRecord, KnowledgeNodeKind
from harborrag_engine.ingestion import GraphProjectionBatch


def document_relations(graph: GraphProjectionBatch) -> tuple[GraphEdgeRecord, ...]:
    """This document's own explicit source-entity relations, both ends included."""

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


def superseded_relations(
    first_pass: GraphProjectionBatch,
    *,
    resolved: Sequence[GraphEdgeRecord],
) -> tuple[GraphEdgeRecord, ...]:
    """Return the first projection's edges that the resolved projection replaces.

    ``first_pass`` is the same document rebuilt with no resolved targets, which
    reproduces what the original projection wrote: the same canonical document and chunks
    go in, and ``relation_id`` is derived from type, endpoints and source relation
    version. So the edges to retract can be named without reading the graph back.

    Compare both endpoints: reversed native predicates place the referenced
    placeholder on the source side. A placeholder endpoint still present in the
    repaired batch is not superseded, including a different unresolved target.
    """

    nodes = {node.node_key: node for node in first_pass.nodes}
    relations = document_relations(first_pass)
    resolved_keys = {
        key for relation in resolved for key in (relation.source_node_key, relation.target_node_key)
    }
    return tuple(
        relation
        for relation in relations
        if any(
            key not in resolved_keys and nodes[key].attributes.get("placeholder") is True
            for key in (relation.source_node_key, relation.target_node_key)
        )
    )
