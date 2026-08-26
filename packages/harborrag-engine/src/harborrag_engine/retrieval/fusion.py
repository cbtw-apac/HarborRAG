from collections.abc import Sequence
from dataclasses import replace

from harborrag_core.domain.retrieval import RetrievalResult


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
