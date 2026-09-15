"""Source assertions have document lifetimes even when their endpoints do not."""

from __future__ import annotations

import pytest

from harborrag_core.chunking import ConnectorType, DocumentKind, RelationType
from harborrag_core.ingestion import (
    ActiveDocumentVersion,
    GraphEntityType,
    GraphOwnershipScope,
    KnowledgeGraphTraversal,
)
from harborrag_core.retrieval import (
    GraphPath,
    GraphPathQuery,
    GraphPathResult,
    GraphSubgraphQuery,
    GraphTriplet,
    GraphTripletQuery,
    GraphTripletResult,
)
from harborrag_core.storage import StorageOperationContext
from harborrag_core.testing.graph_fakes import FakeKnowledgeGraphRepository
from harborrag_engine.ingestion.projections.graph.graph_state import (
    GraphProjectionContext,
    GraphProjectionState,
    GraphRelationSpec,
)
from harborrag_engine.retrieval import AuthoritativeGraphSearch

CONTEXT = StorageOperationContext.system("tenant")


def projection(
    document: str, version: str, *, title: str = "Published", explicit: bool = True
):
    state = GraphProjectionState(
        GraphProjectionContext(
            tenant_id="tenant",
            connection_id="connection",
            document_id=document,
            document_version_id=version,
            source_scope_id="scope",
            source_relation_version="graph-source-supports-v1",
            connector_type=ConnectorType.CONFLUENCE,
            document_kind=DocumentKind.CONFLUENCE_PAGE,
            source_item_id="a",
            source_uri=None,
        )
    )
    source = state.source_node(GraphEntityType.CONFLUENCE_PAGE, "a", title=title)
    target = state.source_node(GraphEntityType.CONFLUENCE_PAGE, "b", title="Target")
    relation = state.relation(
        GraphRelationSpec(
            relation_type=RelationType.PARENT_OF,
            source=source,
            target=target,
            source_explicit=explicit,
        )
    )
    assert relation is not None
    return source, target, relation


class ServingGraph(FakeKnowledgeGraphRepository):
    async def search_triplets(self, query, *, context):
        del query, context
        return GraphTripletResult(
            triplets=tuple(
                GraphTriplet(
                    subject=self.nodes[edge.source_node_key],
                    predicate=edge,
                    object=self.nodes[edge.target_node_key],
                )
                for edge in self.relations.values()
            )
        )

    async def find_paths(self, query, *, context):
        candidates = await self.search_triplets(query, context=context)
        return GraphPathResult(
            paths=tuple(
                GraphPath(nodes=(item.subject, item.object), relations=(item.predicate,))
                for item in candidates.triplets
            )
        )

    async def expand_subgraph(self, query, *, context):
        del query, context
        return KnowledgeGraphTraversal(
            nodes=tuple(self.nodes.values()), relations=tuple(self.relations.values())
        )


class ActiveVersions:
    def __init__(self, versions: dict[str, str]) -> None:
        self.versions = versions
        self.requested: set[str] = set()

    async def active_versions(self, document_ids):
        self.requested.update(document_ids)
        return {
            document: ActiveDocumentVersion(document_id=document, document_version_id=version)
            for document, version in self.versions.items()
            if document in document_ids
        }


@pytest.mark.asyncio
async def test_shared_hierarchy_projects_one_source_scoped_logical_edge() -> None:
    graph = ServingGraph()
    first = projection("document-a", "a-v1", explicit=False)
    second = projection("document-b", "b-v1", explicit=False)
    assert first[0].node_key == second[0].node_key
    assert first[2].relation_id == second[2].relation_id
    assert first[2].ownership_scope == GraphOwnershipScope.SOURCE_SCOPE
    assert first[2].attributes["logical_view"] is True
    for source, target, edge in (first, second):
        await graph.write_projection((source, target), (edge,), context=CONTEXT)

    await graph.delete_version("a-v1", context=CONTEXT)

    assert tuple(graph.relations) == (second[2].relation_id,)
    assert first[0].node_key in graph.nodes
    versions = ActiveVersions({"document-b": "b-v1"})
    result = await AuthoritativeGraphSearch(graph, versions).triplets(
        GraphTripletQuery(subject="a"), context=CONTEXT
    )
    assert len(result.triplets) == 1
    assert not versions.requested


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["triplets", "paths", "subgraph"])
async def test_unpublished_support_cannot_replace_visible_source_metadata(mode: str) -> None:
    graph = ServingGraph()
    published = projection("document", "v1")
    failed = projection("document", "v2", title="Unpublished secret title")
    for source, target, edge in (published, failed):
        await graph.write_projection((source, target), (edge,), context=CONTEXT)
    versions = ActiveVersions({"document": "v1"})
    search = AuthoritativeGraphSearch(graph, versions)

    if mode == "triplets":
        result = await search.triplets(GraphTripletQuery(subject="a"), context=CONTEXT)
        assert len(result.triplets) == 1
        nodes = (result.triplets[0].subject, result.triplets[0].object)
    elif mode == "paths":
        result = await search.paths(GraphPathQuery(start_node="a", end_node="b"), context=CONTEXT)
        assert len(result.paths) == 1
        nodes = result.paths[0].nodes
    else:
        result = await search.subgraph(GraphSubgraphQuery(start_node="a"), context=CONTEXT)
        assert len(result.graph.relations) == 1
        nodes = result.graph.nodes

    assert versions.requested == {"document"}
    assert {node.title for node in nodes} == {"Published", "Target"}


@pytest.mark.asyncio
async def test_title_selection_cannot_use_unpublished_observation() -> None:
    graph = ServingGraph()
    for source, target, edge in (
        projection("document", "v1"),
        projection("document", "v2", title="Unpublished title"),
    ):
        await graph.write_projection((source, target), (edge,), context=CONTEXT)
    result = await AuthoritativeGraphSearch(graph, ActiveVersions({"document": "v1"})).triplets(
        GraphTripletQuery(subject="Unpublished title"), context=CONTEXT
    )
    assert not result.triplets


@pytest.mark.asyncio
async def test_legacy_source_owned_assertion_fails_closed_until_rebuild() -> None:
    source, target, edge = projection("document", "v1")
    legacy = edge.model_copy(
        update={
            "ownership_scope": GraphOwnershipScope.SOURCE_SCOPE,
            "document_id": None,
            "document_version_id": None,
        }
    )
    graph = ServingGraph()
    await graph.write_projection((source, target), (legacy,), context=CONTEXT)
    result = await AuthoritativeGraphSearch(graph, ActiveVersions({})).triplets(
        GraphTripletQuery(subject="a"), context=CONTEXT
    )
    assert not result.triplets
    assert result.diagnostics.stale_count == 1


@pytest.mark.asyncio
async def test_retired_support_is_hidden_before_physical_cleanup() -> None:
    graph = ServingGraph()
    source, target, edge = projection("document", "v1")
    await graph.write_projection((source, target), (edge,), context=CONTEXT)
    result = await AuthoritativeGraphSearch(graph, ActiveVersions({"document": "v2"})).triplets(
        GraphTripletQuery(subject="a"), context=CONTEXT
    )
    assert not result.triplets
    assert edge.relation_id in graph.relations
