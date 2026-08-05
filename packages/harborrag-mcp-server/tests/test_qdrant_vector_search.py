from __future__ import annotations

import hashlib
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_mcp_server.tools.vector_search import VectorSearchTool

_DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
SMOKE_DIMENSION = 8


def deterministic_embed(text: str, *, dim: int = SMOKE_DIMENSION) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    raw = [((digest[i] / 255.0) * 2.0 - 1.0) for i in range(dim)]
    magnitude = math.sqrt(sum(v * v for v in raw)) or 1.0
    return [v / magnitude for v in raw]


def _qdrant_url() -> str:
    return os.getenv("HARBOR_SMOKE_QDRANT_URL", _DEFAULT_QDRANT_URL).strip()


def _collection_name() -> str:
    return f"mcp_server_smoke_{uuid.uuid4().hex[:8]}"


@dataclass
class QdrantRetrievalPipeline:
    url: str
    collection: str
    tenant_id: str = "smoke"
    embed_fn: Callable[[str], list[float]] = field(default_factory=lambda: deterministic_embed)
    dimension: int = SMOKE_DIMENSION
    _client: Any = field(default=None, init=False, repr=False)

    async def __aenter__(self) -> QdrantRetrievalPipeline:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.http import models

        self._client = AsyncQdrantClient(url=self.url, prefer_grpc=False)
        await self._client.recreate_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self.dimension,
                distance=models.Distance.COSINE,
            ),
        )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def seed(self, points: list[tuple[str, str, dict[str, Any]]]) -> None:
        from qdrant_client.http import models

        assert self._client is not None
        vector_points = [
            models.PointStruct(
                id=pt_id,
                vector=self.embed_fn(text),
                payload={**payload, "text": text, "tenant_id": self.tenant_id},
            )
            for pt_id, text, payload in points
        ]
        await self._client.upsert(
            collection_name=self.collection,
            points=vector_points,
            wait=True,
        )

    async def delete_collection(self) -> None:
        assert self._client is not None
        await self._client.delete_collection(collection_name=self.collection)

    async def aretrieve(
        self,
        query: RetrievalQuery,
        *,
        score_threshold: float | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievalResult]:
        from qdrant_client.http import models

        assert self._client is not None
        merged_filters: dict[str, Any] = {"tenant_id": self.tenant_id}
        merged_filters.update(query.filters)
        if filters is not None:
            merged_filters.update(filters)
        field_filters = models.Filter(
            must=[
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in merged_filters.items()
            ]
        )

        raw = await self._client.search(
            collection_name=self.collection,
            query_vector=self.embed_fn(query.text),
            limit=query.top_k,
            score_threshold=score_threshold,
            query_filter=field_filters,
        )
        return [
            RetrievalResult(
                id=item.id,
                text=str((item.payload or {}).get("text", "")),
                score=item.score,
                metadata={k: v for k, v in (item.payload or {}).items() if k != "text"},
            )
            for item in raw
        ]


def _sync_retrieve(
    pipeline: QdrantRetrievalPipeline,
    query: str,
    *,
    top_k: int = 10,
    score_threshold: float | None = None,
    filters: dict[str, Any] | None = None,
) -> list[RetrievalResult]:
    import asyncio

    return asyncio.run(
        pipeline.aretrieve(
            RetrievalQuery(text=query, top_k=top_k),
            score_threshold=score_threshold,
            filters=filters,
        )
    )


@pytest.fixture(scope="module")
def qdrant_url() -> str:
    pytest.importorskip("qdrant_client")

    import httpx

    url = _qdrant_url()
    try:
        response = httpx.get(f"{url}/healthz", timeout=3.0)
        response.raise_for_status()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Qdrant not reachable at {url}: {exc}")
    return url


@pytest.fixture(scope="module")
def seeded_pipeline(qdrant_url: str):
    import asyncio

    seed = [
        ("doc-1", "HarborRAG vector search integration", {"category": "rag"}),
        ("doc-2", "Qdrant database running on localhost", {"category": "infra"}),
        ("doc-3", "Score threshold filters low-quality results", {"category": "rag"}),
    ]
    collection = _collection_name()
    pipeline = QdrantRetrievalPipeline(url=qdrant_url, collection=collection)

    async def _setup() -> None:
        await pipeline.__aenter__()
        await pipeline.seed(seed)

    async def _teardown() -> None:
        await pipeline.delete_collection()
        await pipeline.__aexit__(None, None, None)

    asyncio.run(_setup())
    yield pipeline
    asyncio.run(_teardown())


def test_qdrant_pipeline_returns_results(seeded_pipeline: QdrantRetrievalPipeline) -> None:
    results = _sync_retrieve(seeded_pipeline, "HarborRAG vector search")

    assert len(results) > 0
    assert results == sorted(results, key=lambda item: item.score, reverse=True)


def test_qdrant_pipeline_filter_works(seeded_pipeline: QdrantRetrievalPipeline) -> None:
    rag_results = _sync_retrieve(
        seeded_pipeline,
        "HarborRAG",
        filters={"category": "rag"},
    )

    assert len(rag_results) > 0
    assert all(item.metadata.get("category") == "rag" for item in rag_results)


def test_vector_search_tool_threshold_with_qdrant_seeded_scores(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    live_results = _sync_retrieve(
        seeded_pipeline,
        "HarborRAG",
        top_k=10,
        filters={"category": "rag"},
    )
    if len(live_results) < 2:
        pytest.skip("Not enough Qdrant results to validate threshold filtering")

    class QdrantToolPipeline:
        def __init__(self, pipeline: QdrantRetrievalPipeline) -> None:
            self._pipeline = pipeline

        def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
            import asyncio

            return asyncio.run(
                self._pipeline.aretrieve(
                    query,
                    filters={k: v for k, v in query.filters.items() if k != "tenant_id"},
                )
            )

    tool = VectorSearchTool(pipeline=QdrantToolPipeline(seeded_pipeline))
    threshold = sorted((item.score for item in live_results), reverse=True)[0]

    result = tool.call(
        {
            "query": "HarborRAG",
            "top_k": 10,
            "score_threshold": threshold,
            "filters": {"tenant_id": "smoke", "category": "rag"},
        }
    )

    assert result["ok"] is True
    assert all(item["score"] >= threshold for item in result["results"])
