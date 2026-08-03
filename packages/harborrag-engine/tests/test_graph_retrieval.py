from __future__ import annotations

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    GraphEdgeRecord,
    GraphNodeRecord,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import AuthoritativeGraphSearch


def node(version: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=f"node-{version}",
        node_kind=KnowledgeNodeKind.DOCUMENT,
        logical_id="document-1",
        document_id="document-1",
        document_version_id=version,
        source_scope_id="scope-1",
    )


def triplet(version: str) -> GraphTriplet:
    subject = node(version)
    object_node = GraphNodeRecord(
        node_key=f"section-{version}",
        node_kind=KnowledgeNodeKind.SECTION,
        logical_id="section-1",
        document_id="document-1",
        document_version_id=version,
        source_scope_id="scope-1",
    )
    relation = GraphEdgeRecord(
        relation_id=f"relation-{version}",
        relation_type=RelationType.HAS_SECTION,
        source_node_key=subject.node_key,
        target_node_key=object_node.node_key,
        document_version_id=version,
        source_relation_version="source-v1",
        source_explicit=False,
    )
    return GraphTriplet(subject=subject, predicate=relation, object=object_node)


class Repository:
    def __init__(self) -> None:
        self.query = None

    async def search_triplets(self, query, *, context):
        self.query = query
        return GraphTripletResult(
            triplets=(triplet("version-active"), triplet("version-retired")),
        )

    async def find_paths(self, query, *, context):
        del query, context
        return GraphPathResult(paths=())

    async def expand_subgraph(self, query, *, context):
        del query, context
        active = triplet("version-active")
        stale = triplet("version-retired")
        return KnowledgeGraphTraversal(
            nodes=(active.subject, active.object, stale.subject, stale.object),
            relations=(active.predicate, stale.predicate),
        )


class ActiveVersions:
    async def active_versions(self, document_ids):
        assert tuple(document_ids) == ("document-1",)
        return {
            "document-1": ActiveDocumentVersion(
                document_id="document-1",
                document_version_id="version-active",
            )
        }


@pytest.mark.asyncio
async def test_triplet_search_rejects_retired_projection_records() -> None:
    repository = Repository()
    search = AuthoritativeGraphSearch(repository, ActiveVersions())  # type: ignore[arg-type]

    result = await search.triplets(
        GraphTripletQuery(subject="document-1", limit=1),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert [item.predicate.relation_id for item in result.triplets] == ["relation-version-active"]
    assert result.diagnostics.stale_count == 1
    assert repository.query.limit == 4


@pytest.mark.asyncio
async def test_subgraph_removes_stale_nodes_and_relations() -> None:
    search = AuthoritativeGraphSearch(Repository(), ActiveVersions())  # type: ignore[arg-type]

    result = await search.subgraph(
        GraphSubgraphQuery(start_node="document-1"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert {item.document_version_id for item in result.graph.nodes} == {"version-active"}
    assert [item.relation_id for item in result.graph.relations] == ["relation-version-active"]
    assert result.diagnostics.stale_count == 2


def test_graph_query_contracts_reject_unbounded_or_ambiguous_requests() -> None:
    with pytest.raises(ValueError, match="requires"):
        GraphTripletQuery()
    with pytest.raises(ValueError, match="different"):
        GraphPathQuery(start_node="same", end_node="same")
