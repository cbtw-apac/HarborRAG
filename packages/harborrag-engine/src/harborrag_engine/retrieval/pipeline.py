"""Async retrieval orchestration across injected provider boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from typing import cast

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult

from .fusion import reciprocal_rank_fusion
from .ports import QueryRewriter, ResultReranker, RetrievalContext, RetrievalSource


class RetrievalPipeline:
    """Rewrite, retrieve, fuse, and optionally rerank tenant-scoped results."""

    def __init__(
        self,
        sources: Sequence[RetrievalSource] | Sequence[RetrievalResult],
        *,
        rewriter: QueryRewriter | None = None,
        reranker: ResultReranker | None = None,
        oversample_factor: int = 3,
    ) -> None:
        if oversample_factor < 1:
            raise ValueError("retrieval oversample_factor must be positive")
        # Compatibility for the original alpha constructor. Production code
        # should inject repository adapters through RetrievalSource.
        if sources and all(isinstance(item, RetrievalResult) for item in sources):
            self._sources: tuple[RetrievalSource, ...] = (
                _SequenceSource(cast("Sequence[RetrievalResult]", sources)),
            )
        else:
            self._sources = tuple(cast("Sequence[RetrievalSource]", sources))
        if not self._sources:
            raise ValueError("retrieval requires at least one source")
        self._rewriter = rewriter
        self._reranker = reranker
        self._oversample_factor = oversample_factor

    async def aretrieve(
        self,
        query: RetrievalQuery,
        *,
        context: RetrievalContext | None = None,
    ) -> list[RetrievalResult]:
        """Execute all I/O concurrently and preserve explicit tenant scope."""

        if not query.text.strip() or query.top_k < 1:
            raise ValueError("retrieval query text/top_k are invalid")
        retrieval_context = context or _context_from_query(query)
        rewrites = (
            await self._rewriter.rewrite(query, context=retrieval_context)
            if self._rewriter is not None
            else (query.text,)
        )
        normalized_rewrites = tuple(
            dict.fromkeys(value.strip() for value in rewrites if value.strip())
        )
        if not normalized_rewrites:
            raise ValueError("query rewriter returned no usable query")
        provider_query = replace(
            query,
            top_k=query.top_k * self._oversample_factor,
        )
        operations = [
            source.search(
                replace(provider_query, text=text),
                context=retrieval_context,
            )
            for text in normalized_rewrites
            for source in self._sources
        ]
        rankings = await asyncio.gather(*operations)
        fused = reciprocal_rank_fusion(rankings)
        if self._reranker is not None:
            fused = list(
                await self._reranker.rerank(
                    query,
                    fused,
                    context=retrieval_context,
                )
            )
        return fused[: query.top_k]

    def retrieve(
        self,
        query: RetrievalQuery,
        *,
        context: RetrievalContext | None = None,
    ) -> list[RetrievalResult]:
        """Compatibility sync facade for non-async callers."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.aretrieve(query, context=context))
        raise RuntimeError("use await aretrieve() from an async context")


class _SequenceSource:
    """Compatibility bridge for callers of the original alpha constructor."""

    def __init__(self, results: Sequence[RetrievalResult]) -> None:
        self._results = tuple(results)

    async def search(
        self,
        query: RetrievalQuery,
        *,
        context: RetrievalContext,
    ) -> Sequence[RetrievalResult]:
        del context
        return sorted(self._results, key=lambda item: item.score, reverse=True)[: query.top_k]


def _context_from_query(query: RetrievalQuery) -> RetrievalContext:
    tenant = query.filters.get("tenant_id")
    if not isinstance(tenant, str) or not tenant.strip():
        raise ValueError("retrieval requires RetrievalContext or filters['tenant_id']")
    request_id = query.filters.get("request_id")
    return RetrievalContext(
        tenant_id=tenant,
        request_id=request_id if isinstance(request_id, str) else None,
    )
