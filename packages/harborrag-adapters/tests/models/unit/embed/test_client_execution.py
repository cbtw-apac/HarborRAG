from __future__ import annotations

from typing import Any

import pytest
from model_runtime_support import FakeEmbeddingInvocation, embed_client, embed_config

from harborrag_adapters.models.embed.configs import HarborEmbedProviderConfig
from harborrag_adapters.models.embed.registry import HarborEmbedProvider
from harborrag_adapters.models.runtime.config import CacheConfig, RetryPolicyConfig
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_core.models.capabilities import HarborEmbedCapabilities
from harborrag_core.models.embed import HarborEmbedRequest
from harborrag_core.models.errors import HarborEmbedProviderError

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def raw_batch(*vectors: list[float]) -> dict[str, Any]:
    """Build one LiteLLM-style embedding response batch."""
    return {
        "model": "provider-embed",
        "data": [{"index": index, "embedding": value} for index, value in enumerate(vectors)],
        "usage": {"prompt_tokens": len(vectors), "total_tokens": len(vectors)},
    }


def deployment(name: str, *, order: int = 0) -> HarborEmbedProviderConfig:
    """Build one compatible embedding deployment."""
    return HarborEmbedProviderConfig(
        name=name,
        provider=HarborEmbedProvider.OPENAI,
        model=f"openai/{name}",
        api_key="secret",
        order=order,
        expected_dimensions=3,
        capabilities=HarborEmbedCapabilities(
            batch=True,
            max_batch_size=2,
            configurable_dimensions=True,
            default_dimensions=3,
            encoding_format=True,
        ),
    )


def test_embed_sync_async_cache_middleware_and_capabilities() -> None:
    events: list[str] = []

    class Middleware:
        def before_request(self, request: Any, context: Any) -> Any:
            events.append("before")
            return request

        def after_response(self, response: Any, context: Any) -> Any:
            events.append("after")
            return response

    invocation = FakeEmbeddingInvocation([raw_batch([1, 0, 0]), raw_batch([0, 1, 0])])
    client = embed_client(
        embed_config(cache=CacheConfig(enabled=True, ttl_seconds=30)),
        invocation=invocation,
        middleware=(Middleware(),),
    )
    request = HarborEmbedRequest(
        inputs=("hello",), metadata={"tenant_id": "tenant"}, cacheable=True
    )
    first = client.embed(request=request)
    cached = client.embed(request=request)
    assert first.embeddings[0].value == (1.0, 0.0, 0.0)
    assert cached.cache_hit and len(invocation.calls) == 1
    assert events == ["before", "after", "before", "after"]
    assert set(client.capabilities()) == {"embed-a"}

    async def run() -> None:
        response = await client.aembed(["other"])
        assert response.embeddings[0].value == (0.0, 1.0, 0.0)

    import asyncio

    asyncio.run(run())
    client.close()
    client.close()
    assert invocation.closed == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.embed(["x"])


@pytest.mark.asyncio
async def test_embed_async_context_and_borrowed_invocation() -> None:
    invocation = FakeEmbeddingInvocation([raw_batch([1, 0, 0])])
    async with embed_client(
        embed_config(),
        invocation=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    ) as client:
        assert (await client.aembed("x")).dimensions == 3
    assert invocation.closed == 0


def test_embedding_private_batches_use_one_deployment_and_merge() -> None:
    invocation = FakeEmbeddingInvocation([raw_batch([1, 0, 0], [0, 1, 0]), raw_batch([0, 0, 1])])
    response = embed_client(embed_config(), invocation=invocation).embed(["a", "b", "c"])
    assert [item.index for item in response.embeddings] == [0, 1, 2]
    assert len(invocation.calls) == 2
    assert invocation.calls[0]["model"] == invocation.calls[1]["model"]
    assert invocation.calls[0]["input"] == ["a", "b"]
    assert invocation.calls[1]["input"] == ["c"]


def test_partial_batch_failure_retries_the_complete_request() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=2,
        max_deployment_failovers=0,
        max_model_fallbacks=0,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeEmbeddingInvocation(
        [
            raw_batch([1, 0, 0], [0, 1, 0]),
            TimeoutError("batch two"),
            raw_batch([1, 0, 0], [0, 1, 0]),
            raw_batch([0, 0, 1]),
        ]
    )
    response = embed_client(embed_config(retry=retry), invocation=invocation).embed(["a", "b", "c"])
    assert len(response.embeddings) == 3
    assert response.retry_count == 1
    assert [call["input"] for call in invocation.calls] == [
        ["a", "b"],
        ["c"],
        ["a", "b"],
        ["c"],
    ]


def test_embedding_deployment_and_model_fallbacks_preserve_space() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=1,
        max_deployment_failovers=1,
        max_model_fallbacks=1,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeEmbeddingInvocation([TimeoutError(), raw_batch([1, 0, 0])])
    response = embed_client(
        embed_config(
            deployments=(deployment("first"), deployment("second", order=1)),
            retry=retry,
        ),
        invocation=invocation,
    ).embed("x")
    assert response.deployment == "second" and response.fallback_count == 1

    fallback_invocation = FakeEmbeddingInvocation([TimeoutError(), raw_batch([0, 1, 0])])
    fallback = embed_client(
        embed_config(deployments=(deployment("only"),), retry=retry, fallbacks=("fallback",)),
        invocation=fallback_invocation,
    ).embed("x")
    assert fallback.logical_model == "fallback"
    assert fallback.embedding_space == "test-space"


@pytest.mark.asyncio
async def test_async_embedding_retry_and_error_path() -> None:
    retry = RetryPolicyConfig(
        same_deployment_attempts=2,
        max_deployment_failovers=0,
        max_model_fallbacks=0,
        base_delay_seconds=0,
        max_delay_seconds=0,
    )
    invocation = FakeEmbeddingInvocation([TimeoutError(), raw_batch([1, 0, 0])])
    response = await embed_client(embed_config(retry=retry), invocation=invocation).aembed("x")
    assert response.retry_count == 1
    failed = FakeEmbeddingInvocation([HarborEmbedProviderError("safe", retryable=False)])
    with pytest.raises(HarborEmbedProviderError):
        await embed_client(embed_config(retry=retry), invocation=failed).aembed("x")
    assert len(failed.calls) == 1
