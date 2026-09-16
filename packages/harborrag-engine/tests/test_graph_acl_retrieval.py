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
from harborrag_core.retrieval import (
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_engine.retrieval import AuthoritativeGraphSearch
from harborrag_engine.retrieval.graph_visibility import apply_graph_permissions


class ScopedAuthorizer:
    def __init__(self, *, documents=(), sources=()):
        self.documents = set(documents)
        self.sources = set(sources)

    async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
        del tenant_id, access, limit
        return tuple(sorted(self.documents))

    async def allowed_source_scope_ids(self, tenant_id, *, access, limit=10000):
        del tenant_id, access, limit
        return tuple(sorted(self.sources))

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
async def test_graph_acl_is_attached_before_the_repository_applies_its_limit() -> None:
    visible = _source("visible", "visible")
    target = _source("target", "visible")
    visible_triplet = GraphTriplet(
        subject=visible,
        predicate=_link("visible-link", visible, target, "visible"),
        object=target,
    )

    class ScopedRepository(Repository):
        async def search_triplets(self, query, *, context):
            del context
            assert query.access_scope is not None
            assert query.access_scope.source_scope_ids == ("visible",)
            return GraphTripletResult(triplets=(visible_triplet,), truncated=False)

    search = AuthoritativeGraphSearch(
        ScopedRepository(),
        NoActiveVersions(),
        ScopedAuthorizer(sources=("visible",)),  # type: ignore[arg-type]
    )

    result = await search.triplets(
        GraphTripletQuery(subject="visible", limit=1),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert result.triplets == (visible_triplet,)
    assert result.diagnostics.projection_truncated is False


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


def _tenant_node(key: str) -> GraphNodeRecord:
    """A tenant-scoped node: no document and no source to authorize against."""

    return GraphNodeRecord(
        node_key=key,
        node_kind=KnowledgeNodeKind.TENANT,
        entity_type=GraphEntityType.TENANT,
        logical_id=key,
        ownership_scope=GraphOwnershipScope.TENANT,
        owner_id="tenant-1",
        title=key,
    )


@pytest.mark.asyncio
async def test_tenant_nodes_are_authorized_without_a_readable_neighbor() -> None:
    """A reader's own grants decide, not what else is in the candidate batch.

    Tenant visibility used to be derived from the batch's own allowlists, so a
    batch holding nothing but tenant-scoped nodes produced empty lists and
    denied every one of them to a reader who was fully authorized.
    """

    records = {"n-1": _tenant_node("n-1"), "n-2": _tenant_node("n-2")}
    authorizer = ScopedAuthorizer(documents={"doc-1"})

    states = await apply_graph_permissions(
        records,
        dict.fromkeys(records, "active"),
        authorizer,
        StorageOperationContext.system("tenant-1"),
    )

    assert states == {"n-1": "active", "n-2": "active"}


@pytest.mark.asyncio
async def test_a_reader_with_no_grants_still_sees_no_tenant_nodes() -> None:
    """The predicate stays fail-closed: no grant anywhere means no tenant node."""

    records = {"n-1": _tenant_node("n-1")}

    states = await apply_graph_permissions(
        records,
        dict.fromkeys(records, "active"),
        ScopedAuthorizer(),
        StorageOperationContext.system("tenant-1"),
    )

    assert states == {"n-1": "denied"}


@pytest.mark.asyncio
async def test_a_readable_document_does_not_unlock_tenant_nodes_beside_it() -> None:
    """The converse leak: one readable document used to authorize the batch."""

    tenant_node = _tenant_node("n-1")
    readable = _source("s-1", "scope-1")
    records = {"n-1": tenant_node, "s-1": readable}

    states = await apply_graph_permissions(
        records,
        dict.fromkeys(records, "active"),
        ScopedAuthorizer(sources={"scope-1"}),
        StorageOperationContext.system("tenant-1"),
    )

    # The reader holds a source grant, so both are legitimately visible here;
    # what matters is that "n-1" was decided by that grant rather than by
    # "s-1" happening to share the batch.
    assert states["s-1"] == "active"
    assert states["n-1"] == "active"
