"""Cross-document node-key convergence for provider source projectors.

Each case here builds *two* documents and compares their node keys. A source-entity
node key hashes tenant, scope, entity_type and provider id, so a placeholder and the
concrete node it stands in for converge only when all four agree -- and a mismatch is
silent, because ``GraphProjectionState`` only detects conflicts within one build. That
is why these live apart from the single-document topology tests.
"""

from __future__ import annotations

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType, KnowledgeNodeKind
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput

from .chunking_helpers import make_document, make_profile, make_request, make_service


def _project(
    connector: str,
    extra: dict[str, object],
    *,
    source_item_id: str,
    relations: list[DocumentRelation] | None = None,
):
    document = make_document(
        [DocumentElement("p1", "paragraph", "Provider evidence")],
        source=connector,
        extra=extra,
    )
    document.relations = relations or []
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
                "connector_type": ConnectorType(connector),
                "document_kind": DocumentKind(f"{connector}_file"),
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


def _keys(graph, entity_type: GraphEntityType) -> dict[str, str]:
    return {
        node.logical_id: node.node_key for node in graph.nodes if node.entity_type is entity_type
    }


def test_confluence_attachment_parent_converges_with_the_ingested_page() -> None:
    """A dispatched attachment must key its parent the way that page keys itself.

    ``attachment_source_record`` copies ``parent_source_item_id`` verbatim from the parent
    ``SourceRecord.id``, which for Confluence is ``confluence://SPACE/ID`` -- while the
    page's own projection keys it by the bare content id. Both the provider projector and
    the reversed ``attached_to`` edge build a stand-in for that page, and both feed
    ``entity_type`` and ``provider_id`` into the node key, so either one skipping the
    reduction or mistyping the far end leaves the real page unreachable behind a stub.
    """

    page = _project(
        "confluence",
        {"space_id": "space-1", "space_key": "ENG", "page_id": "77"},
        source_item_id="confluence://ENG/77",
    )
    attachment = _project(
        "confluence",
        {
            "space_id": "space-1",
            "space_key": "ENG",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "confluence://ENG/77",
        },
        source_item_id="confluence://ENG/77/attachments/att-9",
        relations=[
            DocumentRelation(
                predicate="attached_to",
                target_id="confluence://ENG/77",
                target_type="document",
            )
        ],
    )

    # Exactly one parent node, on the key the page really has -- not one per creating path.
    assert _keys(attachment, GraphEntityType.CONFLUENCE_PAGE) == _keys(
        page, GraphEntityType.CONFLUENCE_PAGE
    )
    # The far end of a reversed attached_to is the container, so nothing types it as a
    # second attachment, and the two paths collapse to a single has_attachment edge.
    assert set(_keys(attachment, GraphEntityType.CONFLUENCE_ATTACHMENT)) == {"att-9"}
    assert (
        sum(
            1
            for relation in attachment.relations
            if relation.relation_type is RelationType.HAS_ATTACHMENT
        )
        == 1
    )


