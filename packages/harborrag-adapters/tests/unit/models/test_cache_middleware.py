from __future__ import annotations

from typing import Any

import pytest
from harborrag_adapters.models.common.cache import (
    CacheDecision,
    InMemoryModelCache,
    ResponseCacheController,
    deterministic_cache_key,
)
from harborrag_adapters.models.common.config import CacheBackend, CacheConfig
from harborrag_adapters.models.common.middleware import (
    MiddlewarePipeline,
    middleware_context,
)
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from pydantic import BaseModel, Field

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class ValueModel(BaseModel):
    """Represent a small immutable-style response used by cache tests."""

    value: int
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    cache_hit: bool = False
    latency_ms: float = 1
    request_id: str | None = None


def test_in_memory_cache_copy_expiry_eviction_and_close() -> None:
    """Verify copies, expiration, eviction, and close behavior."""
    now = [0.0]
    cache = InMemoryModelCache(max_entries=2, clock=lambda: now[0])
    first = ValueModel(value=1)
    cache.set("a", first, 5)
    read = cache.get("a")
    assert read == first and read is not first
    cache.set("b", ValueModel(value=2), 10)
    cache.set("c", ValueModel(value=3), 20)
    assert cache.get("a") is None
    assert cache.get("b") is not None
    now[0] = 11
    assert cache.get("b") is None
    cache.close()
    assert cache.get("c") is None


@pytest.mark.asyncio
async def test_in_memory_cache_async_operations() -> None:
    """Verify asynchronous cache operations use the same semantics."""
    cache = InMemoryModelCache()
    await cache.aset("x", ValueModel(value=1), 10)
    assert await cache.aget("x") == ValueModel(value=1)
    await cache.aclose()
    assert await cache.aget("x") is None


def test_cache_controller_policy_and_metadata() -> None:
    """Verify request policy decisions and cache-hit annotations."""
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        metadata={"tenant_id": "tenant", "request_id": "r1"},
        cacheable=True,
    )
    disabled = ResponseCacheController(CacheConfig(enabled=False), family="chat")
    assert disabled.decision(request, "primary").reason == "disabled"
    controller = ResponseCacheController(CacheConfig(enabled=True, ttl_seconds=12), family="chat")
    decision = controller.decision(request, "primary")
    assert decision.allowed
    response = ValueModel(value=1)
    controller.set(decision, response)
    cached = controller.get(decision)
    assert cached == response
    marked = controller.mark_hit(cached, request_id="r2")
    assert marked.cache_hit and marked.request_id == "r2"
    assert marked.provider_metadata["cache"]["ttl_seconds"] == 12
    assert (
        controller.decision(request.model_copy(update={"sensitive": True}), "primary").reason
        == "sensitive"
    )
    assert (
        controller.decision(request.model_copy(update={"cacheable": False}), "primary").reason
        == "request_bypass"
    )
    no_tenant = request.model_copy(
        update={"metadata": request.metadata.model_copy(update={"tenant_id": None})}
    )
    assert controller.decision(no_tenant, "primary").reason == "missing_tenant"


@pytest.mark.asyncio
async def test_cache_controller_async_and_litellm_controls() -> None:
    """Verify async cache access and LiteLLM cache parameter projection."""
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        metadata={"tenant_id": "tenant"},
        cacheable=True,
    )
    controller = ResponseCacheController(
        CacheConfig(enabled=True, backend=CacheBackend.CUSTOM), family="chat"
    )
    decision = controller.decision(request, "primary")
    await controller.aset(decision, ValueModel(value=2))
    assert await controller.aget(decision) == ValueModel(value=2)
    litellm = ResponseCacheController(
        CacheConfig(enabled=True, backend=CacheBackend.LITELLM), family="chat"
    )
    enabled = litellm.decision(request, "primary")
    assert litellm.provider_parameters(enabled)["caching"] is True
    assert litellm.provider_parameters(CacheDecision(None, "no")) == {"caching": False}
    assert litellm.get(enabled) is None
    assert await litellm.aget(enabled) is None


def test_deterministic_cache_key_is_tenant_aware() -> None:
    """Ensure transient identifiers do not affect keys while tenants do."""
    request = HarborChatRequest(
        messages=(HarborChatMessage.user("hello"),),
        metadata={"request_id": "one", "trace_id": "trace"},
        cacheable=True,
    )
    key_one = deterministic_cache_key(
        family="chat", logical_model="primary", tenant_id="a", request=request
    )
    changed_ids = request.model_copy(
        update={"metadata": request.metadata.model_copy(update={"request_id": "two"})}
    )
    assert key_one == deterministic_cache_key(
        family="chat", logical_model="primary", tenant_id="a", request=changed_ids
    )
    assert key_one != deterministic_cache_key(
        family="chat", logical_model="primary", tenant_id="b", request=request
    )


def test_middleware_order_and_sync_async_guard() -> None:
    """Enforce deterministic middleware order and sync-only hooks."""
    events: list[str] = []

    class Middleware:
        def __init__(self, name: str) -> None:
            self.name = name

        def before_request(self, request: Any, context: Any) -> Any:
            events.append(f"before:{self.name}")
            return request

        def after_response(self, response: Any, context: Any) -> Any:
            events.append(f"after:{self.name}")
            return response

        def on_error(self, error: Exception, context: Any) -> None:
            events.append(f"error:{self.name}")

    request = HarborChatRequest(messages=(HarborChatMessage.user("x"),))
    context = middleware_context(
        operation="chat", logical_model="primary", model_alias="alias", request=request
    )
    pipeline = MiddlewarePipeline((Middleware("a"), Middleware("b")))
    assert pipeline.before(request, context) is request
    response = ValueModel(value=1)
    assert pipeline.after(response, context) is response
    error = RuntimeError("x")
    pipeline.error(error, context)
    assert events == [
        "before:a",
        "before:b",
        "after:b",
        "after:a",
        "error:b",
        "error:a",
    ]

    class BadSync:
        async def before_request(self, request: Any, context: Any) -> Any:
            return request

    with pytest.raises(TypeError, match="async middleware"):
        MiddlewarePipeline((BadSync(),)).before(request, context)


@pytest.mark.asyncio
async def test_async_middleware_and_error_hook_isolation() -> None:
    """Support mixed hooks and isolate error-hook failures."""
    events: list[str] = []

    class Mixed:
        async def before_request(self, request: Any, context: Any) -> Any:
            events.append("before")
            return request

        def after_response(self, response: Any, context: Any) -> Any:
            events.append("after")
            return response

        async def on_error(self, error: Exception, context: Any) -> None:
            events.append("error")
            raise RuntimeError("hook")

    request = HarborChatRequest(messages=(HarborChatMessage.user("x"),))
    context = middleware_context(
        operation="chat", logical_model="primary", model_alias="alias", request=request
    )
    pipeline = MiddlewarePipeline((Mixed(),))
    assert await pipeline.abefore(request, context) is request
    assert await pipeline.aafter(ValueModel(value=1), context) == ValueModel(value=1)
    error = RuntimeError("original")
    await pipeline.aerror(error, context)
    assert events == ["before", "after", "error"]
    assert any("middleware error hooks" in note for note in error.__notes__)
