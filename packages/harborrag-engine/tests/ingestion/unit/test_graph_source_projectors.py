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
    chunks = make_service(
        make_profile(target=40, maximum=60),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(make_document(document.content))).chunks
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


def test_confluence_topology_includes_ancestry_and_attachments() -> None:
    graph = _project(
        "confluence",
        {
            "space_id": "space-1",
            "space_key": "ENG",
            "page_id": "page-2",
            "ancestor_ids": ["page-1"],
            "ancestor_titles": ["Parent"],
            "attachments": [{"id": "attachment-1", "title": "Runbook.pdf"}],
        },
        source_item_id="confluence://ENG/page-2",
    )

    assert {
        GraphEntityType.CONFLUENCE_SPACE,
        GraphEntityType.CONFLUENCE_PAGE,
        GraphEntityType.CONFLUENCE_ATTACHMENT,
    } <= {node.entity_type for node in graph.nodes}
    assert {"parent_of", "has_attachment", "has_version"} <= {
        relation.relation_type.value for relation in graph.relations
    }


def test_jira_topology_preserves_parent_and_native_issue_links() -> None:
    graph = _project(
        "jira",
        {
            "project_id": "project-1",
            "project_key": "ENG",
            "issue_key": "ENG-2",
            "parent": {"key": "ENG-1", "summary": "Parent issue"},
        },
        source_item_id="ENG-2",
        relations=[
            DocumentRelation(
                predicate="blocks",
                target_id="ENG-3",
                target_type="issue",
            )
        ],
    )

    assert {GraphEntityType.JIRA_PROJECT, GraphEntityType.JIRA_ISSUE} <= {
        node.entity_type for node in graph.nodes
    }
    assert {"parent_of", "blocks"} <= {
        relation.relation_type.value for relation in graph.relations
    }
    assert graph.unresolved_relations[0].target_source_item_id == "ENG-3"


def test_github_topology_resolves_ref_to_commit_without_file_content() -> None:
    graph = _project(
        "github",
        {
            "owner": "acme",
            "repo": "harbor",
            "repository_id": "repo-1",
            "path": "docs/guide.md",
            "ref": "main",
            "commit_sha": "abcdef1234567890",
        },
        source_item_id="github://acme/harbor/docs/guide.md",
    )

    assert {
        GraphEntityType.GITHUB_OWNER,
        GraphEntityType.GITHUB_REPOSITORY,
        GraphEntityType.GITHUB_DIRECTORY,
        GraphEntityType.GITHUB_FILE,
        GraphEntityType.GITHUB_REF,
        GraphEntityType.GITHUB_COMMIT,
    } <= {node.entity_type for node in graph.nodes}
    assert {"points_to", "resolved_at"} <= {
        relation.relation_type.value for relation in graph.relations
    }


def test_sharepoint_missing_parent_builds_placeholder_folder_hierarchy() -> None:
    graph = _project(
        "sharepoint",
        {
            "site_id": "site-1",
            "site_name": "Engineering",
            "drive_id": "drive-1",
            "drive_name": "Documents",
            "item_id": "file-1",
            "parent": {
                "id": "folder-2",
                "path": "/drives/drive-1/root:/Policies/Security",
            },
        },
        source_item_id="sharepoint://site-1/drive-1/file-1",
    )

    folders = [
        node for node in graph.nodes if node.entity_type == GraphEntityType.SHAREPOINT_FOLDER
    ]
    assert len(folders) == 2
    assert all(node.attributes["placeholder"] is True for node in folders)


def test_local_topology_uses_only_portable_relative_paths() -> None:
    graph = _project(
        "local",
        {"relative_path": "docs/guide.md", "path": "/must/not/persist"},
        source_item_id="docs/guide.md",
    )

    local_nodes = [
        node
        for node in graph.nodes
        if node.entity_type
        in {GraphEntityType.LOCAL_ROOT, GraphEntityType.LOCAL_DIRECTORY, GraphEntityType.LOCAL_FILE}
    ]
    assert local_nodes
    assert all(
        not str(node.attributes.get("relative_path", "")).startswith("/")
        for node in local_nodes
    )
    assert all("path" not in node.attributes for node in graph.nodes)


def test_custom_connector_uses_generic_source_item_fallback() -> None:
    graph = _project(
        "catalog",
        {"provider_id": "item-1"},
        source_item_id="item-1",
    )

    assert GraphEntityType.GENERIC_SOURCE_ITEM in {node.entity_type for node in graph.nodes}


def test_every_connector_hierarchy_descends_from_the_tenant_node() -> None:
    # Each connector adapts the middle of the chain to its own hierarchy, but the spine
    # is invariant: the tenant owns its data sources, and every source entity is reachable
    # from one. A connector projector that forgot to anchor itself would strand its
    # subtree, which no per-connector topology test would catch on its own.
    cases = {
        "confluence": ({"space_id": "space-1", "space_key": "ENG", "page_id": "page-2"}, "page-2"),
        "jira": ({"project_key": "ENG", "issue_key": "ENG-1"}, "ENG-1"),
        "github": ({"owner": "acme", "repo": "core", "relative_path": "a/b.md"}, "a/b.md"),
        "sharepoint": ({"site_id": "site-1", "drive_id": "drive-1"}, "file-1"),
        "local": ({"relative_path": "docs/guide.md"}, "docs/guide.md"),
        "catalog": ({"provider_id": "item-1"}, "item-1"),
    }
    for connector, (extra, source_item_id) in cases.items():
        graph = _project(connector, extra, source_item_id=source_item_id)
        nodes = {node.node_key: node for node in graph.nodes}
        tenants = [
            node for node in graph.nodes if node.node_kind is KnowledgeNodeKind.TENANT
        ]
        assert len(tenants) == 1, connector

        # Undirected, matching how traversals actually run: Chunk points *into* the spine
        # via (:Chunk)-[:SUPPORTS]->(:Structure), so a directed walk from the tenant would
        # strand every chunk.
        edges = {(edge.source_node_key, edge.target_node_key) for edge in graph.relations}
        adjacency = edges | {(target, source) for source, target in edges}
        reachable = {tenants[0].node_key}
        while True:
            grown = reachable | {
                target for source, target in adjacency if source in reachable
            }
            if grown == reachable:
                break
            reachable = grown

        data_sources = [
            edge.target_node_key
            for edge in graph.relations
            if edge.relation_type is RelationType.HAS_DATA_SOURCE
        ]
        assert len(data_sources) == 1, connector
        assert nodes[data_sources[0]].node_kind is KnowledgeNodeKind.DATA_SOURCE, connector

        # Placeholder stubs for cross-document targets are resolved by relation repair and
        # are legitimately unanchored until then; every concrete node must be reachable.
        stranded = {
            node.node_key
            for node in graph.nodes
            if node.node_key not in reachable and not node.attributes.get("placeholder")
        }
        assert not stranded, (connector, stranded)
