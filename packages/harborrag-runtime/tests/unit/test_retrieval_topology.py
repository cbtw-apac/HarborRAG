"""Semantic evidence must keep both document and topology publication authority."""

from __future__ import annotations

import pytest
from topology_retrieval_support import (
    ActiveVersions,
    CanonicalTopology,
    TopologyVectors,
    mention,
    service,
)

from harborrag_core.indexing import VectorFilter, VectorFilterCondition
from harborrag_core.ingestion import DocumentIdentityBuilder
from harborrag_core.security import AccessContext
from harborrag_core.topology.search import RetrievalMode
from harborrag_engine.topology.retrieval import LocalTopologySearch
from harborrag_runtime.retrieval import RetrievalOptions

LOCAL = RetrievalOptions(mode=RetrievalMode.LOCAL_SEMANTIC)


@pytest.mark.asyncio
async def test_local_expansion_adds_grounded_chunks_and_revalidates_every_path_build():
    topology = CanonicalTopology()
    report = await service(topology).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )

    assert [item.id for item in report.results] == ["chunk-1", "chunk-2"]
    expanded = report.results[1]
    assert expanded.text == "Beta controls retries."
    assert expanded.metadata["topology_build_ids"] == ("build-1", "build-2")
    assert expanded.metadata["topology_assertion_ids"] == ("assertion-1",)
    assert expanded.metadata["retrieval_ranks"] == {"semantic": 2}
    assert topology.validation_calls == [("tenant-1", ("build-1", "build-2"))]
    assert report.diagnostics.short_by == 0
    assert report.diagnostics.topology.fallback is None


@pytest.mark.asyncio
async def test_flat_default_never_reads_topology():
    topology = CanonicalTopology()
    report = await service(topology).retrieve("dependencies", tenant_id="tenant-1", top_k=2)
    assert [item.id for item in report.results] == ["chunk-1"]
    assert topology.calls == []
    assert report.diagnostics.short_by == 1
    assert report.results[0].score == 0.9


@pytest.mark.asyncio
async def test_graph_only_evidence_can_enter_an_already_full_flat_result_set():
    class FullFlatVectors(TopologyVectors):
        def _results(self, collection):
            first = super()._results(collection)[0]
            return [
                first,
                *(
                    first.model_copy(
                        update={
                            "id": DocumentIdentityBuilder().point_id(chunk_id=chunk),
                            "payload": {**first.payload, "chunk_id": chunk},
                        }
                    )
                    for chunk in ("flat-only-1", "flat-only-2")
                ),
            ]

    report = await service(CanonicalTopology(), FullFlatVectors()).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=3, options=LOCAL
    )
    assert len(report.results) == 3
    assert "chunk-2" in {item.id for item in report.results}


@pytest.mark.asyncio
async def test_filtered_local_request_explicitly_falls_back_without_unfiltered_reads():
    topology = CanonicalTopology()
    vectors = TopologyVectors()
    filters = VectorFilter(must=[VectorFilterCondition(field="source_scope_id", value="scope-1")])
    report = await service(topology, vectors).retrieve(
        "dependencies",
        tenant_id="tenant-1",
        options=RetrievalOptions(mode=LOCAL.mode, filters=filters),
    )
    assert report.diagnostics.topology.fallback == "filters_require_flat"
    scoped = vectors.hybrid_queries[0][0].filters
    assert scoped.must[:-1] == filters.must
    assert scoped.must[-1].field == "document_id"
    assert topology.calls == vectors.get_calls == []


@pytest.mark.asyncio
async def test_retired_seed_build_invalidates_entire_path_but_preserves_flat_ranking():
    topology = CanonicalTopology()
    topology.eligible = {"build-2"}
    report = await service(topology).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.results[0].score == 0.9
    assert "topology_build_ids" not in report.results[0].metadata
    assert report.diagnostics.topology.fallback == "topology_changed"


@pytest.mark.asyncio
async def test_stale_document_vectors_cannot_be_reintroduced_by_live_topology():
    authority = ActiveVersions()
    authority.versions["document-2"] = "version-3"
    report = await service(CanonicalTopology(), authority=authority).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.diagnostics.topology.rejected == 1


@pytest.mark.asyncio
async def test_mismatched_projection_identity_is_rejected():
    vectors = TopologyVectors()
    vectors.records[1] = vectors.records[1].model_copy(
        update={"payload": {**vectors.records[1].payload, "document_id": "wrong-document"}}
    )
    report = await service(CanonicalTopology(), vectors).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.diagnostics.topology.rejected == 1


@pytest.mark.asyncio
async def test_ambiguous_exact_labels_never_create_identity_merges():
    topology = CanonicalTopology()
    topology.mentions = (*topology.mentions, mention("other-alpha", "chunk-3", "Alpha"))
    expansion = await LocalTopologySearch(topology).expand(
        "Alpha", (), tenant_id="tenant-1", access=AccessContext.system("tenant-1")
    )
    assert expansion.evidence == ()
    assert expansion.ambiguous_labels == 1


@pytest.mark.asyncio
async def test_truncated_label_lookup_cannot_claim_unambiguous_entity():
    topology = CanonicalTopology()
    topology.mentions = tuple(mention("entity-1", f"chunk-{i}", "Alpha") for i in range(128))
    expansion = await LocalTopologySearch(topology).expand(
        "Alpha", (), tenant_id="tenant-1", access=AccessContext.system("tenant-1")
    )
    assert expansion.evidence == ()
    assert expansion.truncated


@pytest.mark.asyncio
async def test_topology_authority_outage_returns_flat_results():
    class FailingAuthority(CanonicalTopology):
        async def eligible_build_ids(self, tenant_id, build_ids, *, access=None):
            raise ConnectionError("unavailable")

    report = await service(FailingAuthority()).retrieve(
        "dependencies", tenant_id="tenant-1", options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.diagnostics.topology.fallback == "topology_validation_failed"


@pytest.mark.asyncio
async def test_final_document_check_runs_after_topology_validation():
    authority = ActiveVersions()

    class AdvancingTopology(CanonicalTopology):
        async def eligible_build_ids(self, tenant_id, build_ids, *, access=None):
            authority.versions["document-2"] = "version-3"
            return await super().eligible_build_ids(tenant_id, build_ids, access=access)

    report = await service(AdvancingTopology(), authority=authority).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.diagnostics.stale_candidates == 1
    assert report.diagnostics.short_by == 1


@pytest.mark.asyncio
async def test_expansion_requires_canonical_quote_to_match_projection_content():
    vectors = TopologyVectors()
    vectors.records[1] = vectors.records[1].model_copy(
        update={"payload": {**vectors.records[1].payload, "content": "Unrelated corrupted text"}}
    )
    report = await service(CanonicalTopology(), vectors).retrieve(
        "dependencies", tenant_id="tenant-1", top_k=2, options=LOCAL
    )
    assert [item.id for item in report.results] == ["chunk-1"]
    assert report.diagnostics.topology.rejected == 1


@pytest.mark.asyncio
async def test_tenant_argument_cannot_disagree_with_access_boundary():
    with pytest.raises(ValueError, match="access tenant"):
        await service(CanonicalTopology()).retrieve(
            "dependencies",
            tenant_id="tenant-1",
            access=AccessContext.system("tenant-2"),
            options=LOCAL,
        )
