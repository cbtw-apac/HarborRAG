from __future__ import annotations

import pytest
from retrieval_test_support import FakeGraphRepository
from retrieval_test_support import (
    policy as _policy,
)
from retrieval_test_support import (
    resources as _resources,
)

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_runtime.retrieval import (
    RetrievalOptions,
    RuntimeRetrievalService,
)


def _document_node(node_key: str, node_kind: KnowledgeNodeKind, **overrides) -> GraphNodeRecord:
    defaults = {
        "node_key": node_key,
        "node_kind": node_kind,
        "entity_type": GraphEntityType.CHUNK,
        "logical_id": node_key,
        "ownership_scope": GraphOwnershipScope.DOCUMENT_VERSION,
        "owner_id": "tenant-1",
        "source_scope_id": "scope-1",
        "document_id": "document-1",
        "document_version_id": "version-1",
    }
    return GraphNodeRecord(**{**defaults, **overrides})


def _document_relation(
    relation_id: str, relation_type: RelationType, source: str, target: str
) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=relation_type,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id="document-1",
        document_version_id="version-1",
        source_relation_version="graph-v1",
        source_explicit=False,
    )


class DocumentGraphRepository(FakeGraphRepository):
    """Returns a real 2-hop neighborhood: chunk-1 -SUPPORTS-> structure-1 <-CONTAINS- docversion-1."""

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        chunk = _document_node("chunk-1", KnowledgeNodeKind.CHUNK)
        structure = _document_node(
            "structure-1",
            KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            title="Rollback steps",
        )
        document_version = _document_node(
            "docversion-1",
            KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            title="Runbook",
        )
        return KnowledgeGraphTraversal(
            nodes=(chunk, structure, document_version),
            relations=(
                _document_relation(
                    "relation-supports", RelationType.SUPPORTS, "chunk-1", "structure-1"
                ),
                _document_relation(
                    "relation-contains", RelationType.CONTAINS, "docversion-1", "structure-1"
                ),
            ),
        )


@pytest.mark.asyncio
async def test_graph_documents_reports_the_related_result_and_its_neighborhood() -> None:
    """graph_documents must say which vector result it came from and how, not just what it is."""

    graph = DocumentGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(graph=graph),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True),
    )

    [document] = report.diagnostics.graph_documents
    assert document.document_id == "document-1"
    assert document.title == "Runbook"
    assert document.sections == ("Rollback steps",)

    [neighborhood] = document.related_results
    assert neighborhood.result_id == "chunk-1"
    assert {node["node_key"] for node in neighborhood.nodes} == {
        "chunk-1",
        "structure-1",
        "docversion-1",
    }
    assert {relation["relation_type"] for relation in neighborhood.relations} == {
        RelationType.SUPPORTS.value,
        RelationType.CONTAINS.value,
    }


class CrossDocumentGraphRepository(FakeGraphRepository):
    """Two documents joined by one cross-document LINKS_TO relation from doc-1's structure."""

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        chunk = _document_node("chunk-1", KnowledgeNodeKind.CHUNK)
        structure = _document_node(
            "structure-1",
            KnowledgeNodeKind.STRUCTURE,
            entity_type=GraphEntityType.SECTION,
            title="Rollback steps",
        )
        document_version = _document_node(
            "docversion-1",
            KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            title="Runbook",
        )
        other_document_version = _document_node(
            "docversion-2",
            KnowledgeNodeKind.DOCUMENT_VERSION,
            entity_type=GraphEntityType.DOCUMENT_VERSION,
            title="Release Notes",
            document_id="document-2",
            document_version_id="version-2",
        )
        return KnowledgeGraphTraversal(
            nodes=(chunk, structure, document_version, other_document_version),
            relations=(
                _document_relation(
                    "relation-supports", RelationType.SUPPORTS, "chunk-1", "structure-1"
                ),
                _document_relation(
                    "relation-contains", RelationType.CONTAINS, "docversion-1", "structure-1"
                ),
                _document_relation(
                    "relation-links", RelationType.LINKS_TO, "structure-1", "docversion-2"
                ),
            ),
        )


@pytest.mark.asyncio
async def test_graph_documents_preserves_cross_document_connecting_edge() -> None:
    """A relation crossing a document boundary must still show up in both neighborhoods.

    Requiring both endpoints inside one document silently dropped the connecting edge,
    leaving the document reached only through a cross-document link with no way to show
    how the result got there.
    """

    graph = CrossDocumentGraphRepository()
    service = RuntimeRetrievalService(
        resources=_resources(graph=graph),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True),
    )

    documents = {document.document_id: document for document in report.diagnostics.graph_documents}
    assert set(documents) == {"document-1", "document-2"}

    [other_neighborhood] = documents["document-2"].related_results
    assert other_neighborhood.result_id == "chunk-1"
    assert {relation["relation_type"] for relation in other_neighborhood.relations} == {
        RelationType.LINKS_TO.value,
    }
    assert {node["node_key"] for node in other_neighborhood.nodes} == {
        "docversion-2",
        "structure-1",
    }

    [origin_neighborhood] = documents["document-1"].related_results
    assert RelationType.LINKS_TO.value in {
        relation["relation_type"] for relation in origin_neighborhood.relations
    }
    assert "docversion-2" in {node["node_key"] for node in origin_neighborhood.nodes}
