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


@pytest.mark.parametrize("connector", sorted(CASES))
def test_a_container_never_holds_an_attachment(connector: str) -> None:
    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)
    nodes = {node.node_key: node for node in graph.nodes}

    held = {
        nodes[edge.target_node_key].entity_type
        for edge in graph.relations
        if edge.relation_type is RelationType.CONTAINS
    }
    assert not (held & _ATTACHMENT_TYPES), connector


@pytest.mark.parametrize("connector", sorted(CASES))
def test_the_structure_axis_reaches_the_item_from_its_container(connector: str) -> None:
    """PARENT_OF must be walkable top-down, whether or not anything sits in between."""

    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)

    parented = {
        edge.target_node_key
        for edge in graph.relations
        if edge.relation_type is RelationType.PARENT_OF
    }
    item = next(
        node
        for node in graph.nodes
        if node.node_kind is KnowledgeNodeKind.SOURCE_ENTITY
        and any(
            edge.source_node_key == node.node_key and edge.relation_type is RelationType.HAS_VERSION
            for edge in graph.relations
        )
    )
    assert item.node_key in parented, connector


@pytest.mark.parametrize("connector", sorted(CASES))
def test_membership_and_structure_both_reach_the_document(connector: str) -> None:
    """Neither axis is derivable from the other, so the document sits on both."""

    extra, source_item_id = CASES[connector]
    graph = _project(connector, extra, source_item_id=source_item_id)

    contained = {
        edge.target_node_key
        for edge in graph.relations
        if edge.relation_type is RelationType.CONTAINS
    }
    item = next(
        node
        for node in graph.nodes
        if node.node_kind is KnowledgeNodeKind.SOURCE_ENTITY
        and any(
            edge.source_node_key == node.node_key and edge.relation_type is RelationType.HAS_VERSION
            for edge in graph.relations
        )
    )
    assert item.node_key in contained, connector
