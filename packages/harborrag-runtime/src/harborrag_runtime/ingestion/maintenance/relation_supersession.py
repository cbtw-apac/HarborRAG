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

    The discriminator is the *target node key*, not the relation. Only the targets whose
    far end moved are superseded: a target key present in both projections was already
    resolvable on the first pass and its edge is live, so matching on
    ``(type, source, version)`` instead would let one resolved link mark every other link
    of the same document for deletion. The placeholder attribute is checked too, so a key
    that is placeholder-only for some other reason is left alone.
    """

    nodes = {node.node_key: node for node in first_pass.nodes}
    relations = document_relations(first_pass)
    retired = {relation.target_node_key for relation in relations} - {
        relation.target_node_key for relation in resolved
    }
    return tuple(
        relation
        for relation in relations
        if relation.target_node_key in retired
        and nodes[relation.target_node_key].attributes.get("placeholder") is True
    )
