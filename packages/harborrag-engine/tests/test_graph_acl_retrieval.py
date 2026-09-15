"""Permission isolation for structural graph traversal."""

from __future__ import annotations

import pytest
from test_graph_retrieval import ActiveVersions, NoActiveVersions, Repository

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import GraphSubgraphQuery, GraphTripletQuery
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import AuthoritativeGraphSearch


class ScopedAuthorizer:
    def __init__(self, *, documents=(), sources=()):
        self.documents = set(documents)
        self.sources = set(sources)

    async def authorized_document_ids(self, tenant_id, document_ids, *, access):
        del tenant_id, access
        return set(document_ids) & self.documents

    async def authorized_source_scope_ids(self, tenant_id, source_scope_ids, *, access):
        del tenant_id, access
        return set(source_scope_ids) & self.sources


def _source(key: str, scope: str) -> GraphNodeRecord:
    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.SOURCE_ENTITY,
        entity_type=GraphEntityType.GENERIC_SOURCE_ITEM,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id=scope,
        title=key,
    )


def _link(
    relation_id: str,
    source: GraphNodeRecord,
    target: GraphNodeRecord,
    scope: str,
) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=RelationType.HAS_DATA_SOURCE,
        source_node_key=source.node_key,
        target_node_key=target.node_key,
        ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
        owner_id="tenant-1",
        source_scope_id=scope,
        source_relation_version="v1",
        source_explicit=False,
    )


@pytest.mark.asyncio
async def test_graph_acl_drops_denied_items_without_leaking_them_in_diagnostics() -> None:
    search = AuthoritativeGraphSearch(
        Repository(),
        ActiveVersions(),
        ScopedAuthorizer(),  # type: ignore[arg-type]
    )

    result = await search.triplets(
        GraphTripletQuery(subject="document-1"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert result.triplets == ()
    assert result.diagnostics.candidate_count == 0
    assert result.diagnostics.stale_count == 0
    assert result.diagnostics.unpublished_count == 0


@pytest.mark.asyncio
async def test_subgraph_never_connects_visible_nodes_through_a_hidden_intermediate() -> None:
    visible_a = _source("A", "visible")
    hidden_b = _source("B", "hidden")
    visible_c = _source("C", "visible")
    relations = (
        _link("A-B", visible_a, hidden_b, "hidden"),
        _link("B-C", hidden_b, visible_c, "hidden"),
    )

    class HiddenBridgeRepository(Repository):
        async def expand_subgraph(self, query, *, context):
            del query, context
            return KnowledgeGraphTraversal(
                nodes=(visible_a, hidden_b, visible_c), relations=relations
            )

    search = AuthoritativeGraphSearch(
        HiddenBridgeRepository(),
        NoActiveVersions(),
        ScopedAuthorizer(sources=("visible",)),  # type: ignore[arg-type]
    )
    result = await search.subgraph(
        GraphSubgraphQuery(start_node="A", max_nodes=20),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert [item.node_key for item in result.graph.nodes] == ["A"]
    assert result.graph.relations == ()
    assert result.diagnostics.candidate_count == 2
