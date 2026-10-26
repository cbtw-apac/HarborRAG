"""Generated-context candidates remain raw evidence with independently checked support."""

from __future__ import annotations

from dataclasses import replace

import pytest
from retrieval_test_support import policy, resources
from topology_retrieval_support import ActiveVersions, CanonicalTopology, TopologyVectors

from harborrag_core.indexing import VectorSearchResult
from harborrag_core.topology.search import TopologyEvidence
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService


class ContextualLane:
    def __init__(self, record):
        self.calls = []
        self.record = record

    async def search(self, query, *, context):
        self.calls.append((query, context))
        return (
            (
                VectorSearchResult(
                    id=self.record.id, score=0.8, raw_score=0.8, payload=self.record.payload
                ),
                TopologyEvidence("chunk-2", "document-2", "version-2", ("build-2",)),
            ),
        )


@pytest.mark.asyncio
async def test_exact_derived_artifact_revocation_removes_enrichment_even_while_build_is_live():
    class RevokedArtifact(CanonicalTopology):
        async def eligible_artifact_ids(self, tenant_id, artifact_ids, *, access=None):
            assert artifact_ids == ("artifact-2",)
            return set()

    class ArtifactLane(ContextualLane):
        async def search(self, query, *, context):
            results = await super().search(query, context=context)
            return tuple(
                (candidate, replace(support, derived_artifact_ids=("artifact-2",)))
                for candidate, support in results
            )

    topology = RevokedArtifact()
    topology.mentions = topology.assertions = ()
    vectors = TopologyVectors()
    report = await runtime(topology, ArtifactLane(vectors.records[1]), vectors).retrieve(
        "query", tenant_id="tenant-1", options=RetrievalOptions(mode="local_semantic")
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.evidence.navigation_summaries == ()


def runtime(topology, contextual, vectors):
    return RuntimeRetrievalService(
        resources=replace(
            resources(vectors=vectors, active_versions=ActiveVersions()),
            topology_repository=topology,
            contextual_search=contextual,
        ),
        policy=policy(),
    )


@pytest.mark.asyncio
async def test_contextual_lane_returns_original_text_even_without_any_graph_mentions():
    topology = CanonicalTopology()
    topology.mentions = topology.assertions = ()
    vectors = TopologyVectors()
    lane = ContextualLane(vectors.records[1])
    report = await runtime(topology, lane, vectors).retrieve(
        "query", tenant_id="tenant-1", options=RetrievalOptions(mode="local_semantic")
    )
    assert [item.id for item in report.results] == ["chunk-1", "chunk-2"]
    assert report.results[1].text == "Beta controls retries."
    assert report.results[1].metadata["topology_build_ids"] == ("build-2",)
    assert lane.calls[0][0].top_k == 100
    assert str(lane.calls[0][1].access.tenant_id) == "tenant-1"


@pytest.mark.asyncio
async def test_contextual_generation_rejected_at_finalization_cannot_displace_raw():
    topology = CanonicalTopology()
    topology.mentions = topology.assertions = ()
    topology.eligible = set()
    vectors = TopologyVectors()
    report = await runtime(topology, ContextualLane(vectors.records[1]), vectors).retrieve(
        "query", tenant_id="tenant-1", options=RetrievalOptions(mode="local_semantic")
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.results[0].score == 0.9
    assert not report.evidence.relevant_assertions


@pytest.mark.asyncio
async def test_flat_mode_does_not_invoke_contextual_or_graph_search():
    topology = CanonicalTopology()
    vectors = TopologyVectors()
    lane = ContextualLane(vectors.records[1])
    report = await runtime(topology, lane, vectors).retrieve("query", tenant_id="tenant-1")
    assert not lane.calls
    assert not topology.calls
    assert report.results[0].score == 0.9


@pytest.mark.asyncio
async def test_contextual_lane_cannot_smuggle_generated_text_as_raw_record_kind():
    topology = CanonicalTopology()
    topology.mentions = topology.assertions = ()
    vectors = TopologyVectors()
    generated = vectors.records[1].model_copy(
        update={
            "payload": {
                **vectors.records[1].payload,
                "record_kind": "contextual",
                "content": "generated",
            }
        }
    )
    report = await runtime(topology, ContextualLane(generated), vectors).retrieve(
        "query", tenant_id="tenant-1", options=RetrievalOptions(mode="local_semantic")
    )
    assert [item.id for item in report.results] == ["chunk-1"]
