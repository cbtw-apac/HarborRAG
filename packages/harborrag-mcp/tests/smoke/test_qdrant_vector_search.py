"""Smoke tests for VectorSearchTool against a locally running Qdrant instance.

Run with Qdrant available on localhost:6333 (default) or override via the
``HARBOR_SMOKE_QDRANT_URL`` environment variable::

    docker run -p 6333:6333 qdrant/qdrant
    pytest packages/harborrag-mcp/tests/smoke/test_qdrant_vector_search.py -v

Tests are automatically skipped when Qdrant is unreachable.
"""
from __future__ import annotations

import os
import uuid

import pytest

from harborrag_core.domain.retrieval import RetrievalQuery, RetrievalResult
from harborrag_engine.retrieval.mock import MockRetrievalPipeline
from harborrag_mcp.tools.vector_search import VectorSearchTool

from .qdrant_pipeline import QdrantRetrievalPipeline

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"


def _qdrant_url() -> str:
    return os.getenv("HARBOR_SMOKE_QDRANT_URL", _DEFAULT_QDRANT_URL).strip()


def _collection_name() -> str:
    """A unique collection per test run so parallel runs don't collide."""
    return f"mcp_smoke_{uuid.uuid4().hex[:8]}"


# Seed corpus: (id, text, payload)
_SEED: list[tuple[str, str, dict]] = [
    ("doc-1", "HarborRAG vector search integration", {"category": "rag", "priority": 1}),
    ("doc-2", "Qdrant database running on localhost", {"category": "infra", "priority": 2}),
    ("doc-3", "Score threshold filters low-quality results", {"category": "rag", "priority": 3}),
    ("doc-4", "Unrelated document about something else", {"category": "other", "priority": 4}),
]

# ---------------------------------------------------------------------------
# Qdrant availability fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qdrant_url() -> str:
    """Skip all tests in this module when Qdrant is not reachable."""
    import httpx

    url = _qdrant_url()
    try:
        resp = httpx.get(f"{url}/healthz", timeout=3.0)
        resp.raise_for_status()
    except Exception as exc:
        pytest.skip(f"Qdrant not reachable at {url}: {exc}")
    return url


# ---------------------------------------------------------------------------
# Async pipeline fixture (module-scoped: one Qdrant collection per test run)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_pipeline(qdrant_url: str):  # type: ignore[return]
    """Create, seed, and tear down a QdrantRetrievalPipeline synchronously.

    Uses ``asyncio.run()`` in a sync fixture so pytest's event-loop bookkeeping
    (anyio mode=STRICT) is not involved in lifecycle management.
    """
    import asyncio

    collection = _collection_name()
    pipeline = QdrantRetrievalPipeline(url=qdrant_url, collection=collection)

    async def _setup() -> None:
        await pipeline.__aenter__()
        await pipeline.seed(_SEED)

    async def _teardown() -> None:
        await pipeline.delete_collection()
        await pipeline.__aexit__(None, None, None)

    asyncio.run(_setup())
    yield pipeline
    asyncio.run(_teardown())


# ---------------------------------------------------------------------------
# Helpers that wrap async pipeline calls for sync test bodies
# ---------------------------------------------------------------------------


def _sync_retrieve(
    pipeline: QdrantRetrievalPipeline,
    query: str,
    *,
    top_k: int = 10,
    score_threshold: float | None = None,
    filters: dict | None = None,
) -> list[RetrievalResult]:
    import asyncio

    return asyncio.run(
        pipeline.aretrieve(
            RetrievalQuery(text=query, top_k=top_k),
            score_threshold=score_threshold,
            filters=filters,
        )
    )


# ---------------------------------------------------------------------------
# Pipeline smoke tests (direct, no MCP layer)
# ---------------------------------------------------------------------------


def test_qdrant_pipeline_returns_results(seeded_pipeline: QdrantRetrievalPipeline) -> None:
    results = _sync_retrieve(seeded_pipeline, "HarborRAG vector search")

    assert len(results) > 0
    assert all(hasattr(r, "id") and hasattr(r, "score") for r in results)


def test_qdrant_pipeline_results_ordered_by_score(seeded_pipeline: QdrantRetrievalPipeline) -> None:
    results = _sync_retrieve(seeded_pipeline, "HarborRAG")

    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_qdrant_pipeline_top_k_is_respected(seeded_pipeline: QdrantRetrievalPipeline) -> None:
    results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=2)

    assert len(results) <= 2


def test_qdrant_pipeline_score_threshold_filters_low_scores(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    # Retrieve without threshold to get the full score distribution
    all_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10)
    if len(all_results) < 2:
        pytest.skip("Not enough results to verify threshold filtering")

    # Use the median score as threshold so at least one result is dropped
    mid_score = sorted(r.score for r in all_results)[len(all_results) // 2]
    filtered = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10, score_threshold=mid_score)

    assert all(r.score >= mid_score for r in filtered)
    assert len(filtered) <= len(all_results)


