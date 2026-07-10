from __future__ import annotations

import pytest
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.builder import EngineBuilder
from harborrag_engine.config import EngineConfig
from harborrag_engine.policy import EnginePolicy
from harborrag_engine.retrieval.base import BaseEvidenceBuilder, BaseRetrievalPipeline
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


def test_engine_builder_and_policy():
    diagnostics = EngineBuilder(
        EngineConfig(tenant="t", environment="test"),
        EnginePolicy(max_concurrency=2),
    ).diagnostics()

    assert diagnostics["tenant"] == "t"
    assert diagnostics["environment"] == "test"
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
