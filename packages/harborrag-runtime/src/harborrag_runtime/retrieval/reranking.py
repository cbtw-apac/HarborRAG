"""Rerank stage of authoritative retrieval.

Orders passages the caller is already allowed to read; it never adds, filters or
re-checks them. The model's score goes in ``metadata["rerank_score"]``, and
``relevance`` keeps the lane's similarity, which chat and the MCP ``score_threshold``
compare against. Passage text reaches the rerank provider but is never cached or
echoed back.

Entry point: PassageReranker.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.models.rerank import HarborRerankDocument, HarborRerankRequest
from harborrag_core.ports.model_clients import AsyncHarborRerankClientProtocol

RERANK_SCORE_KEY = "rerank_score"


class PassageReranker:
    """Order passages by the rerank model's relevance to the query.

    Args:
        client: Rerank client built from the ``rerank`` section of the model catalog.
    """

    def __init__(self, client: AsyncHarborRerankClientProtocol) -> None:
        self._client = client

    async def rerank(self, query: str, results: Sequence[RetrievalResult]) -> list[RetrievalResult]:
        """Return ``results`` most relevant first, each with its rerank score.

        Errors from the rerank client, such as ``HarborRerankTimeoutError``,
        propagate unchanged, as embedding errors do.

        Args:
            query: The question the passages should answer.
            results: Passages to order.

        Returns:
            The passages the model ranked, most relevant first.
        """
        if not results:
            return []
        response = await self._client.arerank(
            request=HarborRerankRequest(
                query=query,
                documents=tuple(HarborRerankDocument.text(result.text) for result in results),
                return_documents=False,
                cacheable=False,
                sensitive=True,
            )
        )
        return [
            replace(
                results[ranked.index],
                metadata={
                    **results[ranked.index].metadata,
                    RERANK_SCORE_KEY: ranked.relevance_score,
                },
            )
            for ranked in response.results
        ]


__all__ = ["RERANK_SCORE_KEY", "PassageReranker"]
