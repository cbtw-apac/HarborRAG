from __future__ import annotations

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy
from harborrag_engine.retrieval.base import BaseEvidenceBuilder, BaseRetrievalPipeline
from harborrag_engine.retrieval.evidence import EvidenceBuilder
from harborrag_engine.retrieval.fusion import reciprocal_rank_fusion
from harborrag_engine.retrieval.reranking import keep_top
from harborrag_engine.retrieval.rewriting import identity_rewrite


class BrokenRetrieval(BaseRetrievalPipeline):
    def retrieve(self, query):
        return super().retrieve(query)


class BrokenEvidence(BaseEvidenceBuilder):
    def build(self, results):
        return super().build(results)


def test_implemented_engine_base_methods_raise():
    with pytest.raises(NotImplementedError):
        BrokenRetrieval().retrieve(RetrievalQuery("q"))
    with pytest.raises(NotImplementedError):
        BrokenEvidence().build([])


def test_evidence_builder_numbers_results_in_order():
    builder = EvidenceBuilder()
    results = [
        RetrievalResult("a", "first", 0.9),
        RetrievalResult("b", "second", 0.5),
    ]

    evidence = builder.build(results)

    assert evidence == "[1] first\n\n[2] second"


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
    assert identity_rewrite("hello") == ["hello"]
    assert keep_top(results, 1)[0].id == "b"
