from __future__ import annotations

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy
from harborrag_engine.retrieval.evidence import EvidenceBuilder
from harborrag_engine.retrieval.fusion import reciprocal_rank_fusion
from harborrag_engine.retrieval.pipeline import RetrievalPipeline
from harborrag_engine.retrieval.reranking import keep_top
from harborrag_engine.retrieval.rewriting import identity_rewrite


def test_evidence_builder_numbers_results_in_order():
    builder = EvidenceBuilder()
    results = [
        RetrievalResult("a", "first", 0.9),
        RetrievalResult("b", "second", 0.5),
    ]

    evidence = builder.build(results)

    assert evidence == (
        '<retrieved_evidence trust="untrusted">\n'
        '<document citation="1" id="a">\nfirst\n</document>\n'
        '<document citation="2" id="b">\nsecond\n</document>\n'
        "</retrieved_evidence>"
    )


def test_retrieval_pipeline_returns_the_requested_highest_scoring_results():
    pipeline = RetrievalPipeline(
        [
            RetrievalResult("a", "first", 0.4),
            RetrievalResult("b", "second", 0.9),
        ]
    )

    retrieved = pipeline.retrieve(RetrievalQuery("q", top_k=1, filters={"tenant_id": "tenant-1"}))

    assert retrieved[0].id == "b"
    assert retrieved[0].score == pytest.approx(1 / 61)


def test_engine_configuration_and_policy():
    config = EngineConfig(tenant="t", environment="test")
    policy = EnginePolicy(max_concurrency=2)

    assert config.tenant == "t"
    assert config.environment == "test"
    assert policy.max_concurrency == 2
    with pytest.raises(ValueError):
        EnginePolicy(max_concurrency=0)


def test_retrieval_helpers():
    results = [
        RetrievalResult("a", "harbor rag", 0.1),
        RetrievalResult("b", "other", 0.9),
    ]

    fused = reciprocal_rank_fusion([[results[0], results[1]], [results[1]]])

    assert fused[0].id == "b"
    assert fused[0].score > fused[1].score
    assert identity_rewrite("hello") == ["hello"]
    assert keep_top(results, 1)[0].id == "b"


def test_weighted_fusion_rejects_invalid_weights():
    result = RetrievalResult("a", "first", 0.9)

    with pytest.raises(ValueError, match="match"):
        reciprocal_rank_fusion([[result]], weights=(1.0, 0.5))
    with pytest.raises(ValueError, match="negative"):
        reciprocal_rank_fusion([[result]], weights=(-1.0,))
