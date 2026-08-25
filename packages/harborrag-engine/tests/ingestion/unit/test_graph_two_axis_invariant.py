"""The two-axis invariant, asserted for every connector.

CONTAINS is *membership*: which documents belong to a container, flat and single-typed, so
counting them is one hop. PARENT_OF is *structure*: where an item sits, deliberately
heterogeneous. They used to be one edge, so a container's children mixed documents with
the nodes above them -- measured on a live graph, one Confluence space held 104 pages and
149 attachments under the same edge type, and "how many pages in this space" needed an
entity-type filter and a guess at depth.
"""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import GraphEntityType, KnowledgeNodeKind
from harborrag_engine.ingestion import GraphProjectionBuilder, GraphProjectionInput

from .chunking_helpers import make_document, make_profile, make_request, make_service

# One document per connector, shaped the way its own discovery shapes it, deep enough that
# every connector actually builds a multi-level tree rather than a single edge.
CASES: dict[str, tuple[dict[str, object], str]] = {
    "confluence": (
        {
            "space_id": "space-1",
            "space_key": "ENG",
            "page_id": "page-9",
            "ancestor_ids": ["page-1", "page-4"],
            "ancestor_titles": ["Root", "Section"],
        },
        "confluence://ENG/page-9",
    ),
    "jira": (
        {"project_key": "ENG", "issue_key": "ENG-7", "parent": {"key": "ENG-1"}},
        "jira://ENG/ENG-7",
    ),
    "github": (
        {
            "owner": "acme",
            "repo": "harbor",
            "repository_id": "repo-1",
            "path": "docs/deep/guide.md",
            "ref": "main",
            "commit_sha": "abcdef1234567890",
        },
        "github://acme/harbor/docs/deep/guide.md",
    ),
    "sharepoint": (
        {
            "site_id": "site-1",
            "drive_id": "drive-1",
            "item_id": "file-1",
            "parent": {"id": "folder-2", "path": "/drives/drive-1/root:/Policies/Security"},
        },
        "sharepoint://site-1/drive-1/file-1",
    ),
    "local": ({"relative_path": "docs/deep/guide.md"}, "docs/deep/guide.md"),
}

# Attachment documents, kept apart from CASES: an attachment sits on neither axis -- it
# hangs off exactly one document by HAS_ATTACHMENT -- so the two tests that require the
# item on CONTAINS and on PARENT_OF do not describe it. Without these cases nothing in this
# module ever projects an attachment, and `test_a_container_never_holds_an_attachment`
# passed on documents that had no attachment to misplace in the first place.
# Each entry is (connector, extra, source_item_id, expected parent entity type and id).
ATTACHMENT_CASES: dict[str, tuple[str, dict[str, object], str, tuple[str, str]]] = {
    "confluence attachment": (
        "confluence",
        {
            "attachment_id": "att-9",
            "title": "runbook.pdf",
            "media_type": "application/pdf",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "confluence://ENG/page-9",
        },
        "confluence://ENG/page-9/attachments/att-9",
        ("confluence_page", "page-9"),
    ),
    "jira attachment": (
        "jira",
        {
            "attachment_id": "55",
            "title": "payment-trace.log",
            "filename": "payment-trace.log",
            "media_type": "text/plain",
            "size_bytes": 2048,
            "source_version": "1",
            "binding_kind": "ATTACHMENT",
            "parent_source_item_id": "jira://ENG/ENG-7",
        },
        "jira://ENG/ENG-7/attachments/55",
        ("jira_issue", "ENG-7"),
    ),
}

# Connectors whose structure chain is rooted at the container the item belongs to, so a
# top-down PARENT_OF walk from that container reaches the item inside one batch. Jira is
# deliberately not one of them -- see the test below.
_CONTAINER_ROOTED = tuple(connector for connector in sorted(CASES) if connector != "jira")

# Every projected document in this module, attachments included, for the invariants that
# hold whatever the document is.
_ALL_CASES: dict[str, tuple[str, dict[str, object], str]] = {
    **{connector: (connector, extra, item) for connector, (extra, item) in CASES.items()},
    **{name: (case[0], case[1], case[2]) for name, case in ATTACHMENT_CASES.items()},
}

_ATTACHMENT_TYPES = frozenset(
    {
        GraphEntityType.CONFLUENCE_ATTACHMENT,
        GraphEntityType.JIRA_ATTACHMENT,
    }
)


def _project(connector: str, extra: dict[str, object], *, source_item_id: str):
    document = make_document(
        [DocumentElement("p1", "paragraph", "Provider evidence")],
        source=connector,
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


def _contains_by_source(graph) -> dict[str, set[str]]:
    """Map each node to the set of entity types it CONTAINS."""

    nodes = {node.node_key: node for node in graph.nodes}
    grouped: dict[str, set[str]] = {}
    for edge in graph.relations:
        if edge.relation_type is not RelationType.CONTAINS:
            continue
        target = nodes[edge.target_node_key]
        label = target.entity_type.value if target.entity_type else target.node_kind.value
        grouped.setdefault(edge.source_node_key, set()).add(label)
    return grouped


def _document_item(graph):
    """The source entity this document *is* -- the one its version hangs off."""

    return next(
        node
        for node in graph.nodes
        if node.node_kind is KnowledgeNodeKind.SOURCE_ENTITY
        and any(
            edge.source_node_key == node.node_key and edge.relation_type is RelationType.HAS_VERSION
            for edge in graph.relations
        )
    )


def _edges(graph, relation_type: RelationType) -> set[tuple[str, str]]:
    return {
        (edge.source_node_key, edge.target_node_key)
        for edge in graph.relations
        if edge.relation_type is relation_type
    }


@pytest.mark.parametrize("connector", sorted(CASES))
def test_every_contains_set_holds_exactly_one_entity_type(connector: str) -> None:
    """The invariant. A heterogeneous CONTAINS set is what forces callers to filter."""

    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)
    nodes = {node.node_key: node for node in graph.nodes}

    for source_key, target_types in _contains_by_source(graph).items():
        source = nodes[source_key]
        # DocumentVersion owns the structural projection (sections, tables, comments),
        # which is a different concern from source-entity membership.
        if source.node_kind is KnowledgeNodeKind.DOCUMENT_VERSION:
            continue
        label = source.entity_type.value if source.entity_type else source.node_kind.value
        assert len(target_types) == 1, (connector, label, sorted(target_types))


