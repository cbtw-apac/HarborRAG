from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from harborrag_adapters.repositories.vector.qdrant.mapping import QdrantMapper
from harborrag_core.indexing import (
    HybridSearchQuery,
    SparseSearchQuery,
    SparseVector,
    VectorIndexRecord,
    VectorIndexSpec,
    VectorSearchQuery,
    VectorSearchResult,
)
from harborrag_core.storage import StorageOperationContext


def search_result(
    point: Any,
    query: VectorSearchQuery,
    spec: VectorIndexSpec,
) -> VectorSearchResult:
    normalized = QdrantMapper.normalize_score(float(point.score), spec.distance)
    payload, logical_id = _payload(point)
    dense, _, _ = vectors(point.vector, spec)
    return VectorSearchResult(
        id=logical_id,
        score=normalized,
        raw_score=float(point.score),
        payload=payload if query.include_payload else {},
        vector=dense if query.include_vectors else None,
    )


def sparse_result(
    point: Any,
    *,
    query: SparseSearchQuery,
    spec: VectorIndexSpec,
) -> VectorSearchResult:
    raw_score = max(0.0, float(point.score))
    payload, logical_id = _payload(point)
    dense, _, _ = vectors(point.vector, spec)
    return VectorSearchResult(
        id=logical_id,
        score=raw_score / (1.0 + raw_score),
        raw_score=raw_score,
        payload=payload if query.include_payload else {},
        vector=dense if query.include_vectors else None,
    )


def weighted_rrf(
    dense_points: Sequence[Any],
    sparse_points: Sequence[Any],
    *,
    dense_weight: float,
) -> list[tuple[float, Any]]:
    scores: dict[str, float] = {}
    points: dict[str, Any] = {}
    _add_ranked_scores(scores, points, dense_points, dense_weight)
    _add_ranked_scores(
        scores,
        points,
        sparse_points,
        1.0 - dense_weight,
    )
    return sorted(
        ((score, points[identity]) for identity, score in scores.items()),
        key=lambda item: (-item[0], str(item[1].id)),
    )


def _add_ranked_scores(
    scores: dict[str, float],
    points: dict[str, Any],
    ranked: Sequence[Any],
    weight: float,
) -> None:
    for rank, point in enumerate(ranked, start=1):
        identity = str(point.id)
        scores[identity] = scores.get(identity, 0.0) + weight / (60 + rank)
        points.setdefault(identity, point)


def fused_result(
    point: Any,
    *,
    score: float,
    raw_score: float,
    query: HybridSearchQuery,
    spec: VectorIndexSpec,
) -> VectorSearchResult:
    payload, logical_id = _payload(point)
    dense, _, _ = vectors(point.vector, spec)
    return VectorSearchResult(
        id=logical_id,
        score=score,
        raw_score=raw_score,
        payload=payload if query.include_payload else {},
        vector=dense if query.include_vectors else None,
    )


def point_record(
    record: Any,
    context: StorageOperationContext,
    spec: VectorIndexSpec,
) -> VectorIndexRecord:
    payload, logical_id = _payload(record)
    vector, sparse, named = vectors(record.vector, spec)
    return VectorIndexRecord(
        id=logical_id,
        tenant_id=context.tenant_id,
        vector=vector,
        sparse_vector=sparse,
        named_vectors=named,
        payload=payload,
    )


def vectors(
    value: object,
    spec: VectorIndexSpec,
) -> tuple[list[float], SparseVector | None, dict[str, list[float]]]:
    if isinstance(value, list):
        return value, None, {}
    if not isinstance(value, dict):
        return [], None, {}
    dense_name = spec.dense_vector_name
    dense_value = value.get(dense_name) if dense_name is not None else None
    dense = list(dense_value) if isinstance(dense_value, list) else []
    sparse_value = (
        value.get(spec.sparse_vector_name) if spec.sparse_vector_name is not None else None
    )
    indices = getattr(sparse_value, "indices", None)
    values = getattr(sparse_value, "values", None)
    if isinstance(sparse_value, dict):
        indices = sparse_value.get("indices")
        values = sparse_value.get("values")
    sparse = (
        SparseVector(indices=list(indices), values=list(values))
        if indices is not None and values is not None
        else None
    )
    named = {
        name: list(vector)
        for name, vector in value.items()
        if name not in {dense_name, spec.sparse_vector_name} and isinstance(vector, list)
    }
    return dense, sparse, named


def _payload(record: Any) -> tuple[dict[str, object], str]:
    payload = dict(record.payload or {})
    return payload, str(record.id)
