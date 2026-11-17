"""Retrieval carries a relevance a caller may act on, not just a rank.

``RetrievalResult.score`` is whatever the lane produced. On the hybrid lane
that is reciprocal-rank fusion rescaled into a 0..1 shape, so it looks like a
confidence and is not one -- a nonsense query's top hit scores as high as a
perfect match's. ``relevance`` is the similarity the lane actually measured,
which is what a caller can threshold.
"""

from __future__ import annotations

import pytest

from harborrag_engine.retrieval.service.contracts import RetrievalOptions
from harborrag_engine.retrieval.service.service import RuntimeRetrievalService
from harborrag_engine.testing.retrieval_fakes import (
    FakeChunkReader,
    FakeEmbedClient,
    FakeVectorRepository,
)
from harborrag_engine.testing.retrieval_fakes import policy as _policy
from harborrag_engine.testing.retrieval_fakes import resources as _resources


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