@pytest.mark.parametrize("case", sorted(_ALL_CASES))
def test_a_container_never_holds_an_attachment(case: str) -> None:
    connector, extra, source_item_id = _ALL_CASES[case]
    graph = _project(connector, extra, source_item_id=source_item_id)
    nodes = {node.node_key: node for node in graph.nodes}

    held = {
        nodes[edge.target_node_key].entity_type
        for edge in graph.relations
        if edge.relation_type is RelationType.CONTAINS
    }
    assert not (held & _ATTACHMENT_TYPES), case


@pytest.mark.parametrize("case", sorted(ATTACHMENT_CASES))
def test_an_attachment_hangs_off_its_document_and_off_nothing_else(case: str) -> None:
    """The positive half: the attachment is on HAS_ATTACHMENT, and only there.

    `test_a_container_never_holds_an_attachment` is satisfied by an attachment that is on
    no edge at all, so it has to be paired with a check that the one edge which should
    reach it does -- from the parent document, not from a space, project or drive.
    """

    connector, extra, source_item_id, expected_parent = ATTACHMENT_CASES[case]
    graph = _project(connector, extra, source_item_id=source_item_id)
    nodes = {node.node_key: node for node in graph.nodes}
    attachment = _document_item(graph)
    assert attachment.entity_type in _ATTACHMENT_TYPES, case

    parent_key = next(
        node.node_key
        for node in graph.nodes
        if (node.entity_type.value, node.logical_id) == expected_parent
    )
    assert _edges(graph, RelationType.HAS_ATTACHMENT) == {(parent_key, attachment.node_key)}
    assert nodes[parent_key].node_kind is KnowledgeNodeKind.SOURCE_ENTITY

    for axis in (RelationType.CONTAINS, RelationType.PARENT_OF):
        assert attachment.node_key not in {target for _, target in _edges(graph, axis)}, (
            case,
            axis,
        )


@pytest.mark.parametrize("connector", sorted(_CONTAINER_ROOTED))
def test_the_structure_axis_reaches_the_item_from_its_container(connector: str) -> None:
    """PARENT_OF must be walkable top-down, whether or not anything sits in between.

    An incoming PARENT_OF edge proves nothing on its own about where that chain starts: a
    subtree hanging off some node the item does not belong to satisfies it just as well.
    So the walk starts where a caller's walk starts -- at the container that holds the
    item's membership -- and follows PARENT_OF down until it either reaches the item or
    runs out of tree.
    """

    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)
    item = _document_item(graph)

    containers = {
        source for source, target in _edges(graph, RelationType.CONTAINS) if target == item.node_key
    }
    assert containers, connector

    children: dict[str, set[str]] = {}
    for source, target in _edges(graph, RelationType.PARENT_OF):
        children.setdefault(source, set()).add(target)

    reached: set[str] = set()
    frontier = list(containers)
    while frontier:
        for child in children.get(frontier.pop(), ()):
            if child not in reached:
                reached.add(child)
                frontier.append(child)
    assert item.node_key in reached, connector


def test_a_jira_issue_is_parented_by_its_parent_issue_not_by_its_project() -> None:
    """Jira roots the structure chain at the parent issue, on purpose.

    Advanced Roadmaps lets a parent issue live in another project, so the projector
    deliberately files no membership edge for it: chaining it from *this* issue's project
    would give it a second, wrong project, and where the parent really is in this project
    its own projection files it anyway. The consequence is that one batch cannot be walked
    from project to issue over PARENT_OF -- the join happens across batches, on the parent
    issue's node key -- which is why Jira is excluded from the walk above rather than
    quietly passing it.
    """

    extra, source_item_id = CASES["jira"]
    graph = _project("jira", extra, source_item_id=source_item_id)
    nodes = {node.node_key: node for node in graph.nodes}
    issue = _document_item(graph)

    def key(entity_type: GraphEntityType, logical_id: str) -> str:
        return next(
            node.node_key
            for node in graph.nodes
            if node.entity_type is entity_type and node.logical_id == logical_id
        )

    project = key(GraphEntityType.JIRA_PROJECT, "ENG")
    parent_issue = key(GraphEntityType.JIRA_ISSUE, "ENG-1")

    assert (project, issue.node_key) in _edges(graph, RelationType.CONTAINS)
    assert (parent_issue, issue.node_key) in _edges(graph, RelationType.PARENT_OF)
    # The parent placeholder is on neither axis under this project.
    assert parent_issue not in {
        target
        for _, target in _edges(graph, RelationType.CONTAINS)
        | _edges(graph, RelationType.PARENT_OF)
    }
    assert nodes[parent_issue].attributes.get("placeholder") is True


@pytest.mark.parametrize("connector", sorted(CASES))
def test_membership_and_structure_both_reach_the_document(connector: str) -> None:
    """Neither axis is derivable from the other, so the document sits on both."""

    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)

    contained = {target for _, target in _edges(graph, RelationType.CONTAINS)}
    assert _document_item(graph).node_key in contained, connector
