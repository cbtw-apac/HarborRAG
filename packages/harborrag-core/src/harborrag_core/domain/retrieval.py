from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class RetrievalQuery:
    text: str
    top_k: int = 10
    filters: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RetrievalResult:
    id: str
    text: str
    score: float
    metadata: dict[str, Any] = field(default_factory=dict)
    # How similar this result actually is to the query, 0..1, or ``None``
    # when the lane cannot say. ``score`` is not a substitute: on the hybrid
    # lane it is a rank-fusion value rescaled into a 0..1 shape, so its top
    # hit sits near 1.0 even when nothing matched. Threshold on this.
    #
    # Kept last with a default so the positional shape of the existing fields
    # is unchanged -- callers construct this positionally.
    relevance: float | None = None
