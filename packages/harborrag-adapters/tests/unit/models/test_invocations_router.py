from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import pytest
from harborrag_adapters.models.chat.invocation import (
    LiteLLMChatInvocation,
    LiteLLMChatRouterInvocation,
)
from harborrag_adapters.models.common.config import (
    BudgetLimitConfig,
    CacheBackend,
    CacheConfig,
    RoutingConfig,
    RoutingStrategy,
)
from harborrag_adapters.models.common.litellm_backend import litellm_routing_strategy
from harborrag_adapters.models.common.litellm_router import (
    build_litellm_router,
    router_model_name,
)
from harborrag_adapters.models.embed.invocation import (
    LiteLLMEmbeddingInvocation,
    LiteLLMEmbeddingRouterInvocation,
)
from harborrag_adapters.models.rerank.invocation import LiteLLMRerankInvocation
from model_runtime_support import chat_config

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


async def _async_value(**kwargs: Any) -> dict[str, Any]:
    return kwargs


class _SyncStream:
    def __init__(self, result: Awaitable[None] | None = None) -> None:
        self.closed = False
        self.result = result

    def close(self) -> Awaitable[None] | None:
        self.closed = True
        return self.result


class _AsyncStream:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Router:
    def __init__(self, *, async_close: bool = True) -> None:
        self.closed = 0
        self.async_close = async_close

    def completion(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    def embedding(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    async def aembedding(self, **kwargs: Any) -> dict[str, Any]:
        return kwargs

    def close(self) -> None:
        self.closed += 1

    def __getattribute__(self, name: str) -> Any:
        if name == "aclose" and not object.__getattribute__(self, "async_close"):
            raise AttributeError(name)
        return object.__getattribute__(self, name)

    async def aclose(self) -> None:
        self.closed += 1


def test_direct_invocations_forward_sync_calls() -> None:
    def echo(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    chat = LiteLLMChatInvocation(echo, _async_value)
    embed = LiteLLMEmbeddingInvocation(echo, _async_value)
    rerank = LiteLLMRerankInvocation(echo, _async_value)

    assert chat.complete(model="chat") == {"model": "chat"}
    assert chat.stream(model="stream") == {"model": "stream"}
    assert embed.embed(model="embed") == {"model": "embed"}
    assert rerank.rerank(model="rerank") == {"model": "rerank"}
    assert chat.close() is None
    assert embed.close() is None
    assert rerank.close() is None


@pytest.mark.asyncio
async def test_direct_invocations_forward_async_calls_and_close() -> None:
    chat = LiteLLMChatInvocation(lambda **kwargs: kwargs, _async_value)
    embed = LiteLLMEmbeddingInvocation(lambda **kwargs: kwargs, _async_value)
    rerank = LiteLLMRerankInvocation(lambda **kwargs: kwargs, _async_value)

    assert await chat.acomplete(model="chat") == {"model": "chat"}
    assert await chat.astream(model="stream") == {"model": "stream"}
    assert await embed.aembed(model="embed") == {"model": "embed"}
    assert await rerank.arerank(model="rerank") == {"model": "rerank"}
    assert await chat.aclose() is None
    assert await embed.aclose() is None
    assert await rerank.aclose() is None


def test_sync_stream_cleanup_supports_sync_and_async_only_streams() -> None:
    sync_stream = _SyncStream()
    async_stream = _AsyncStream()
    invocation = LiteLLMChatInvocation(lambda **kwargs: kwargs, _async_value)

    invocation.close_stream(sync_stream)
    invocation.close_stream(async_stream)

    assert sync_stream.closed is True
    assert async_stream.closed is True


@pytest.mark.asyncio
async def test_sync_stream_cleanup_resolves_awaitable_inside_running_loop() -> None:
    closed = False

    async def mark_closed() -> None:
        nonlocal closed
        closed = True

    stream = _SyncStream(mark_closed())
    invocation = LiteLLMChatInvocation(lambda **kwargs: kwargs, _async_value)

    invocation.close_stream(stream)

    assert stream.closed is True
    assert closed is True


@pytest.mark.asyncio
async def test_async_stream_cleanup_supports_async_and_awaitable_sync_close() -> None:
    async_stream = _AsyncStream()
    closed = False

    async def mark_closed() -> None:
        nonlocal closed
        closed = True

    sync_stream = _SyncStream(mark_closed())
    invocation = LiteLLMChatInvocation(lambda **kwargs: kwargs, _async_value)

    await invocation.aclose_stream(async_stream)
    await invocation.aclose_stream(sync_stream)

    assert async_stream.closed is True
    assert sync_stream.closed is True
    assert closed is True


@pytest.mark.parametrize(
    ("invocation_type", "sync_method", "async_method"),
    [
        (LiteLLMChatRouterInvocation, "complete", "acomplete"),
        (LiteLLMEmbeddingRouterInvocation, "embed", "aembed"),
    ],
)
@pytest.mark.asyncio
async def test_router_invocations_forward_and_close(
    invocation_type: type[Any],
    sync_method: str,
    async_method: str,
) -> None:
    router = _Router()
    invocation = invocation_type(router)

    assert getattr(invocation, sync_method)(model="sync") == {"model": "sync"}
    assert await getattr(invocation, async_method)(model="async") == {"model": "async"}
    invocation.close()
    await invocation.aclose()

    expected_closes = 1 if invocation_type is LiteLLMChatRouterInvocation else 2
    assert router.closed == expected_closes


@pytest.mark.parametrize(
    "invocation_type",
    [LiteLLMChatRouterInvocation, LiteLLMEmbeddingRouterInvocation],
)
@pytest.mark.asyncio
async def test_router_async_close_falls_back_to_sync(
    invocation_type: type[Any],
) -> None:
    router = _Router(async_close=False)
    invocation = invocation_type(router)

    await invocation.aclose()

    assert router.closed == 1


def test_router_model_name_is_private_and_stable() -> None:
    assert router_model_name("primary", "east") == "harbor::primary::east"


@pytest.mark.parametrize(
    ("strategy", "expected"),
    [
        (RoutingStrategy.WEIGHTED, "simple-shuffle"),
        (RoutingStrategy.ORDERED, "simple-shuffle"),
        (RoutingStrategy.LEAST_BUSY, "least-busy"),
        (RoutingStrategy.LATENCY, "latency-based-routing"),
    ],
)
def test_litellm_routing_strategy_mapping(
    strategy: RoutingStrategy,
    expected: str,
) -> None:
    assert litellm_routing_strategy(strategy) == expected


def test_litellm_routing_strategy_rejects_round_robin() -> None:
    with pytest.raises(ValueError, match="unsupported LiteLLM routing strategy"):
        litellm_routing_strategy(RoutingStrategy.ROUND_ROBIN)


def test_build_litellm_router_translates_deployments_budgets_and_cache() -> None:
    config = chat_config(
        routing=RoutingConfig(strategy=RoutingStrategy.LEAST_BUSY),
        cache=CacheConfig(enabled=True, backend=CacheBackend.LITELLM, ttl_seconds=42),
    ).model_copy(
        update={
            "provider_budgets": {"openai": BudgetLimitConfig(max_budget=10, budget_duration="1d")}
        }
    )
    logical = config.models["primary"]
    disabled = logical.deployments[0].model_copy(update={"name": "disabled", "enabled": False})
    models = {
        "primary": logical.model_copy(update={"deployments": (*logical.deployments, disabled)})
    }
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    result = build_litellm_router(
        config,
        models,
        provider_resolver=lambda deployment: deployment.provider.value,
        router_factory=factory,
    )

    assert type(result) is object
    assert captured["routing_strategy"] == "least-busy"
    assert captured["num_retries"] == 0
    assert captured["max_fallbacks"] == 0
    assert captured["disable_cooldowns"] is True
    assert captured["cache_responses"] is True
    assert captured["cache_kwargs"] == {"ttl": 42}
    assert len(captured["model_list"]) == 1
    model_entry = captured["model_list"][0]
    assert model_entry["model_name"] == "harbor::primary::openai-a"
    assert model_entry["litellm_params"]["custom_llm_provider"] == "openai"
    assert "openai" in captured["provider_budget_config"]
