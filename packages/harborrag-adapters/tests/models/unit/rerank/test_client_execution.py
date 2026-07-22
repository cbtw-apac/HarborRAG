from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import FakeRerankInvocation, rerank_config

from harborrag_adapters.models.common.config import CacheConfig, RetryPolicyConfig
from harborrag_adapters.models.common.lifecycle import ResourceOwnership
from harborrag_adapters.models.rerank import HarborRerankingClient
from harborrag_adapters.models.rerank.configs import HarborRerankProviderConfig
from harborrag_adapters.models.rerank.registry import HarborRerankProvider
from harborrag_core.models.capabilities import HarborRerankCapabilities
from harborrag_core.models.errors import HarborRerankProviderError
from harborrag_core.models.rerank import HarborRerankRequest

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def raw_rerank(*scores: float) -> dict[str, Any]:
    """Build one LiteLLM-style reranking response."""
    return {
        "id": "rerank-response",
        "results": [
            {"index": index, "relevance_score": score} for index, score in enumerate(scores)
        ],
        "meta": {"billed_units": {"search_units": 1}},
    }


def deployment(name: str, *, order: int = 0) -> HarborRerankProviderConfig:
    """Build one compatible reranking deployment."""
    return HarborRerankProviderConfig(
        name=name,
        provider=HarborRerankProvider.COHERE,
        model=f"cohere/{name}",
        api_key="secret",
        order=order,
        capabilities=HarborRerankCapabilities(),
    )


def test_rerank_sync_async_cache_middleware_and_capabilities() -> None:
    events: list[str] = []

    class Middleware:
        def before_request(self, request: Any, context: Any) -> Any:
            events.append("before")
            return request

        def after_response(self, response: Any, context: Any) -> Any:
            events.append("after")
            return response

    invocation = FakeRerankInvocation([raw_rerank(0.2, 0.9), raw_rerank(1.0)])
    client = HarborRerankingClient.from_config(
        rerank_config(cache=CacheConfig(enabled=True, ttl_seconds=30)),
        invocation=invocation,
        middleware=(Middleware(),),
    )
    request = HarborRerankRequest(
        query="q",
        documents=(
            {"content": "a", "document_id": "a"},
            {"content": "b", "document_id": "b"},
        ),
        metadata={"tenant_id": "tenant"},
        cacheable=True,
    )
    first = client.rerank(request=request)
    cached = client.rerank(request=request)
    assert [item.document_id for item in first.results] == ["b", "a"]
    assert cached.cache_hit and len(invocation.calls) == 1
    assert events == ["before", "after", "before", "after"]
    assert set(client.capabilities()) == {"rerank-a"}

    async def run() -> None:
        response = await client.arerank("q", ["a"])
        assert response.results[0].index == 0

    import asyncio

    asyncio.run(run())
    client.close()
    client.close()
    assert invocation.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.rerank("q", ["a"])


@pytest.mark.asyncio
async def test_rerank_async_context_and_borrowed_invocation() -> None:
    invocation = FakeRerankInvocation([raw_rerank(1.0)])
    async with HarborRerankingClient(
        rerank_config(),
        invocation=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    ) as client:
        response = await client.arerank("q", ["a"])
        assert response.results[0].relevance_score == 1.0
    assert invocation.closed == 0


def test_rerank_sends_complete_candidate_set_once() -> None:
    documents = [f"document-{index}" for index in range(5)]
    invocation = FakeRerankInvocation([raw_rerank(0.1, 0.2, 0.3, 0.4, 0.5)])
    response = HarborRerankingClient(rerank_config(), invocation=invocation).rerank("q", documents)
    assert len(invocation.calls) == 1
    assert invocation.calls[0]["documents"] == documents
    assert response.results[0].index == 4


def test_rerank_retry_deployment_and_model_fallback() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=1,
        max_deployment_failovers=1,
        max_model_fallbacks=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeRerankInvocation([TimeoutError(), raw_rerank(1.0)])
    response = HarborRerankingClient(
        rerank_config(
            deployments=(deployment("first"), deployment("second", order=1)),
            retry=retry,
        ),
        invocation=invocation,
    ).rerank("q", ["a"])
    assert response.deployment == "second" and response.fallback_count == 1

    fallback_invocation = FakeRerankInvocation([TimeoutError(), raw_rerank(1.0)])
    fallback = HarborRerankingClient(
        rerank_config(deployments=(deployment("only"),), retry=retry, fallbacks=("fallback",)),
        invocation=fallback_invocation,
    ).rerank("q", ["a"])
    assert fallback.logical_model == "fallback" and fallback.fallback_count == 1


@pytest.mark.asyncio
async def test_rerank_same_deployment_retry_and_nonretryable_error() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=2,
        max_deployment_failovers=0,
        max_model_fallbacks=0,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeRerankInvocation([TimeoutError(), raw_rerank(1.0)])
    response = await HarborRerankingClient(
        rerank_config(retry=retry), invocation=invocation
    ).arerank("q", ["a"])
    assert response.retry_count == 1
    failed = FakeRerankInvocation([HarborRerankProviderError("safe", retryable=False)])
    with pytest.raises(HarborRerankProviderError):
        await HarborRerankingClient(rerank_config(retry=retry), invocation=failed).arerank(
            "q", ["a"]
        )
    assert len(failed.calls) == 1
