"""One logical relationship is one edge, however many projectors assert it."""

from __future__ import annotations

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput
from harborrag_engine.ingestion.projections.graph.graph_models import GraphDocumentTarget

from .chunking_helpers import make_document, make_profile, make_request, make_service


def _attachment_projection():
    """An attachment that both hangs off its page and resolves `attached_to` back to it.

    Two authors then describe the identical page -> attachment pair: the Confluence
    projector (parent -> item) and the resolved reverse source relation.
    """
    document = make_document(
        [DocumentElement("p1", "paragraph", "Attachment evidence")],
        source="confluence",
        extra={
            "space_key": "SPACE",
            "space_id": "SPACE",
            "page_id": "77",
            "content_id": "77",
            "attachments": [{"id": "att-9", "title": "Diagram.png"}],
        },
    )
    document.relations = [
        DocumentRelation(
            predicate="has_attachment",
            target_id="att-9",
            target_type="attachment",
            # The provider stamps the related entity's own version here. It feeds the
            # relation_id hash, so without it both authors happen to agree by accident.
            metadata={"source_relation_version": "attachment-v7"},
        )
    ]
    chunks = (
        make_service(
            make_profile(target=40, maximum=60),
            configuration_version="3",
            create_route_chunks=True,
        )
        .chunk(make_request(make_document(document.content)))
        .chunks
    )
    rebound = tuple(
        chunk.model_copy(
            update={
                "connector_type": ConnectorType("confluence"),
                "document_kind": DocumentKind("confluence_file"),
                "source_item_id": "77",
            }
        )
        for chunk in chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=rebound,
            resolved_targets={
                "att-9": GraphDocumentTarget(
                    source_item_id="att-9",
                    document_id="doc-attachment",
                    document_version_id="version-attachment",
                    source_scope_id="tenant-1",
                    title="Diagram.png",
                )
            },
            graph_projection_version="graph-v2",
        )
    )


def test_one_page_to_attachment_edge_however_many_projectors_assert_it() -> None:
    attachment = _attachment_projection()

    edges = [
        relation
        for relation in attachment.relations
        if relation.relation_type is RelationType.HAS_ATTACHMENT
    ]

    # Parallel edges differ only by relation_id, so MERGE keys them apart and every one
    # survives into the graph and consumes a slot of each query's LIMIT.
    assert len(edges) == 1, [
        (edge.source_node_key, edge.target_node_key, edge.relation_id) for edge in edges
    ]
