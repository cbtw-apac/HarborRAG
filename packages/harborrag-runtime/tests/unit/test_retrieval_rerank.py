"""Reranking reorders passages by the rerank model without touching lane relevance."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from retrieval_test_support import FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.models.rerank import HarborRerankRequest
from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService
from harborrag_runtime.retrieval.reranking import RERANK_SCORE_KEY, PassageReranker
from harborrag_runtime.retrieval.service import (
    RERANK_POOL_FACTOR,
    RERANK_POOL_LIMIT,
    in_result_order,
    rerank_pool,
)

pytestmark = pytest.mark.unit


class ReversingRerankClient:
    def __init__(self) -> None:
        self.requests: list[HarborRerankRequest] = []

    async def arerank(self, *, request: HarborRerankRequest) -> SimpleNamespace:
        self.requests.append(request)
        count = len(request.documents)
        return SimpleNamespace(
            results=tuple(
                SimpleNamespace(index=index, relevance_score=0.9 - 0.1 * rank)
                for rank, index in enumerate(reversed(range(count)))
            )
        )


@pytest.mark.asyncio
async def test_reranker_orders_by_the_model_and_keeps_text_out_of_cache() -> None:
    client = ReversingRerankClient()
    passages = [
        RetrievalResult("chunk-1", "first", 0.9, relevance=0.8),
        RetrievalResult("chunk-2", "second", 0.5, relevance=0.4),
    ]

    ranked = await PassageReranker(client).rerank("question", passages)

    assert [result.id for result in ranked] == ["chunk-2", "chunk-1"]
    assert ranked[0].metadata[RERANK_SCORE_KEY] == pytest.approx(0.9)
    assert [result.relevance for result in ranked] == [0.4, 0.8]
    (request,) = client.requests
    assert request.return_documents is False
    assert request.cacheable is False
    assert request.sensitive is True


@pytest.mark.asyncio
async def test_retrieval_reranks_a_wider_pool_when_asked() -> None:
    client = ReversingRerankClient()
    plain_vectors, reranked_vectors = FakeVectorRepository(), FakeVectorRepository()
    plain = RuntimeRetrievalService(resources=_resources(vectors=plain_vectors), policy=_policy())
    reranked = RuntimeRetrievalService(
        resources=replace(_resources(vectors=reranked_vectors), rerank_client=client),
        policy=_policy(),
    )

    await plain.retrieve("timeout", tenant_id="tenant-1", top_k=8)
    report = await reranked.retrieve(
        "timeout", tenant_id="tenant-1", top_k=8, options=RetrievalOptions(rerank=True)
    )

    assert client.requests[0].query == "timeout"
    assert RERANK_SCORE_KEY in report.results[0].metadata
    plain_window = plain_vectors.hybrid_queries[0][0].top_k
    assert reranked_vectors.hybrid_queries[0][0].top_k > plain_window


@pytest.mark.asyncio
async def test_rerank_without_a_rerank_model_serves_the_lane_order() -> None:
    service = RuntimeRetrievalService(resources=_resources(), policy=_policy())

    report = await service.retrieve(
        "timeout", tenant_id="tenant-1", top_k=2, options=RetrievalOptions(rerank=True)
    )

    assert report.results
    assert RERANK_SCORE_KEY not in report.results[0].metadata


@pytest.mark.parametrize("top_k", range(1, 101))
def test_rerank_pool_holds_top_k_and_stays_within_one_search_unit(top_k: int) -> None:
    pool = rerank_pool(top_k)

    assert top_k <= pool <= max(top_k, RERANK_POOL_LIMIT)
    assert pool == max(top_k, min(top_k * RERANK_POOL_FACTOR, RERANK_POOL_LIMIT))


def test_rerank_pool_matches_pinned_values() -> None:
    assert [rerank_pool(top_k) for top_k in (1, 5, 25, 60, 100)] == [4, 20, 100, 100, 100]


def _direct_hit(chunk_id: str) -> VectorSearchResult:
    return VectorSearchResult(
        id=f"point-{chunk_id}", score=0.5, raw_score=0.5, payload={"chunk_id": chunk_id}
    )


def test_in_result_order_follows_the_rerank_and_keeps_unranked_hits_last() -> None:
    direct = tuple(_direct_hit(chunk_id) for chunk_id in ("chunk-1", "chunk-2", "chunk-3"))
    reranked = [
        RetrievalResult("chunk-3", "third", 0.1),
        RetrievalResult("chunk-1", "first", 0.9),
    ]

    ordered = in_result_order(direct, reranked)

    assert [hit.payload["chunk_id"] for hit in ordered] == ["chunk-3", "chunk-1", "chunk-2"]