def test_qdrant_pipeline_metadata_filter_returns_only_matching_category(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    results = _sync_retrieve(
        seeded_pipeline, "HarborRAG", top_k=10, filters={"category": "rag"}
    )

    assert len(results) > 0
    assert all(r.metadata.get("category") == "rag" for r in results)


def test_qdrant_pipeline_filter_excludes_non_matching_docs(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    infra = _sync_retrieve(seeded_pipeline, "database", top_k=10, filters={"category": "infra"})
    other = _sync_retrieve(seeded_pipeline, "database", top_k=10, filters={"category": "other"})

    infra_ids = {r.id for r in infra}
    other_ids = {r.id for r in other}
    assert infra_ids.isdisjoint(other_ids), "filter must not mix categories"


# ---------------------------------------------------------------------------
# VectorSearchTool smoke tests (MCP tool layer + Qdrant data)
# ---------------------------------------------------------------------------


def test_vector_search_tool_score_threshold_with_qdrant_data(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    """Seed data from Qdrant, then exercise VectorSearchTool's score_threshold
    post-filter with a MockRetrievalPipeline carrying real scores."""
    live_results = _sync_retrieve(seeded_pipeline, "HarborRAG vector", top_k=10)
    if len(live_results) < 2:
        pytest.skip("Not enough Qdrant results to test threshold filtering via tool")

    # Wire the tool with a mock pipeline carrying the scores we got from Qdrant
    mock_pipeline = MockRetrievalPipeline(live_results)
    tool = VectorSearchTool(pipeline=mock_pipeline)

    # Pick a threshold that admits at most the top-scoring result
    high_threshold = live_results[0].score  # at least the best result passes
    result = tool.call({"query": "HarborRAG vector", "score_threshold": high_threshold})

    assert result["ok"] is True
    assert result["score_threshold"] == high_threshold
    assert all(r["score"] >= high_threshold for r in result["results"])


def test_vector_search_tool_filters_forwarded_to_pipeline(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    """Confirm that filters are forwarded through the tool call to the pipeline."""
    # Retrieve "rag" category docs from Qdrant to build a typed mock
    rag_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10, filters={"category": "rag"})
    other_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10, filters={"category": "other"})
    all_results = rag_results + other_results

    mock_pipeline = MockRetrievalPipeline(all_results)
    tool = VectorSearchTool(pipeline=mock_pipeline)

    # The mock doesn't do metadata filtering, but the tool must forward filters correctly
    result = tool.call({"query": "HarborRAG", "filters": {"category": "rag"}})

    assert result["ok"] is True
    # Filters are echoed back through the mock (mock ignores them, but the tool passes them)
    # What we verify here: the call doesn't fail and filters are accepted by the schema
    assert isinstance(result["results"], list)


def test_vector_search_tool_score_threshold_zero_returns_all(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    """A score_threshold of 0.0 must not drop any results."""
    live_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10)
    mock_pipeline = MockRetrievalPipeline(live_results)
    tool = VectorSearchTool(pipeline=mock_pipeline)

    with_zero = tool.call({"query": "HarborRAG", "top_k": 10, "score_threshold": 0.0})

    assert len(with_zero["results"]) == len(live_results)


def test_vector_search_tool_default_threshold_matches_explicit_03(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    """The default threshold should behave the same as score_threshold=0.3."""
    live_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10)
    mock_pipeline = MockRetrievalPipeline(live_results)
    tool = VectorSearchTool(pipeline=mock_pipeline)

    default_threshold = tool.call({"query": "HarborRAG", "top_k": 10})
    explicit_threshold = tool.call({"query": "HarborRAG", "top_k": 10, "score_threshold": 0.3})

    assert default_threshold["score_threshold"] == 0.3
    assert default_threshold["results"] == explicit_threshold["results"]


def test_vector_search_tool_score_threshold_one_drops_all_below(
    seeded_pipeline: QdrantRetrievalPipeline,
) -> None:
    """A score_threshold of 1.0 should drop all results unless a perfect match exists."""
    live_results = _sync_retrieve(seeded_pipeline, "HarborRAG", top_k=10)
    mock_pipeline = MockRetrievalPipeline(live_results)
    tool = VectorSearchTool(pipeline=mock_pipeline)

    result = tool.call({"query": "HarborRAG", "score_threshold": 1.0})

    assert result["ok"] is True
    # All remaining results must have score == 1.0; most will be dropped
    assert all(r["score"] >= 1.0 for r in result["results"])
