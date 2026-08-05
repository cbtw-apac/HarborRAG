from __future__ import annotations

import pytest

from harborrag_core.chunking import RelationType
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    GraphEdgeRecord,
    GraphEntityType,
    GraphNodeRecord,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
    KnowledgeNodeKind,
)
from harborrag_core.retrieval import (
    GraphNeighborhoodQuery,
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
        node_kind=KnowledgeNodeKind.DOCUMENT_VERSION,
        entity_type=GraphEntityType.DOCUMENT_VERSION,
        logical_id=version,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        document_id="document-1",
        document_version_id=version,
        source_scope_id="scope-1",
    )


def triplet(version: str) -> GraphTriplet:
    subject = node(version)
    object_node = GraphNodeRecord(
        node_key=f"section-{version}",
        node_kind=KnowledgeNodeKind.STRUCTURE,
        entity_type=GraphEntityType.SECTION,
        logical_id="section-1",
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        document_id="document-1",
        document_version_id=version,
        source_scope_id="scope-1",
    )
    relation = GraphEdgeRecord(
        relation_id=f"relation-{version}",
        relation_type=RelationType.CONTAINS,
        source_node_key=subject.node_key,
        target_node_key=object_node.node_key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id="document-1",
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


class StableRepository(Repository):
    async def search_triplets(self, query, *, context):
        del query, context
        tenant = GraphNodeRecord(
            node_key="tenant-1-key",
            node_kind=KnowledgeNodeKind.TENANT,
            entity_type=GraphEntityType.TENANT,
            logical_id="tenant-1",
            ownership_scope=GraphOwnershipScope.TENANT,
            owner_id="tenant-1",
        )
        data_source = GraphNodeRecord(
            node_key="source-1-key",
            node_kind=KnowledgeNodeKind.DATA_SOURCE,
            entity_type=GraphEntityType.DATA_SOURCE,
            logical_id="scope-1",
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
        )
        relation = GraphEdgeRecord(
            relation_id="has-source-1",
            relation_type=RelationType.HAS_DATA_SOURCE,
            source_node_key=tenant.node_key,
            target_node_key=data_source.node_key,
            ownership_scope=GraphOwnershipScope.SOURCE_SCOPE,
            owner_id="tenant-1",
            source_scope_id="scope-1",
            source_relation_version="2.0",
            source_explicit=False,
        )
        return GraphTripletResult(
            triplets=(GraphTriplet(subject=tenant, predicate=relation, object=data_source),)
        )


class NoActiveVersions:
    async def active_versions(self, document_ids):
        assert tuple(document_ids) == ()
        return {}


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


@pytest.mark.asyncio
async def test_stable_tenant_and_source_nodes_are_visible_without_document_versions() -> None:
    search = AuthoritativeGraphSearch(StableRepository(), NoActiveVersions())  # type: ignore[arg-type]

    result = await search.triplets(
        GraphTripletQuery(subject="tenant-1"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(result.triplets) == 1
    assert result.diagnostics.stale_count == 0
    assert result.diagnostics.unpublished_count == 0


def test_graph_query_contracts_reject_unbounded_or_ambiguous_requests() -> None:
    with pytest.raises(ValueError, match="requires"):
        GraphTripletQuery()
    with pytest.raises(ValueError, match="different"):
        GraphPathQuery(start_node="same", end_node="same")


class WideningRepository(Repository):
    """Answers with exactly as many nodes as it was asked for, all but one stale."""

    def __init__(self) -> None:
        super().__init__()
        self.requested_max_nodes: int | None = None

    async def expand_subgraph(self, query, *, context):
        del context
        self.requested_max_nodes = query.max_nodes
        active = triplet("version-active")
        stale = [triplet(f"version-retired-{index}") for index in range(query.max_nodes - 2)]
        return KnowledgeGraphTraversal(
            nodes=(active.subject, active.object, *(item.subject for item in stale)),
            relations=(active.predicate,),
        )


@pytest.mark.asyncio
async def test_subgraph_widens_the_request_before_dropping_stale_nodes() -> None:
    """Post-filtering an unwidened request silently returns an inactive-heavy neighborhood.

    Without over-fetching, a caller asking for 20 nodes in a neighborhood dominated by
    superseded versions gets a handful back with truncated=False, which is
    indistinguishable from a genuinely small neighborhood.
    """

    repository = WideningRepository()
    search = AuthoritativeGraphSearch(repository, ActiveVersions())  # type: ignore[arg-type]

    result = await search.subgraph(
        GraphSubgraphQuery(start_node="document-1", max_nodes=5),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert repository.requested_max_nodes == 20
    assert {item.document_version_id for item in result.graph.nodes} == {"version-active"}


@pytest.mark.asyncio
async def test_subgraph_reports_truncation_when_active_nodes_were_cut() -> None:
    class ManyActive(Repository):
        async def expand_subgraph(self, query, *, context):
            del query, context
            active = triplet("version-active")
            return KnowledgeGraphTraversal(
                nodes=(active.subject, active.object),
                relations=(active.predicate,),
            )

    search = AuthoritativeGraphSearch(ManyActive(), ActiveVersions())  # type: ignore[arg-type]

    result = await search.subgraph(
        GraphSubgraphQuery(start_node="document-1", max_nodes=1),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert len(result.graph.nodes) == 1
    assert result.graph.truncated is True
    assert result.diagnostics.projection_truncated is True


@pytest.mark.asyncio
async def test_neighborhood_merges_seed_expansions_without_duplicates() -> None:
    class CountingRepository(Repository):
        def __init__(self) -> None:
            super().__init__()
            self.seeds: list[str] = []

        async def expand_subgraph(self, query, *, context):
            del context
            self.seeds.append(query.start_node)
            active = triplet("version-active")
            return KnowledgeGraphTraversal(
                nodes=(active.subject, active.object),
                relations=(active.predicate,),
            )

    repository = CountingRepository()
    search = AuthoritativeGraphSearch(repository, ActiveVersions())  # type: ignore[arg-type]

    result = await search.neighborhood(
        ("chunk:a", "chunk:b"),
        GraphNeighborhoodQuery(query="anything"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert repository.seeds == ["chunk:a", "chunk:b"]
    # Both seeds expand into the same two nodes; the merge must not double them.
    assert len(result.graph.nodes) == 2
    assert len(result.graph.relations) == 1


@pytest.mark.asyncio
async def test_neighborhood_without_seeds_returns_an_empty_graph() -> None:
    search = AuthoritativeGraphSearch(Repository(), ActiveVersions())  # type: ignore[arg-type]

    result = await search.neighborhood(
        (),
        GraphNeighborhoodQuery(query="nothing matches"),
        context=StorageOperationContext.system("tenant-1"),
    )

    assert result.graph.nodes == ()
    assert result.diagnostics.accepted_count == 0


def test_path_search_defaults_to_an_undirected_walk() -> None:
    # A chunk reaches its document only by traversing SUPPORTS backwards and CONTAINS
    # forwards, so an outgoing-only default answers the most natural question with silence.
    assert GraphPathQuery(start_node="a", end_node="b").direction == "both"
