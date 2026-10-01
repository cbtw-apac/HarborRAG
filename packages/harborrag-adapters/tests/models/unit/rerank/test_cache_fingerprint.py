from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import FakeRerankInvocation, rerank_config

from harborrag_adapters.models.rerank import HarborRerankingClient
from harborrag_adapters.models.rerank.configs import HarborRerankProviderConfig
from harborrag_adapters.models.rerank.registry import HarborRerankProvider
from harborrag_adapters.models.runtime.cache import InMemoryModelCache
from harborrag_adapters.models.runtime.config import CacheConfig
from harborrag_core.models.capabilities import HarborRerankCapabilities
from harborrag_core.models.rerank import HarborRerankRequest

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


def _deployment(*, model: str) -> HarborRerankProviderConfig:
    """Build one reranking deployment pinned to the given provider model."""
    return HarborRerankProviderConfig(
        name="rerank-a",
        provider=HarborRerankProvider.COHERE,
        model=model,
        api_key="secret",
        capabilities=HarborRerankCapabilities(),
    )


def _raw_rerank(*scores: float) -> dict[str, Any]:
    """Build one LiteLLM-style reranking response."""
    return {
        "id": "rerank-response",
        "results": [
            {"index": index, "relevance_score": score} for index, score in enumerate(scores)
        ],
        "meta": {"billed_units": {"search_units": 1}},
    }


def test_repointed_rerank_model_does_not_serve_previous_models_cached_scores() -> None:
    """A deployment's provider model change must partition the shared response cache.

    Two configurations that differ only in a deployment's provider model must not
    collide on the same cache key: otherwise a repointed rerank model keeps serving
    the previous model's relevance scores from a shared cache.
    """
    cache_config = CacheConfig(enabled=True, ttl_seconds=30)
    request = HarborRerankRequest(
        query="q",
        documents=(
            {"content": "a", "document_id": "a"},
            {"content": "b", "document_id": "b"},
        ),
        metadata={"tenant_id": "tenant"},
        cacheable=True,
    )

    model_a = HarborRerankingClient(
        rerank_config(deployments=(_deployment(model="cohere/model-a"),), cache=cache_config),
        invocation=FakeRerankInvocation([_raw_rerank(0.2, 0.9)]),
    )
    model_b = HarborRerankingClient(
        rerank_config(deployments=(_deployment(model="cohere/model-b"),), cache=cache_config),
        invocation=FakeRerankInvocation([_raw_rerank(0.9, 0.2)]),
    )
    key_a = model_a._execution.cache.decision(request, "primary").key
    key_b = model_b._execution.cache.decision(request, "primary").key
    assert key_a is not None
    assert key_b is not None
    assert key_a != key_b

    shared_cache = InMemoryModelCache()
    first_invocation = FakeRerankInvocation([_raw_rerank(0.2, 0.9)])
    first_client = HarborRerankingClient(
        rerank_config(deployments=(_deployment(model="cohere/model-a"),), cache=cache_config),
        invocation=first_invocation,
        cache=shared_cache,
    )
    assert first_client._execution.cache.decision(request, "primary").key == key_a
    first = first_client.rerank(request=request)
    assert [item.document_id for item in first.results] == ["b", "a"]

    second_invocation = FakeRerankInvocation([_raw_rerank(0.9, 0.2)])
    second_client = HarborRerankingClient(
        rerank_config(deployments=(_deployment(model="cohere/model-b"),), cache=cache_config),
        invocation=second_invocation,
        cache=shared_cache,
    )
    second = second_client.rerank(request=request)
    assert second.cache_hit is False
    assert [item.document_id for item in second.results] == ["a", "b"]
    assert len(second_invocation.calls) == 1
