"""Tests for seeding graph observation from entities memory already established."""

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
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.graph_observation import _MEMORY_SEED_LIMIT

_ENTITY_TYPES = {
    KnowledgeNodeKind.CHUNK: GraphEntityType.CHUNK,
    KnowledgeNodeKind.STRUCTURE: GraphEntityType.SECTION,
    KnowledgeNodeKind.DOCUMENT_VERSION: GraphEntityType.DOCUMENT_VERSION,
}


def _node(node_key: str, kind: KnowledgeNodeKind, *, document_id: str, title: str | None = None):
    return GraphNodeRecord(
        node_key=node_key,
        node_kind=kind,
        entity_type=_ENTITY_TYPES[kind],
        logical_id=node_key,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id=document_id,
        document_version_id=f"version-{document_id}",
        title=title,
    )


def _relation(relation_id: str, source: str, target: str, document_id: str) -> GraphEdgeRecord:
    return GraphEdgeRecord(
        relation_id=relation_id,
        relation_type=RelationType.SUPPORTS,
        source_node_key=source,
        target_node_key=target,
        ownership_scope=GraphOwnershipScope.DOCUMENT_VERSION,
        owner_id="tenant-1",
        source_scope_id="scope-1",
        document_id=document_id,
        document_version_id=f"version-{document_id}",
        source_relation_version="graph-v1",
        source_explicit=False,
    )


class _SeededGraph(FakeGraphRepository):
    """The result chunk sits in document-1; the memory-seeded node in document-2."""

    async def traverse(self, start_node_key, **kwargs):
        self.queries.append((start_node_key, kwargs))
        if start_node_key == "chunk-1":
            return KnowledgeGraphTraversal(
                nodes=(
                    _node("chunk-1", KnowledgeNodeKind.CHUNK, document_id="document-1"),
                    _node(
                        "structure-1",
                        KnowledgeNodeKind.STRUCTURE,
                        document_id="document-1",
                        title="Rollback steps",
                    ),
                ),
                relations=(_relation("relation-1", "chunk-1", "structure-1", "document-1"),),
            )
        return KnowledgeGraphTraversal(
            nodes=(
                _node(
                    "node-atlas",
                    KnowledgeNodeKind.DOCUMENT_VERSION,
                    document_id="document-2",
                    title="Atlas Migration",
                ),
            ),
            relations=(),
        )


def _service(graph: FakeGraphRepository) -> RuntimeRetrievalService:
    return RuntimeRetrievalService(resources=_resources(graph=graph), policy=_policy())


@pytest.mark.asyncio
async def test_memory_seeds_widen_the_walk_beyond_the_vector_results() -> None:
    graph = _SeededGraph()

    report = await _service(graph).retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True, graph_seeds=("node-atlas",)),
    )

    # Both the result chunk and the memory-established entity were walked.
    assert [key for key, _ in graph.queries] == ["chunk-1", "node-atlas"]
    documents = {document.document_id for document in report.diagnostics.graph_documents}
    assert documents == {"document-1", "document-2"}
    assert report.diagnostics.graph_nodes == 3


@pytest.mark.asyncio
async def test_a_memory_seeded_document_carries_no_related_result() -> None:
    """``result_id`` is contractually a vector result's chunk id, never a memory seed."""

    report = await _service(_SeededGraph()).retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=True, graph_seeds=("node-atlas",)),
    )
    documents = {document.document_id: document for document in report.diagnostics.graph_documents}

    assert documents["document-2"].related_results == ()
    assert documents["document-2"].title == "Atlas Migration"
    assert [item.result_id for item in documents["document-1"].related_results] == ["chunk-1"]


@pytest.mark.asyncio
async def test_seeds_are_deduplicated_against_the_results_blanks_dropped_and_capped() -> None:
    graph = _SeededGraph()

    await _service(graph).retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(
            observe_graph=True,
            graph_seeds=(
                "chunk-1",
                "  ",
                "node-atlas",
                " node-atlas ",
                *(f"node-{index}" for index in range(20)),
            ),
        ),
    )
    walked = [key for key, _ in graph.queries]

    # The result's own chunk is walked once, not twice as a seed.
    assert walked.count("chunk-1") == 1
    assert walked.count("node-atlas") == 1
    assert len(walked) == 1 + _MEMORY_SEED_LIMIT


@pytest.mark.asyncio
async def test_seeds_are_inert_when_graph_observation_is_off() -> None:
    graph = _SeededGraph()

    report = await _service(graph).retrieve(
        "release",
        tenant_id="tenant-1",
        options=RetrievalOptions(observe_graph=False, graph_seeds=("node-atlas",)),
    )

    assert graph.queries == []
    assert report.diagnostics.graph_nodes == 0
