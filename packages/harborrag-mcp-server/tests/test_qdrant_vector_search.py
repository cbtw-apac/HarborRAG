from __future__ import annotations

import hashlib
import math
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_mcp_server.tools.vector_search import VectorSearchTool
from harborrag_runtime.sdk import RetrievalLane

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
        from qdrant_client import QdrantClient
        from qdrant_client.http import models

        self._client = QdrantClient(url=self.url, prefer_grpc=False)
        self._client.recreate_collection(
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
            self._client.close()
            self._client = None

    async def seed(self, points: list[tuple[str, str, dict[str, Any]]]) -> None:
        from qdrant_client.http import models

        assert self._client is not None
        vector_points = [
            models.PointStruct(
                id=str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{self.collection}:{pt_id}",
                    )
                ),
                vector=self.embed_fn(text),
                payload={**payload, "text": text, "tenant_id": self.tenant_id},
            )
            for pt_id, text, payload in points
        ]
        self._client.upsert(
            collection_name=self.collection,
            points=vector_points,
            wait=True,
        )

    async def delete_collection(self) -> None:
        assert self._client is not None
        self._client.delete_collection(collection_name=self.collection)

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

        response = self._client.query_points(
            collection_name=self.collection,
            query=self.embed_fn(query.text),
            limit=query.top_k,
            score_threshold=score_threshold,
            query_filter=field_filters,
            with_payload=True,
        )
        return [
            RetrievalResult(
                id=item.id,
                text=str((item.payload or {}).get("text", "")),
                score=item.score,
                metadata={k: v for k, v in (item.payload or {}).items() if k != "text"},
            )
            for item in response.points
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
    query_text = "HarborRAG vector search integration"
    live_results = _sync_retrieve(
        seeded_pipeline,
        query_text,
        top_k=10,
        filters={"category": "rag"},
    )
    if len(live_results) < 2:
        pytest.skip("Not enough Qdrant results to validate threshold filtering")

    class QdrantToolRetrieval:
        def __init__(self, pipeline: QdrantRetrievalPipeline) -> None:
            self._pipeline = pipeline

        async def search(self, request):
            results = await self._pipeline.aretrieve(
                RetrievalQuery(
                    text=request.query,
                    top_k=request.top_k,
                    filters=request.filters,
                ),
                filters=request.filters,
            )
            return SimpleNamespace(
                request_id="qdrant-smoke",
                lane=RetrievalLane.HYBRID,
                results=tuple(results),
                diagnostics={"candidate_hits": len(results)},
            )

    tool = VectorSearchTool(
        runtime=SimpleNamespace(retrieval=QdrantToolRetrieval(seeded_pipeline))
    )
    threshold = max(
        0.0,
        min(
            1.0,
            sorted((item.score for item in live_results), reverse=True)[0],
        ),
    )

    import asyncio

    result = asyncio.run(
        tool.call(
            {
                "query": query_text,
                "tenant_id": "smoke",
                "top_k": 10,
                "score_threshold": threshold,
                "filters": {"category": "rag"},
            },
            principal_id="smoke-test",
        )
    )

    assert result["ok"] is True, repr(result)
    assert all(item["score"] >= threshold for item in result["results"])
