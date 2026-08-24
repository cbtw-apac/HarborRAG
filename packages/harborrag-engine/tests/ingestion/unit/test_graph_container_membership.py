"""The container spine: DataSource -> space -> page -> attachment.

Two things went wrong here in sequence. First an attachment binding carried none of its
parent's space metadata, so the projector's fallback anchored it on the data source: 147
attachments there, 1 space, zero pages, the two levels disagreeing. Then the fix over-
corrected and put attachments under the space beside pages, which made `space CONTAINS`
heterogeneous -- 104 pages and 149 attachments in one edge set, so counting pages needed
an entity-type filter. Membership is pages; the attachment belongs to its page.
"""

from __future__ import annotations

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType, KnowledgeNodeKind
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput

from .chunking_helpers import make_document, make_profile, make_request, make_service

_SPACE = {"space_id": "92045319", "space_key": "HARBORRAG"}


def _project(extra: dict[str, object], *, source_item_id: str):
    document = make_document(
        [DocumentElement("p1", "paragraph", "Provider evidence")],
        source="confluence",
        extra=extra,
    )
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
                "connector_type": ConnectorType.CONFLUENCE,
                "document_kind": DocumentKind("confluence_file"),
                "source_item_id": source_item_id,
            }
        )
        for chunk in chunks
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=rebound,
            resolved_targets={},
            graph_projection_version="graph-v2",
        )
    )


def _contained_by(graph, node_kind: KnowledgeNodeKind) -> set[str]:
    """Entity types a node of this kind CONTAINS."""

    types = {node.node_key: node for node in graph.nodes}
    return {
        types[edge.target_node_key].entity_type.value
        for edge in graph.relations
        if edge.relation_type is RelationType.CONTAINS
        and types[edge.source_node_key].node_kind is node_kind
        and types[edge.target_node_key].entity_type is not None
    }


def _space_contains(graph) -> set[str]:
    types = {node.node_key: node for node in graph.nodes}
    return {
        types[edge.target_node_key].entity_type.value
        for edge in graph.relations
        if edge.relation_type is RelationType.CONTAINS
        and types[edge.source_node_key].entity_type is GraphEntityType.CONFLUENCE_SPACE
    }


def _attachment_parents(graph) -> set[str]:
    """Entity types that own an attachment by HAS_ATTACHMENT."""

    types = {node.node_key: node for node in graph.nodes}
    return {
        types[edge.source_node_key].entity_type.value
        for edge in graph.relations
        if edge.relation_type is RelationType.HAS_ATTACHMENT
    }


def test_a_page_is_contained_by_its_space_not_by_the_data_source() -> None:
    graph = _project(
        {**_SPACE, "page_id": "91980595"},
        source_item_id="confluence://HARBORRAG/91980595",
    )

    assert _space_contains(graph) == {"confluence_page"}
    assert _contained_by(graph, KnowledgeNodeKind.DATA_SOURCE) == {"confluence_space"}


def test_an_attachment_reaches_its_space_through_its_page_not_beside_it() -> None:
    """The membership set stays pages-only; the attachment hangs off its page.

    An earlier pass put the attachment under the space directly, which made
    `space CONTAINS` hold pages *and* attachments -- 104 and 149 of them on the live graph
    -- so counting pages needed an entity-type filter. The parent is a page, so the parent
    is what earns membership, and the attachment is one HAS_ATTACHMENT hop further down.
    """

    graph = _project(
        {
            **_SPACE,
            "binding_kind": "ATTACHMENT",
            "page_id": "att91980616",
            "parent_source_item_id": "confluence://HARBORRAG/91980595",
        },
        source_item_id="confluence://HARBORRAG/91980595/attachments/att91980616",
    )

    assert _space_contains(graph) == {"confluence_page"}
    assert _contained_by(graph, KnowledgeNodeKind.DATA_SOURCE) == {"confluence_space"}
    assert _attachment_parents(graph) == {"confluence_page"}


def test_an_attachment_without_space_metadata_still_anchors_somewhere() -> None:
    """The fallback stays: an attachment whose connector cannot supply a container must
    remain reachable from the tenant spine rather than being stranded -- now by way of its
    parent page, which is what the data source holds."""

    graph = _project(
        {
            "binding_kind": "ATTACHMENT",
            "page_id": "att91980616",
            "parent_source_item_id": "confluence://HARBORRAG/91980595",
        },
        source_item_id="confluence://HARBORRAG/91980595/attachments/att91980616",
    )

    assert _space_contains(graph) == set()
    assert _contained_by(graph, KnowledgeNodeKind.DATA_SOURCE) == {"confluence_page"}
    assert _attachment_parents(graph) == {"confluence_page"}


def test_page_and_attachment_resolve_to_the_very_same_space_node() -> None:
    """Different provider ids for the space would fork it into two nodes, which is why
    the attachment inherits space_id and not just space_key."""

    page = _project(
        {**_SPACE, "page_id": "91980595"},
        source_item_id="confluence://HARBORRAG/91980595",
    )
    attachment = _project(
        {
            **_SPACE,
            "binding_kind": "ATTACHMENT",
            "page_id": "att91980616",
            "parent_source_item_id": "confluence://HARBORRAG/91980595",
        },
        source_item_id="confluence://HARBORRAG/91980595/attachments/att91980616",
    )

    def space_key(graph) -> str:
        return next(
            node.node_key
            for node in graph.nodes
            if node.entity_type is GraphEntityType.CONFLUENCE_SPACE
        )

    assert space_key(page) == space_key(attachment)
