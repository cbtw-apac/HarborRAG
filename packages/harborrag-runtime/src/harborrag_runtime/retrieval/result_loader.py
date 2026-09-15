"""Query embedding and projection payload materialization for retrieval."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from harborrag_core.domain.retrieval import RetrievalResult
from harborrag_core.indexing import VectorSearchResult
from harborrag_core.models.embed import EmbeddingPurpose, HarborEmbedRequest
from harborrag_core.ports.model_clients import AsyncHarborEmbedClientProtocol

from .contracts import RetrievalPolicy
from .payload import optional_text, section_path
from .validation import required_text

_LOAD_CONCURRENCY = 8

logger = logging.getLogger("harborrag.runtime.retrieval")


class EvidenceResultLoader:
    """Materialize bounded evidence results without adding repository authority."""

    def __init__(
        self,
        embed_client: AsyncHarborEmbedClientProtocol,
        policy: RetrievalPolicy,
    ) -> None:
        self._embed = embed_client
        self._policy = policy

    async def dense_vector(self, query: str) -> tuple[float, ...]:
        response = await self._embed.aembed(
            request=HarborEmbedRequest(
                inputs=(query,),
                logical_model=self._policy.embedding_model,
                dimensions=self._policy.embedding_dimensions,
                purpose=EmbeddingPurpose.QUERY,
                normalize=self._policy.normalize_embeddings,
                cacheable=False,
                sensitive=True,
            )
        )
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("retrieval requires a float query embedding")
        if len(value) != self._policy.embedding_dimensions:
            raise ValueError("retrieval embedding has an unexpected dimension")
        return value

    async def load_candidates(
        self,
        candidates: Sequence[VectorSearchResult],
        *,
        request_id: str,
    ) -> tuple[list[tuple[VectorSearchResult, RetrievalResult]], int]:
        """Materialize candidates concurrently, skipping malformed payloads."""

        load_limit = asyncio.Semaphore(_LOAD_CONCURRENCY)

        async def load(candidate: VectorSearchResult) -> RetrievalResult:
            async with load_limit:
                return self.result(candidate)

        loaded = await asyncio.gather(
            *(load(candidate) for candidate in candidates),
            return_exceptions=True,
        )
        results: list[tuple[VectorSearchResult, RetrievalResult]] = []
        failures = 0
        for candidate, result in zip(candidates, loaded, strict=True):
            if isinstance(result, Exception):
                failures += 1
                logger.warning(
                    "Skipping malformed or unreadable retrieval candidate",
                    extra={"request_id": request_id, "candidate_id": str(candidate.id)},
                    exc_info=(type(result), result, result.__traceback__),
                )
                continue
            if isinstance(result, BaseException):
                raise result
            results.append((candidate, result))
        return results, failures

    @staticmethod
    def result(candidate: VectorSearchResult) -> RetrievalResult:
        payload = candidate.payload
        metadata: dict[str, object] = {
            "document_id": required_text(payload, "document_id"),
            "document_version_id": required_text(payload, "document_version_id"),
            "record_kind": required_text(payload, "record_kind"),
            "chunk_kind": required_text(payload, "chunk_kind"),
            "connector_type": required_text(payload, "connector_type"),
            "citation_locator": payload.get("citation_locator", {}),
            "quality_score": payload.get("quality_score"),
            "raw_score": candidate.raw_score,
            "retrieval_source": "qdrant-authoritative",
            # Older evidence payloads predate these provenance fields; keep
            # the public metadata shape stable instead of omitting the keys.
            "document_title": optional_text(payload, "document_title"),
            "section_path": section_path(payload),
        }
        for key in (
            "source_scope_id",
            "source_item_id",
            "content_hash",
        ):
            value = payload.get(key)
            if value is not None:
                metadata[key] = value
        return RetrievalResult(
            id=required_text(payload, "chunk_id"),
            text=required_text(payload, "content"),
            score=candidate.score,
            relevance=candidate.relevance,
            metadata=metadata,
        )


__all__ = ["EvidenceResultLoader"]
