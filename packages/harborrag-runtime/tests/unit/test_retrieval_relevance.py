"""Retrieval carries a relevance a caller may act on, not just a rank.

``RetrievalResult.score`` is whatever the lane produced. On the hybrid lane
that is reciprocal-rank fusion rescaled into a 0..1 shape, so it looks like a
confidence and is not one -- a nonsense query's top hit scores as high as a
perfect match's. ``relevance`` is the similarity the lane actually measured,
which is what a caller can threshold.
"""

from __future__ import annotations

import pytest
from retrieval_test_support import FakeChunkReader, FakeEmbedClient, FakeVectorRepository
from retrieval_test_support import policy as _policy
from retrieval_test_support import resources as _resources

from harborrag_runtime.retrieval import RetrievalOptions, RuntimeRetrievalService


@pytest.mark.asyncio
async def test_retrieval_results_carry_the_lane_relevance() -> None:
    service = RuntimeRetrievalService(
        resources=_resources(
            embed=FakeEmbedClient(),
            vectors=FakeVectorRepository(),
            chunks=FakeChunkReader(),
        ),
        policy=_policy(),
    )

    report = await service.retrieve(
        "release acceptance",
        tenant_id="tenant-1",
        top_k=2,
        options=RetrievalOptions(),
    )

    assert report.results, "fixture should return at least one result"
    assert report.results[0].relevance == pytest.approx(0.9)
