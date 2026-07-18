from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.models.chat import HarborChatClient, HarborChatClientConfig
from harborrag_adapters.models.common.cache import (
    InMemoryModelCache,
    deterministic_cache_key,
)
from harborrag_adapters.models.common.litellm_router import build_litellm_router
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.errors import (
    HarborChatAuthenticationError,
    HarborChatRateLimitError,
    HarborChatTimeoutError,
)
from pydantic import ValidationError


class Invocation:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []
        self.async_calls: list[dict[str, Any]] = []

    def complete(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self._next()

    async def acomplete(self, **kwargs: Any) -> Any:
        self.async_calls.append(kwargs)
        return self._next()

    def _next(self) -> Any:
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None: ...

    async def aclose(self) -> None: ...


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def response(text: str = "ok") -> dict[str, Any]:
    return {
        "id": f"response-{text}",
        "model": "provider-model",
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def runtime_config(
    *,
    deployments: int = 1,
    fallback: bool = False,
    attempts: int = 1,
    cache: bool = False,
    ttl: int = 30,
) -> HarborChatClientConfig:
    primary: dict[str, Any] = {
        "fallbacks": ["secondary"] if fallback else [],
        "deployments": [
            {
                "name": f"primary-{index}",
                "provider": "openai",
                "model": f"openai/model-{index}",
                "api_key": "key",
                "order": index,
            }
            for index in range(deployments)
        ],
    }
    models: dict[str, Any] = {"primary": primary}
    if fallback:
        models["secondary"] = {
            "provider": "openai",
            "model": "openai/fallback",
            "api_key": "key",
        }
    return HarborChatClientConfig.from_dict(
        {
            "default_model": "primary",
            "retry": {
                "same_deployment_attempts": attempts,
                "max_deployment_failovers": 5,
                "max_model_fallbacks": 5,
                "base_delay_seconds": 0,
                "max_delay_seconds": 0,
            },
            "routing": {"strategy": "ordered"},
            "cache": {"enabled": cache, "ttl_seconds": ttl},
            "models": models,
        }
    )


def test_same_deployment_retry_is_distinct_from_fallback() -> None:
    invocation = Invocation([HarborChatRateLimitError("limited"), response()])

    result = HarborChatClient(runtime_config(attempts=2), invocation=invocation).chat(
        [HarborChatMessage.user("hello")]
    )

    assert result.retry_count == 1
    assert result.fallback_count == 0
    assert result.provider_metadata["routing"] == {
        "same_deployment_retries": 1,
        "deployment_failovers": 0,
        "model_fallbacks": 0,
    }


def test_non_retryable_failure_stops_immediately() -> None:
    invocation = Invocation([HarborChatAuthenticationError("bad credentials")])

    with pytest.raises(HarborChatAuthenticationError):
        HarborChatClient(runtime_config(attempts=3), invocation=invocation).chat(
            [HarborChatMessage.user("hello")]
        )

    assert len(invocation.calls) == 1


def test_deployment_failover_is_reported_separately() -> None:
    invocation = Invocation([TimeoutError("slow"), response()])

    result = HarborChatClient(runtime_config(deployments=2), invocation=invocation).chat(
        [HarborChatMessage.user("hello")]
    )

    assert [call["model"] for call in invocation.calls] == [
        "openai/model-0",
        "openai/model-1",
    ]
    assert result.deployment == "primary-1"
    assert result.provider_metadata["routing"]["deployment_failovers"] == 1
    assert result.provider_metadata["routing"]["model_fallbacks"] == 0


@pytest.mark.asyncio
async def test_async_logical_model_fallback() -> None:
    invocation = Invocation([TimeoutError("slow"), response("fallback")])

    result = await HarborChatClient(runtime_config(fallback=True), invocation=invocation).achat(
        [HarborChatMessage.user("hello")]
    )

    assert result.logical_model == "secondary"
    assert result.text == "fallback"
    assert result.provider_metadata["routing"]["model_fallbacks"] == 1


def test_maximum_retry_exhaustion_preserves_timeout_error() -> None:
    invocation = Invocation([TimeoutError("slow"), TimeoutError("still slow")])

    with pytest.raises(HarborChatTimeoutError):
        HarborChatClient(runtime_config(attempts=2), invocation=invocation).chat(
            [HarborChatMessage.user("hello")]
        )

    assert len(invocation.calls) == 2


def test_cache_hit_miss_and_request_id_refresh() -> None:
    invocation = Invocation([response("cached")])
    client = HarborChatClient(runtime_config(cache=True), invocation=invocation)
    kwargs = {"cacheable": True, "metadata": {"tenant_id": "tenant-a"}}

    first = client.chat([HarborChatMessage.user("hello")], **kwargs)
    second = client.chat([HarborChatMessage.user("hello")], **kwargs)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.request_id != second.request_id
    assert len(invocation.calls) == 1


def test_cache_bypass_and_sensitive_default() -> None:
    invocation = Invocation([response("one"), response("two"), response("three")])
    client = HarborChatClient(runtime_config(cache=True), invocation=invocation)

    client.chat([HarborChatMessage.user("hello")], metadata={"tenant_id": "tenant"})
    client.chat(
        [HarborChatMessage.user("hello")],
        cacheable=True,
        sensitive=True,
        metadata={"tenant_id": "tenant"},
    )
    client.chat(
        [HarborChatMessage.user("hello")],
        cacheable=True,
        sensitive=True,
        metadata={"tenant_id": "tenant"},
    )

    assert len(invocation.calls) == 3


def test_cache_ttl_and_tenant_isolation() -> None:
    clock = Clock()
    backend = InMemoryModelCache(clock=clock)
    invocation = Invocation([response("a"), response("b"), response("expired")])
    client = HarborChatClient(
        runtime_config(cache=True, ttl=1), invocation=invocation, cache=backend
    )
    request = [HarborChatMessage.user("hello")]

    first = client.chat(request, cacheable=True, metadata={"tenant_id": "a"})
    other = client.chat(request, cacheable=True, metadata={"tenant_id": "b"})
    clock.value = 2
    expired = client.chat(request, cacheable=True, metadata={"tenant_id": "a"})

    assert (first.text, other.text, expired.text) == ("a", "b", "expired")
    assert len(invocation.calls) == 3


def test_cache_keys_are_deterministic_and_tenant_partitioned() -> None:
    request_one = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        metadata={"request_id": "one", "tenant_id": "tenant-a"},
    )
    request_two = request_one.model_copy(
        update={"metadata": request_one.metadata.model_copy(update={"request_id": "two"})}
    )

    key_one = deterministic_cache_key(
        family="chat",
        logical_model="primary",
        tenant_id="tenant-a",
        request=request_one,
    )
    key_two = deterministic_cache_key(
        family="chat",
        logical_model="primary",
        tenant_id="tenant-a",
        request=request_two,
    )
    other_tenant = deterministic_cache_key(
        family="chat",
        logical_model="primary",
        tenant_id="tenant-b",
        request=request_two,
    )

    assert key_one == key_two
    assert key_one != other_tenant


def test_circular_fallback_graph_is_rejected() -> None:
    with pytest.raises(ValidationError, match="circular chat fallback chain"):
        HarborChatClientConfig.from_dict(
            {
                "default_model": "a",
                "models": {
                    "a": {
                        "fallbacks": ["b"],
                        "provider": "openai",
                        "model": "openai/a",
                        "api_key": "key",
                    },
                    "b": {
                        "fallbacks": ["a"],
                        "provider": "openai",
                        "model": "openai/b",
                        "api_key": "key",
                    },
                },
            }
        )


def test_litellm_router_disables_opaque_retry_and_forwards_budgets() -> None:
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> object:
        captured.update(kwargs)
        return object()

    base = runtime_config(deployments=2).model_dump(mode="python")
    base["provider_budgets"] = {"openai": {"rpm_limit": 100}}
    config = HarborChatClientConfig.model_validate(base)

    build_litellm_router(
        config,
        config.models,
        provider_resolver=lambda _deployment: "openai",
        router_factory=factory,
    )

    assert captured["num_retries"] == 0
    assert captured["max_fallbacks"] == 0
    assert captured["cache_responses"] is False
    assert captured["provider_budget_config"]["openai"].model_dump(exclude_none=True) == {
        "rpm_limit": 100
    }
    assert [item["model_name"] for item in captured["model_list"]] == [
        "harbor::primary::primary-0",
        "harbor::primary::primary-1",
    ]
