from __future__ import annotations

from harborrag_core.domain.document import DocumentRelation
from harborrag_core.domain.element import DocumentElement
from harborrag_core.ingestion import (
    GraphEntityType,
    GraphOwnershipScope,
    KnowledgeNodeKind,
)
from harborrag_engine.ingestion import (
    GraphDocumentTarget,
    GraphProjectionBuilder,
    GraphProjectionInput,
)

from .chunking_helpers import make_document, make_profile, make_request, make_service


def test_graph_projection_builds_structure_and_resolved_source_edges() -> None:
    document = make_document(
        [
            DocumentElement("h1", "heading", "Operations", {"level": 1}),
            DocumentElement("p1", "paragraph", "Run the worker."),
            DocumentElement("table-1", "table", "Mode\tTimeout\nprod\t30\n"),
        ]
    )
    document.relations = [
        DocumentRelation(
            predicate="links_to",
            target_id="target-page",
            target_type="document",
        ),
        DocumentRelation(
            predicate="parent_of",
            target_id="target-page",
            target_type="document",
        ),
        DocumentRelation(
            predicate="links_to",
            target_id="not-published",
            target_type="document",
        ),
    ]
    chunking = make_service(
        make_profile(target=40, maximum=60),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(document))

    projection = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunking.chunks,
            resolved_targets={
                "target-page": GraphDocumentTarget(
                    source_item_id="target-page",
                    document_id="document-target",
                    document_version_id="document-target-version",
                    source_scope_id="tenant-1",
                    title=None,
                )
            },
            graph_projection_version="graph-v1",
        )
    )

    kinds = {node.node_kind for node in projection.nodes}
    relation_types = [relation.relation_type.value for relation in projection.relations]
    assert KnowledgeNodeKind.TENANT in kinds
    assert KnowledgeNodeKind.DATA_SOURCE in kinds
    assert KnowledgeNodeKind.SOURCE_ENTITY in kinds
    assert KnowledgeNodeKind.DOCUMENT_VERSION in kinds
    assert KnowledgeNodeKind.CHUNK in kinds
    assert KnowledgeNodeKind.STRUCTURE in kinds
    document_node = next(
        node
        for node in projection.nodes
        if node.node_kind == KnowledgeNodeKind.DOCUMENT_VERSION
    )
    section_node = next(
        node for node in projection.nodes if node.entity_type == GraphEntityType.SECTION
    )
    table_node = next(
        node for node in projection.nodes if node.entity_type == GraphEntityType.TABLE
    )
    assert document_node.title == "HarborRAG"
    assert document_node.attributes["source_item_id"]
    assert document_node.attributes["connector_type"]
    assert section_node.section_path == ("Operations",)
    assert table_node.title == "Table — Operations"
    chunk_nodes = [node for node in projection.nodes if node.node_kind == KnowledgeNodeKind.CHUNK]
    assert chunk_nodes
    assert all(node.node_key == node.logical_id for node in chunk_nodes)
    assert all("content_preview" not in node.model_dump() for node in projection.nodes)
    target_node = next(
        node for node in projection.nodes if node.logical_id == "target-page"
    )
    assert target_node.title == "target-page"
    assert target_node.ownership_scope == GraphOwnershipScope.SOURCE_SCOPE
    assert "has_data_source" in relation_types
    assert "has_version" in relation_types
    assert "contains" in relation_types
    assert "supports" in relation_types
    assert "links_to" in relation_types
    assert "parent_of" in relation_types
    assert projection.unresolved_relations[0].target_source_item_id == "not-published"
    assert all(
        relation.document_version_id == "document-version:1"
        for relation in projection.relations
        if relation.ownership_scope == GraphOwnershipScope.DOCUMENT_VERSION
    )
    assert all(node.graph_schema_version == "2.0" for node in projection.nodes)
    assert all(relation.graph_schema_version == "2.0" for relation in projection.relations)
    assert projection.manifest.document_id == chunking.chunks[0].document_id
    assert projection.manifest.document_version_id == chunking.chunks[0].document_version_id
    assert projection.manifest.node_keys == tuple(node.node_key for node in projection.nodes)
    assert projection.manifest.relation_ids == tuple(
        relation.relation_id for relation in projection.relations
    )
    assert len(projection.manifest.payload_sha256) == 64


def test_structural_projection_defers_active_target_resolution() -> None:
    document = make_document(
        [DocumentElement("p1", "paragraph", "See the release runbook.")],
    )
    document.relations = [
        DocumentRelation(
            predicate="links_to",
            target_id="target-page",
            target_type="document",
        )
    ]
    chunks = (
        make_service(
            make_profile(target=40, maximum=60),
            configuration_version="3",
            create_route_chunks=True,
        )
        .chunk(make_request(document))
        .chunks
    )
    builder = GraphProjectionBuilder()

    projection = builder.build_structural(
        document=document,
        chunks=chunks,
        graph_projection_version="graph-stable-links",
    )

    assert projection == builder.build_structural(
        document=document,
        chunks=chunks,
        graph_projection_version="graph-stable-links",
    )
    assert any(
        relation.source_explicit and relation.relation_type.value == "links_to"
        for relation in projection.relations
    )
    assert projection.unresolved_relations[0].target_source_item_id == "target-page"


def test_graph_projection_models_comments_replies_and_section_targets() -> None:
    document = make_document(
        [DocumentElement("p1", "paragraph", "Issue description")],
        source="jira",
        record_id="HARBOR-42",
        extra={
            "issue_key": "HARBOR-42",
            "comments": [
                {"id": "comment-1", "body": "First observation"},
                {
                    "id": "comment-2",
                    "body": "Follow-up observation",
                    "parent_comment_id": "comment-1",
                },
            ],
        },
    )
    chunking = make_service(
        make_profile(name="jira", strategy="jira", target=80, maximum=100),
        configuration_version="3",
        create_route_chunks=True,
    ).chunk(make_request(document))

    projection = GraphProjectionBuilder().build(
        GraphProjectionInput(
            document=document,
            chunks=chunking.chunks,
            resolved_targets={},
            graph_projection_version="graph-v1",
        )
    )

    comments = [
        node for node in projection.nodes if node.entity_type == GraphEntityType.COMMENT
    ]
    relation_types = {relation.relation_type.value for relation in projection.relations}
    assert {node.logical_id for node in comments} == {"comment-1", "comment-2"}
    assert {node.title for node in comments} == {
        "Comment comment-1",
        "Comment comment-2",
    }
    assert {"contains", "links_to", "reply_to"} <= relation_types
    reply = next(
        relation for relation in projection.relations if relation.relation_type.value == "reply_to"
    )
    assert "evidence_chunk_ids" not in reply.model_dump()
