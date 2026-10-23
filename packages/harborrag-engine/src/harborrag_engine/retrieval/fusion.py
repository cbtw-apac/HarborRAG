from collections.abc import Sequence
from dataclasses import replace

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievalResult]],
    k: int = 60,
    *,
    weights: Sequence[float] | None = None,
) -> list[RetrievalResult]:
    """Fuse ranked sources with optional non-negative source weights."""

    if weights is None:
        weights = (1.0,) * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("fusion weights must match the number of rankings")
    if any(weight < 0 for weight in weights):
        raise ValueError("fusion weights must not be negative")

    scores: dict[str, float] = {}
    items: dict[str, RetrievalResult] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, item in enumerate(ranking, start=1):
            scores[item.id] = scores.get(item.id, 0.0) + weight / (k + rank)
            items[item.id] = item
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [replace(items[item_id], score=fused_score) for item_id, fused_score in ranked]


def fuse_candidates(
    flat: tuple[VectorSearchResult, ...],
    semantic: tuple[VectorSearchResult, ...],
    *,
    semantic_weight: float = 0.5,
    rrf_constant: int = 60,
) -> tuple[VectorSearchResult, ...]:
    if not semantic:
        return flat
    scores: dict[str, float] = {}
    items: dict[str, VectorSearchResult] = {}
    for weight, ranking in ((1.0 - semantic_weight, flat), (semantic_weight, semantic)):
        seen: set[str] = set()
        for rank, item in enumerate(ranking, 1):
            identity = str(item.payload.get("chunk_id", item.id))
            if identity in seen:
                continue
            seen.add(identity)
            scores[identity] = scores.get(identity, 0.0) + weight / (rrf_constant + rank)
            items.setdefault(identity, item)
    return tuple(
        items[identity].model_copy(
            update={"score": min(1.0, score * (rrf_constant + 1)), "raw_score": score}
        )
        for identity, score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    )
