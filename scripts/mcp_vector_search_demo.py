"""Standalone smoke test for the MCP vector_search tool: ingest a document,
then search it, over a small FastAPI harness.

This wires VectorSearchTool to a real embedding provider (OpenAI) and an
in-memory Qdrant collection (qdrant-client's ``location=":memory:"`` mode, no
Docker/server required). It talks to the tool the same way a real MCP
transport would: through McpServer.call_tool("vector_search", ...).

Setup (not installed in every environment by default):

    pip install fastapi "uvicorn[standard]" "qdrant-client>=1.10,<2"

Run:

    export OPENAI_API_KEY=sk-...        # required; only OpenAI is wired up here
    python scripts/mcp_vector_search_demo.py

Then, from another shell:

    curl -X POST localhost:8000/ingest -H "content-type: application/json" -d '{
      "tenant_id": "demo", "document_id": "doc-1",
      "text": "HarborRAG is a modular RAG framework.\n\nIt supports Qdrant for vector search."
    }'

    curl -X POST localhost:8000/search -H "content-type: application/json" -d '{
      "tenant_id": "demo", "query": "What vector store does HarborRAG use?"
    }'

Or open http://localhost:8000/docs for an interactive Swagger UI.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qdrant_models

from harborrag_adapters.models.embed import HarborEmbedClient, HarborEmbedClientConfig
from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_mcp_server.server import McpServer
from harborrag_mcp_server.tools.health import HealthTool
from harborrag_mcp_server.tools.vector_search import VectorSearchTool

_COLLECTION = "mcp_vector_search_demo"
_CHUNK_CHARS = 800


def _split_into_chunks(text: str, *, max_chars: int = _CHUNK_CHARS) -> list[str]:
    """Split on blank lines, then hard-wrap any paragraph that's still too long."""

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = [
        paragraph[start : start + max_chars]
        for paragraph in paragraphs
        for start in range(0, len(paragraph), max_chars)
    ]
    return chunks or [text.strip()]


def _build_embed_client() -> HarborEmbedClient:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY before running this demo")
    model = os.environ.get("HARBORRAG_DEMO_EMBED_MODEL", "text-embedding-3-small")
    config = HarborEmbedClientConfig.model_validate(
        {
            "default_model": "default",
            "models": {
                "default": {
                    "provider": "openai",
                    "name": "openai-default",
                    "model": model,
                    "api_key": api_key,
                }
            },
        }
    )
    return HarborEmbedClient.from_config(config)


@dataclass
class QdrantEmbeddedPipeline:
    """Sync-facing retrieval pipeline backed by an in-memory Qdrant collection.

    VectorSearchTool.call() is synchronous end-to-end (see vector_search.py),
    so this exposes plain `.ingest()`/`.retrieve()` methods that drive the
    async embedding/Qdrant calls via `asyncio.run()` — the same compatibility
    pattern harborrag_engine.retrieval.pipeline.RetrievalPipeline.retrieve()
    uses for non-async callers.
    """

    embed_client: HarborEmbedClient
    qdrant: AsyncQdrantClient = field(
        default_factory=lambda: AsyncQdrantClient(location=":memory:")
    )
    _dimension: int | None = field(default=None, init=False, repr=False)

    def _embed(self, text: str) -> list[float]:
        response = self.embed_client.embed(text)
        value = response.embeddings[0].value
        if not isinstance(value, tuple):
            raise ValueError("expected a float embedding, got a base64-encoded value")
        return list(value)

    async def _ensure_collection(self, dimension: int) -> None:
        if await self.qdrant.collection_exists(_COLLECTION):
            return
        await self.qdrant.create_collection(
            collection_name=_COLLECTION,
            vectors_config=qdrant_models.VectorParams(
                size=dimension,
                distance=qdrant_models.Distance.COSINE,
            ),
        )

    async def aingest(self, *, tenant_id: str, document_id: str, text: str) -> int:
        chunks = _split_into_chunks(text)
        vectors = [self._embed(chunk) for chunk in chunks]
        self._dimension = len(vectors[0])
        await self._ensure_collection(self._dimension)
        points = [
            qdrant_models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "text": chunk,
                    "tenant_id": tenant_id,
                    "document_id": document_id,
                    "chunk_index": index,
                },
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ]
        await self.qdrant.upsert(collection_name=_COLLECTION, points=points, wait=True)
        return len(points)

    def ingest(self, *, tenant_id: str, document_id: str, text: str) -> int:
        return asyncio.run(self.aingest(tenant_id=tenant_id, document_id=document_id, text=text))

    async def aretrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        if self._dimension is None:
            return []
        tenant_id = query.filters.get("tenant_id")
        query_filter = (
            qdrant_models.Filter(
                must=[
                    qdrant_models.FieldCondition(
                        key="tenant_id",
                        match=qdrant_models.MatchValue(value=tenant_id),
                    )
                ]
            )
            if tenant_id is not None
            else None
        )
        response = await self.qdrant.query_points(
            collection_name=_COLLECTION,
            query=self._embed(query.text),
            query_filter=query_filter,
            limit=query.top_k,
        )
        return [
            RetrievalResult(
                id=str(point.id),
                text=str((point.payload or {}).get("text", "")),
                score=point.score,
                metadata={k: v for k, v in (point.payload or {}).items() if k != "text"},
            )
            for point in response.points
        ]

    def retrieve(self, query: RetrievalQuery) -> list[RetrievalResult]:
        return asyncio.run(self.aretrieve(query))

    async def aclose(self) -> None:
        await self.embed_client.aclose()
        await self.qdrant.close()


_pipeline = QdrantEmbeddedPipeline(embed_client=_build_embed_client())
_mcp_server = McpServer(tools=[HealthTool(), VectorSearchTool(pipeline=_pipeline)])


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    del app
    yield
    await _pipeline.aclose()


app = FastAPI(title="HarborRAG MCP vector_search demo", lifespan=_lifespan)


class IngestRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SearchRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=20)
    score_threshold: float = Field(default=0.3, ge=0.0, le=1.0)


@app.get("/tools")
def list_tools() -> list[dict[str, Any]]:
    return [
        {"name": spec.name, "description": spec.description} for spec in _mcp_server.list_tools()
    ]


@app.post("/ingest")
def ingest(payload: IngestRequest) -> dict[str, Any]:
    try:
        chunk_count = _pipeline.ingest(
            tenant_id=payload.tenant_id,
            document_id=payload.document_id,
            text=payload.text,
        )
    except Exception as exc:  # embedding/Qdrant failures surface as a clear 502
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "chunks_indexed": chunk_count}


@app.post("/search")
def search(payload: SearchRequest) -> dict[str, Any]:
    try:
        result = _mcp_server.call_tool(
            "vector_search",
            {
                "query": payload.query,
                "top_k": payload.top_k,
                "score_threshold": payload.score_threshold,
                "filters": {"tenant_id": payload.tenant_id},
            },
        )
    except (ValueError, PermissionError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("ok", False):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


if __name__ == "__main__":
    import uvicorn  # type: ignore[import-not-found]

    uvicorn.run(app, host="127.0.0.1", port=8000)
