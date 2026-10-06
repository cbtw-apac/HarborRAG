"""``relevance`` is measured dense similarity on every lane, or ``None``.

``score`` orders results; on the hybrid lane it is rank arithmetic whose top hit
sits near 1.0 whatever the match, and on the sparse lane a squashed BM25 value
that put off-topic CVs at 0.95. Only cosine is a similarity a caller can compare
across queries, so it is the one number reported as relevance.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_adapters.repositories.vector.qdrant import (
    collections as collections_module,
)
from harborrag_adapters.repositories.vector.qdrant import query as query_module
from harborrag_adapters.repositories.vector.qdrant import (
    repository as repository_module,
)
from harborrag_adapters.repositories.vector.qdrant.query_mapping import dense_similarity
from harborrag_adapters.repositories.vector.qdrant.repository import QdrantVectorRepository
from harborrag_core.indexing import VectorDistance
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    HybridSearchQuery,
    SparseSearchQuery,
    SparseVector,
    VectorIndexSpec,
    VectorSearchQuery,
)

from .fakes import ExtendedModels, FakeQdrantClient, FakeRawQdrant, make_config

_SPEC = VectorIndexSpec(
    index_name="docs",
    dimension=3,
    dense_vector_name="dense",
    sparse_vector_name="sparse",
)


class _IdScoringQdrant(FakeRawQdrant):
    """Answers a has-id dense query with the exact cosine of those points."""

    def __init__(self, exact: dict[str, float]) -> None:
        super().__init__()
        self.exact = exact

    async def query_points(self, **kwargs: Any) -> Any:
        query_filter = kwargs.get("query_filter")
        must = getattr(query_filter, "must", None) or []
        ids = [item for condition in must for item in getattr(condition, "has_id", [])]
        if not ids:
            return await super().query_points(**kwargs)
        self.query_calls.append(kwargs)
        return SimpleNamespace(
            points=[
                SimpleNamespace(id=item, score=self.exact[item], payload=None, vector=None)
                for item in ids
                if item in self.exact
            ]
        )


def _repository(
    monkeypatch: pytest.MonkeyPatch, raw: FakeRawQdrant
) -> tuple[QdrantVectorRepository, StorageOperationContext]:
    monkeypatch.setattr(repository_module, "qm", ExtendedModels)
    monkeypatch.setattr(collections_module, "qm", ExtendedModels)
    monkeypatch.setattr(query_module, "qm", ExtendedModels)
    repository = QdrantVectorRepository(
        make_config(),
        client=FakeQdrantClient(raw),  # type: ignore[arg-type]
    )
    context = StorageOperationContext.system(tenant_id="tenant-a")
    repository._specs[repository._queries.spec_key("docs", context)] = _SPEC
    return repository, context


def _point(identity: str, score: float) -> SimpleNamespace:
    return SimpleNamespace(id=identity, score=score, payload={}, vector=None)


def test_only_cosine_is_reported_as_a_similarity() -> None:
    assert dense_similarity(0.42, VectorDistance.COSINE) == pytest.approx(0.42)
    # Opposite is no more relevant than unrelated, and a threshold is 0..1.
    assert dense_similarity(-0.3, VectorDistance.COSINE) == 0.0
    assert dense_similarity(3.5, VectorDistance.DOT_PRODUCT) is None
    assert dense_similarity(0.2, VectorDistance.EUCLIDEAN) is None


@pytest.mark.asyncio
async def test_dense_relevance_is_the_cosine_not_the_rescaled_ranking_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = FakeRawQdrant()
    # An unrelated text: cosine 0.414, which (cos + 1) / 2 used to report as 0.707.
    raw.points = [_point("unrelated", 0.414)]
    repository, context = _repository(monkeypatch, raw)

    [result] = await repository.search(
        VectorSearchQuery(index_name="docs", vector=[1.0, 0.0, 0.0], top_k=1),
        context=context,
    )

    assert result.score == pytest.approx(0.707)
    assert result.relevance == pytest.approx(0.414)


@pytest.mark.asyncio
async def test_the_sparse_lane_reports_no_relevance(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = FakeRawQdrant()
    raw.points = [_point("keyword-heavy", 19.0)]
    repository, context = _repository(monkeypatch, raw)

    [result] = await repository.sparse_search(
        SparseSearchQuery(
            index_name="docs",
            sparse_vector=SparseVector(indices=[2], values=[1.0]),
            top_k=1,
        ),
        context=context,
    )

    # The squashed BM25 value still orders the lane...
    assert result.score == pytest.approx(0.95)
    # ...but it is not a similarity, so it is not reported as one.
    assert result.relevance is None


@pytest.mark.asyncio
async def test_hybrid_relevance_is_dense_similarity_even_for_sparse_only_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A strong keyword match does not lend a point a similarity it lacks.

    The point both lanes found takes its dense cosine, not the larger squashed
    BM25 value; the point only the sparse lane found is measured directly.
    """

    raw = _IdScoringQdrant(exact={"keyword-only": 0.12})
    raw.dense_points = [_point("both", 0.31)]
    raw.sparse_points = [_point("both", 40.0), _point("keyword-only", 25.0)]
    repository, context = _repository(monkeypatch, raw)

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
    assert by_id["both"].score > 0.6
    assert by_id["both"].relevance == pytest.approx(0.31)
    assert by_id["keyword-only"].relevance == pytest.approx(0.12)
    # One extra call, restricted to the unmeasured id, without payload.
    measuring = raw.query_calls[-1]
    assert measuring["using"] == "dense"
    assert measuring["limit"] == 1
    assert measuring["with_payload"] is False


@pytest.mark.asyncio
async def test_hybrid_skips_the_measuring_call_when_dense_scored_every_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = FakeRawQdrant()
    raw.dense_points = [_point("a", 0.5), _point("b", 0.4)]
    raw.sparse_points = [_point("b", 3.0)]
    repository, context = _repository(monkeypatch, raw)

    await repository.hybrid_search(
        HybridSearchQuery(
            index_name="docs",
            vector=[1.0, 0.0, 0.0],
            sparse_vector=SparseVector(indices=[2], values=[1.0]),
            top_k=2,
        ),
        context=context,
    )

    assert [call["using"] for call in raw.query_calls] == ["dense", "sparse"]
