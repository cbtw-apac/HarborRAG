"""Engine-owned retrieval orchestration ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult


@dataclass(frozen=True, slots=True)
class RetrievalContext:
    """Tenant/request scope that every retrieval boundary must receive."""

    tenant_id: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("retrieval tenant_id must be non-empty")


class RetrievalSource(Protocol):
    async def search(
        self,
        query: RetrievalQuery,
        *,
        context: RetrievalContext,
    ) -> Sequence[RetrievalResult]:
        """Return one independently ranked result list."""


class QueryRewriter(Protocol):
    async def rewrite(
        self,
        query: RetrievalQuery,
        *,
        context: RetrievalContext,
    ) -> Sequence[str]:
        """Return bounded query variants without changing tenant scope."""


class ResultReranker(Protocol):
    async def rerank(
        self,
        query: RetrievalQuery,
        results: Sequence[RetrievalResult],
        *,
        context: RetrievalContext,
    ) -> Sequence[RetrievalResult]:
        """Rerank already-fused results within the requested tenant."""
