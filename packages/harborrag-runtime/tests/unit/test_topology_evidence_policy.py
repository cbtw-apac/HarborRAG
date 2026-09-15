"""Direct evidence is protected and enriched passages never bypass serving ACLs."""

from __future__ import annotations

from dataclasses import replace

import pytest
from retrieval_test_support import policy, resources
from topology_retrieval_support import ActiveVersions, CanonicalTopology, TopologyVectors, service

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.security import AccessContext
from harborrag_core.topology.retrieval_policy import TopologyRetrievalPolicy
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.evidence import select_evidence
from harborrag_runtime.retrieval.topology import fuse_candidates

LOCAL = RetrievalOptions(mode="local_semantic")


def test_direct_quota_preserves_four_of_ten_even_when_all_graph_scores_are_higher():
    ranked = [RetrievalResult(str(i), f"raw passage {i}", 0.9) for i in range(20)]
    direct = tuple(
        VectorSearchResult(id=str(i), score=0.1, raw_score=0.1, payload={"chunk_id": str(i)})
        for i in range(10, 20)
    )
    selected, tokens, excluded = select_evidence(
        ranked, direct, top_k=10, policy=TopologyRetrievalPolicy()
    )
    assert [item.id for item in selected[:4]] == ["10", "11", "12", "13"]
    assert [item.id for item in selected[4:]] == [str(i) for i in range(6)]
    assert len({item.id for item in selected}) == 10
    assert tokens <= 8000
    assert excluded == 0


def test_budget_skips_oversized_whole_passages_without_truncation_and_refills():
    huge = RetrievalResult("huge", "世" * 200, 0.9)
    small = RetrievalResult("small", "unchanged raw evidence", 0.8)
    direct = (
        VectorSearchResult(id="huge", score=0.9, raw_score=0.9, payload={"chunk_id": "huge"}),
    )
    selected, tokens, excluded = select_evidence(
        [huge, small], direct, top_k=2, policy=TopologyRetrievalPolicy(max_context_tokens=200)
    )
    assert selected == [small]
    assert selected[0].text == "unchanged raw evidence"
    assert tokens <= 200
    assert excluded == 1


def test_rrf_constant_is_explicit_and_changes_scores_without_mutating_raw_candidates():
    direct = (VectorSearchResult(id="raw", score=0.9, raw_score=0.9, payload={"chunk_id": "raw"}),)
    semantic = (
        VectorSearchResult(id="graph", score=0.2, raw_score=0.2, payload={"chunk_id": "graph"}),
    )
    fused = fuse_candidates(direct, semantic, rrf_constant=10)
    assert fused[0].raw_score == pytest.approx(0.5 / 11)
    assert direct[0].score == 0.9
    assert fuse_candidates(direct, (), rrf_constant=10) == direct


@pytest.mark.asyncio
async def test_raw_acl_filter_exists_before_vector_ranking_and_missing_permission_denies():
    class UnknownPermissions(CanonicalTopology):
        async def allowed_document_ids(self, tenant_id, *, access, limit=10000):
            return ()

    vectors = TopologyVectors()
    report = await service(UnknownPermissions(), vectors).retrieve("question", tenant_id="tenant-1")
    assert report.results == ()
    assert vectors.hybrid_queries[0][0].filters.must[-1].value == []


@pytest.mark.asyncio
async def test_permission_revocation_after_topology_validation_removes_original_passages():
    class Revoked(CanonicalTopology):
        revoked = False

        async def eligible_build_ids(self, tenant_id, build_ids, *, access=None):
            allowed = await super().eligible_build_ids(tenant_id, build_ids, access=access)
            self.revoked = True
            return allowed

        async def authorized_document_ids(self, tenant_id, document_ids, *, access):
            return set() if self.revoked else set(document_ids)

    report = await service(Revoked()).retrieve("dependencies", tenant_id="tenant-1", options=LOCAL)
    assert report.results == ()
    assert report.evidence.original_passages == ()
    assert report.evidence.relevant_assertions == ()
    assert report.evidence.evidence_paths == ()


@pytest.mark.asyncio
async def test_evidence_bundle_keeps_qualified_assertions_paths_and_raw_passages():
    report = await service(CanonicalTopology()).retrieve(
        "dependencies", tenant_id="tenant-1", options=LOCAL
    )
    assert report.evidence.original_passages == report.results
    assert report.evidence.relevant_assertions[0].observation.predicate == "depends_on"
    assert report.evidence.relevant_assertions[0].observation.polarity == "affirmative"
    assert any(path.kind == "typed_path" for path in report.evidence.evidence_paths)
    assert report.results[1].text == "Beta controls retries."
    assert report.diagnostics.topology.policy_version == "bounded-evidence-v2"


@pytest.mark.asyncio
async def test_runtime_budget_is_enforced_after_final_authority_checks():
    runtime = RuntimeRetrievalService(
        resources=replace(
            resources(vectors=TopologyVectors(), active_versions=ActiveVersions()),
            topology_repository=CanonicalTopology(),
        ),
        policy=replace(policy(), topology=TopologyRetrievalPolicy(max_context_tokens=1)),
    )
    report = await runtime.retrieve(
        "dependencies", tenant_id="tenant-1", options=LOCAL, access=AccessContext.system("tenant-1")
    )
    assert report.results == ()
    assert report.diagnostics.topology.budget_excluded == 2
    assert "context_budget_excluded_passages" in report.evidence.coverage_gaps
