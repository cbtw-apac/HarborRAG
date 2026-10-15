"""One logical relationship is one edge, however many projectors assert it."""

from __future__ import annotations

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput
from harborrag_engine.ingestion.projections.graph.graph_models import GraphDocumentTarget

from .chunking_helpers import make_document, make_profile, make_request, make_service
from .graph_convergence_helpers import project


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


def test_one_page_to_attachment_edge_however_many_documents_assert_it() -> None:
    """Page and attachment both describe page -> attachment; only the attachment draws it.

    The page lists the attachment (its projector) and declares `has_attachment` (a
    source relation), and the attachment declares `attached_to`. Every one of those used
    to become an edge owned by its own document version, and relation_id hashes that
    version, so MERGE kept them apart and every copy consumed a slot of each query's
    LIMIT. The attachment is the single owner.
    """

    page = _attachment_projection()
    attachment = project(
        "confluence",
        {
            "space_id": "SPACE",
            "space_key": "SPACE",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "77",
        },
        source_item_id="att-9",
        relations=[
            DocumentRelation(predicate="attached_to", target_id="77", target_type="document")
        ],
    )

    def edges(batch):
        return [
            (edge.source_node_key, edge.target_node_key, edge.relation_id)
            for edge in batch.relations
            if edge.relation_type is RelationType.HAS_ATTACHMENT
        ]

    assert edges(page) == []
    assert len(edges(attachment)) == 1, edges(attachment)


def _jira_issue_projection(resolved: bool):
    """A Jira issue declaring `has_attachment` for an attachment that is its own document."""

    document = make_document(
        [DocumentElement("p1", "paragraph", "Issue evidence")],
        source="jira",
        extra={"project_id": "ENG", "project_key": "ENG", "issue_key": "ENG-1"},
    )
    document.relations = [
        DocumentRelation(
            predicate="has_attachment",
            target_id="jira://ENG/ENG-1/attachments/55",
            target_type="document",
            metadata={"source_relation_version": "attachment-v1"},
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
                "connector_type": ConnectorType("jira"),
                "document_kind": DocumentKind("jira_file"),
                "source_item_id": "jira://ENG/ENG-1",
            }
        )
        for chunk in chunks
    )
    targets = (
        {
            "jira://ENG/ENG-1/attachments/55": GraphDocumentTarget(
                source_item_id="jira://ENG/ENG-1/attachments/55",
                document_id="doc-attachment",
                document_version_id="version-attachment",
                source_scope_id="tenant-1",
                title="payment-trace.log",
            )
        }
        if resolved
        else {}
    )
    return GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=rebound,
            resolved_targets=targets,
            graph_projection_version="graph-v2",
        )
    )


def test_the_container_leaves_has_attachment_to_the_attachment_document() -> None:
    """Measured live: every Jira issue -> attachment pair had two parallel edges.

    One came from the attachment's own projection, one from relation repair of the
    issue's `has_attachment`, owned by the issue version. The attachment's edge is the
    one that lives and dies with the attachment, so the issue asserts none.
    """

    issue = _jira_issue_projection(resolved=True)

    assert [r for r in issue.relations if r.relation_type is RelationType.HAS_ATTACHMENT] == []
    assert issue.unresolved_relations == ()


def test_an_unresolved_attachment_is_still_reported() -> None:
    issue = _jira_issue_projection(resolved=False)

    assert [u.relation_type for u in issue.unresolved_relations] == [
        RelationType.HAS_ATTACHMENT.value
    ]