def test_jira_attachment_is_typed_as_an_attachment_not_as_a_sibling_issue() -> None:
    """Jira dispatches attachments as their own source items exactly as Confluence does.

    ``extra`` here is the shape ``AttachmentDocumentLoader`` and ``reidentify`` actually
    produce, media metadata included. An earlier version of this test supplied only the
    three keys it asserted on, so it exercised this path with a document no connector
    emits -- and missed that the projector passed ``media_type``/``size_bytes`` into
    ``attributes``, which are not allowlisted and therefore raised on every real
    attachment. Keep the unused keys: they are what makes this a regression test.
    """

    issue = _project(
        "jira",
        {"project_id": "ENG", "project_key": "ENG", "issue_key": "ENG-1"},
        source_item_id="jira://ENG/ENG-1",
    )
    attachment = _project(
        "jira",
        {
            "attachment_id": "55",
            "title": "payment-trace.log",
            "filename": "payment-trace.log",
            "media_type": "text/plain",
            "size_bytes": 2048,
            "source_version": "1",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "jira://ENG/ENG-1",
        },
        source_item_id="jira://ENG/ENG-1/attachments/55",
        relations=[
            DocumentRelation(
                predicate="attached_to",
                target_id="jira://ENG/ENG-1",
                target_type="document",
            )
        ],
    )

    assert set(_keys(attachment, GraphEntityType.JIRA_ATTACHMENT)) == {"55"}
    # The only issue in an attachment's batch is its parent, on the real issue's key.
    assert _keys(attachment, GraphEntityType.JIRA_ISSUE) == _keys(issue, GraphEntityType.JIRA_ISSUE)
    # An attachment carries no project metadata, so it invents no project at all -- the
    # issue's own projection is what files it under the real one. Minting a fallback here
    # gave the real issue a second, fictional project parent.
    assert _keys(attachment, GraphEntityType.JIRA_PROJECT) == {}
    assert set(_keys(issue, GraphEntityType.JIRA_PROJECT)) == {"ENG"}
    assert _fallback_containers(attachment) == set()
    # The parent issue is what earns project membership -- and the project was derived
    # from that very issue, so there is no cross-project risk here by construction. The
    # attachment stays off the membership axis so `project CONTAINS` stays issues only.
    assert _placeholders_claiming_a_container(attachment) == {"ENG-1"}


def test_confluence_attachment_invents_no_space_for_its_parent() -> None:
    """The measured defect: 19 of 20 live space-root edges came from attachments.

    A Confluence attachment carries no space metadata, so the old fallback minted an
    `unknown-space` node and asserted CONTAINS from it to the attachment's real parent
    page -- a page its own projection had already filed under the true space.
    """

    page = _project(
        "confluence",
        {"space_id": "92045319", "space_key": "HARBORRAG", "page_id": "77"},
        source_item_id="confluence://HARBORRAG/77",
    )
    attachment = _project(
        "confluence",
        {
            "attachment_id": "att-9",
            "title": "runbook.pdf",
            "media_type": "application/pdf",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "confluence://HARBORRAG/77",
        },
        source_item_id="confluence://HARBORRAG/77/attachments/att-9",
        relations=[
            DocumentRelation(
                predicate="attached_to",
                target_id="confluence://HARBORRAG/77",
                target_type="document",
            )
        ],
    )

    assert set(_keys(page, GraphEntityType.CONFLUENCE_SPACE)) == {"92045319"}
    assert _keys(attachment, GraphEntityType.CONFLUENCE_SPACE) == {}
    assert _fallback_containers(attachment) == set()
    # The parent stand-in still converges on the real page, and is reachable through the
    # data source rather than through a fabricated space.
    assert _keys(attachment, GraphEntityType.CONFLUENCE_PAGE) == _keys(
        page, GraphEntityType.CONFLUENCE_PAGE
    )
    assert _reaches_data_source(attachment)
    # With no space metadata the honest container is the data source, and what it holds is
    # the parent *page* -- never the attachment, which would make that set heterogeneous.
    assert _placeholders_claiming_a_container(attachment) == {"77"}


def test_jira_cross_project_parent_is_not_filed_under_the_wrong_project() -> None:
    """A document may only assert containment for the issue it *is*.

    Jira allows a parent in another project, and subtask/parent references name issues
    this document does not own. Filing them under *this* issue's project gave the real
    issue a second, wrong project once it was ingested -- and that edge is scope-owned,
    so nothing would ever reclaim it. ``PARENT_OF`` carries the relationship instead.
    """

    subtask = _project(
        "jira",
        {"project_key": "ENG", "issue_key": "ENG-2", "parent": {"key": "OPS-9", "summary": "Epic"}},
        source_item_id="jira://ENG/ENG-2",
    )
    epic = _project(
        "jira",
        {"project_key": "OPS", "issue_key": "OPS-9"},
        source_item_id="jira://OPS/OPS-9",
    )

    # The out-of-project parent exists as a stand-in and converges, but is claimed by
    # nobody's project until its own document arrives.
    assert set(_keys(subtask, GraphEntityType.JIRA_ISSUE)) == {"ENG-2", "OPS-9"}
    assert (
        _keys(subtask, GraphEntityType.JIRA_ISSUE)["OPS-9"]
        == (_keys(epic, GraphEntityType.JIRA_ISSUE)["OPS-9"])
    )
    assert set(_keys(subtask, GraphEntityType.JIRA_PROJECT)) == {"ENG"}
    assert _placeholders_claiming_a_container(subtask) == set()
    # The relationship itself is still projected.
    assert any(relation.relation_type is RelationType.PARENT_OF for relation in subtask.relations)


