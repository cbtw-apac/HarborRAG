"""Async Qdrant-backed retrieval pipeline used exclusively by smoke tests.

This helper is intentionally kept outside the production ``src/`` tree because
``harborrag-mcp`` does not declare ``harborrag-adapters`` as a runtime
dependency.  Smoke tests pull it in from the workspace dev environment.
"""
from __future__ import annotations

import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from harborrag_adapters.repositories.vector.qdrant import (
    QdrantVectorConfig,
    QdrantVectorRepository,
)
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_core.schemas.storage import StorageOperationContext
from harborrag_core.schemas.vector import (
    FilterOperator,
    VectorCollectionSpec,
    VectorFilter,
    VectorFilterCondition,
    VectorPoint,
    VectorSearchQuery,
)

# ---------------------------------------------------------------------------
# Embed helpers
# ---------------------------------------------------------------------------

SMOKE_DIMENSION = 8


def deterministic_embed(text: str, *, dim: int = SMOKE_DIMENSION) -> list[float]:
    """Produce a reproducible unit vector from arbitrary text.

    Uses a SHA-256 digest to seed the components, then L2-normalises so that
    cosine similarity is well-defined.  Only suitable for smoke tests – it
    carries no semantic meaning.
    """
    digest = hashlib.sha256(text.encode()).digest()
    raw = [((digest[i] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    magnitude = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / magnitude for v in raw]


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------


def dict_to_vector_filter(filters: dict[str, Any]) -> VectorFilter | None:
    """Translate a flat key/value mapping into a HarborRAG :class:`VectorFilter`.

    Each pair becomes a ``must`` equality condition.  Returns ``None`` when
    ``filters`` is empty so the query hits all points without a filter clause.
    """
    if not filters:
        return None
    return VectorFilter(
        must=[
            VectorFilterCondition(field=k, operator=FilterOperator.EQUALS, value=v)
            for k, v in filters.items()
        ]
    )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


@dataclass
class QdrantRetrievalPipeline:
    """Async retrieval pipeline backed by a live :class:`QdrantVectorRepository`.

    Usage::

        async with QdrantRetrievalPipeline(url="http://localhost:6333", ...) as p:
            results = await p.aretrieve(RetrievalQuery("my query"))
    """

    url: str
    collection: str
    tenant_id: str = "smoke"
    embed_fn: Callable[[str], list[float]] = field(
        default_factory=lambda: deterministic_embed
    )
    dimension: int = SMOKE_DIMENSION
    _repo: QdrantVectorRepository | None = field(default=None, init=False, repr=False)

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> QdrantRetrievalPipeline:
        self._repo = QdrantVectorRepository(
            QdrantVectorConfig(
                instance_name="smoke",
                url=self.url,
                prefer_grpc=False,
            )
        )
        await self._repo.__aenter__()
        await self._repo.ensure_collection(
            VectorCollectionSpec(name=self.collection, dimension=self.dimension),
            context=self._context(),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._repo is not None:
            await self._repo.__aexit__(exc_type, exc, tb)
            self._repo = None

    # -- data helpers --------------------------------------------------------

    async def seed(self, points: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Upsert ``(id, text, payload)`` tuples into the collection.

        The text is embedded with ``embed_fn``; the payload is stored as-is so
        smoke tests can assert on metadata filters.
        """
        assert self._repo is not None, "seed() called outside async context manager"
        vector_points = [
            VectorPoint(
                id=pt_id,
                tenant_id=self.tenant_id,
                vector=self.embed_fn(text),
                payload={**payload, "text": text},
            )
            for pt_id, text, payload in points
        ]
        await self._repo.upsert(self.collection, vector_points, context=self._context())

    async def delete_collection(self) -> None:
        """Remove the smoke collection so tests clean up after themselves."""
        assert self._repo is not None
        if await self._repo.collection_exists(self.collection, context=self._context()):
            await self._repo.delete_collection(self.collection, context=self._context())

    # -- retrieval -----------------------------------------------------------

    async def aretrieve(
        self,
        query: RetrievalQuery,
        *,
        score_threshold: float | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        """Run a vector search and return normalised :class:`RetrievalResult` objects.

        ``score_threshold`` and ``filters`` are applied inside Qdrant before
        results are returned, exercising the native index-level behaviour
        (as opposed to the post-fetch filter in :class:`VectorSearchTool`).
        """
        assert self._repo is not None, "aretrieve() called outside async context manager"
        sq = VectorSearchQuery(
            collection=self.collection,
            vector=self.embed_fn(query.text),
            top_k=query.top_k,
            score_threshold=score_threshold,
            filters=dict_to_vector_filter(filters or query.filters),
        )
        raw = await self._repo.search(sq, context=self._context())
        return [
            RetrievalResult(
                id=r.id,
                text=str(r.payload.get("text", "")),
                score=r.score,
                metadata={k: v for k, v in r.payload.items() if k != "text"},
            )
            for r in raw
        ]

    # -- private -------------------------------------------------------------

    def _context(self) -> StorageOperationContext:
        return StorageOperationContext(tenant_id=self.tenant_id)
