"""Hybrid results carry a relevance distinct from their fused rank score."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    SparseVector,
    VectorIndexSpec,
)

from .fakes import ExtendedModels, FakeQdrantClient, FakeRawQdrant, make_config


@pytest.mark.asyncio
async def test_hybrid_search_reports_lane_relevance_not_rescaled_rank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``score`` on the hybrid lane is rank arithmetic; ``relevance`` must not be.

    ``weighted_rrf`` fuses by position, so ``score`` is ``rank`` rescaled into
    a 0..1 shape that merely *looks* like a similarity: the top hit scores
    near 1.0 whether or not it has anything to do with the query. Callers that
    need to know "is this actually relevant" get ``relevance`` -- the point's
    own normalized score in the lane it came from.
    """

    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    monkeypatch.setattr(query_module, "qm", ExtendedModels)
    raw = FakeRawQdrant()
    # cosine 0.9 -> normalized (0.9 + 1) / 2 == 0.95
    raw.dense_points = [SimpleNamespace(id="strong", score=0.9, payload={}, vector=None)]
    # cosine -0.4 -> normalized 0.3: ranked first in its lane, but a poor match
    raw.sparse_points = [SimpleNamespace(id="weak", score=0.0, payload={}, vector=None)]
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = VectorIndexSpec(
        index_name="docs",
        dimension=3,
        dense_vector_name="dense",
        sparse_vector_name="sparse",
    )

    results = await repository.hybrid_search(
        HybridSearchQuery(
            index_name="docs",
            vector=[1.0, 0.0, 0.0],
            sparse_vector=SparseVector(indices=[2], values=[1.0]),
            dense_weight=0.7,
            top_k=3,
        ),
        context=context,
    )

    by_id = {result.id: result for result in results}
    # Both rank first in their own lane, so both score near the top...
    assert by_id["strong"].score > 0.6
    # ...but only the dense hit is actually similar to the query.
    assert by_id["strong"].relevance == pytest.approx(0.95)
    assert by_id["weak"].relevance == pytest.approx(0.0)