def _fallback_containers(graph) -> set[str]:
    """Any node whose provider id is one of the invented `unknown-*` hubs."""

    return {
        str(node.logical_id) for node in graph.nodes if str(node.logical_id).startswith("unknown-")
    }


def _placeholders_claiming_a_container(graph) -> set[str]:
    """Placeholders handed a CONTAINS parent, keyed by provider id.

    CONTAINS is *membership*, not position, so handing it to a nested parent is no longer
    wrong in itself -- a page five levels down is still a member of its space. What is
    still wrong is claiming membership of a container the node does not belong to: Jira
    allows a parent in another project, and filing it under *this* document's project
    gives the real issue a second, fictional one. So the callers below assert different
    things: an attachment's own parent is expected here (its container was derived from
    that very parent), a cross-project parent is not.
    """

    nodes = {node.node_key: node for node in graph.nodes}
    return {
        str(nodes[relation.target_node_key].logical_id)
        for relation in graph.relations
        if relation.relation_type is RelationType.CONTAINS
        and nodes[relation.target_node_key].attributes.get("placeholder") is True
    }


def _reaches_data_source(graph) -> bool:
    """Every concrete source entity must still be reachable from the data source."""

    nodes = {node.node_key: node for node in graph.nodes}
    adjacency: dict[str, set[str]] = {}
    for relation in graph.relations:
        adjacency.setdefault(relation.source_node_key, set()).add(relation.target_node_key)
        adjacency.setdefault(relation.target_node_key, set()).add(relation.source_node_key)
    roots = [key for key, node in nodes.items() if node.node_kind is KnowledgeNodeKind.DATA_SOURCE]
    seen = set(roots)
    frontier = list(roots)
    while frontier:
        frontier = [
            target
            for key in frontier
            for target in adjacency.get(key, ())
            if target not in seen and not seen.add(target)
        ]
    concrete = {
        key
        for key, node in nodes.items()
        if node.node_kind is KnowledgeNodeKind.SOURCE_ENTITY
        and not node.attributes.get("placeholder")
    }
    return concrete <= seen


def test_sharepoint_folder_identity_does_not_depend_on_item_depth() -> None:
    shallow = _project(
        "sharepoint",
        {
            "site_id": "site-1",
            "drive_id": "drive-1",
            "item_id": "file-1",
            "parent": {"id": "folder-2", "path": "/drives/drive-1/root:/Policies/Security"},
        },
        source_item_id="sharepoint://site-1/drive-1/file-1",
    )
    deep = _project(
        "sharepoint",
        {
            "site_id": "site-1",
            "drive_id": "drive-1",
            "item_id": "file-2",
            "parent": {"id": "folder-9", "path": "/drives/drive-1/root:/Policies/Security/2026"},
        },
        source_item_id="sharepoint://site-1/drive-1/file-2",
    )

    shallow_folders = _keys(shallow, GraphEntityType.SHAREPOINT_FOLDER)
    deep_folders = _keys(deep, GraphEntityType.SHAREPOINT_FOLDER)
    # The folder is the shallow item's immediate parent and the deep item's ancestor; the
    # provider id it carries in one case must not fork it into a second node.
    assert set(shallow_folders) == {"Policies", "Policies/Security"}
    assert shallow_folders["Policies/Security"] == deep_folders["Policies/Security"]
    assert set(deep_folders) - set(shallow_folders) == {"Policies/Security/2026"}
