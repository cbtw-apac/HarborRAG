from __future__ import annotations

from typing import Any

import pytest
from chat.chat_client_support import sync_client

from harborrag_adapters.models.runtime.cache import InMemoryModelCache, deterministic_cache_key
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest

from .fakes import Clock, Invocation, response, runtime_config

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_cache_hit_miss_and_request_id_refresh() -> None:
    invocation = Invocation([response("cached")])
    client = sync_client(runtime_config(cache=True), backend=invocation)
    kwargs = {"cacheable": True, "metadata": {"tenant_id": "tenant-a"}}

    first = client.chat([HarborChatMessage.user("hello")], **kwargs)
    second = client.chat([HarborChatMessage.user("hello")], **kwargs)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.request_id != second.request_id
    assert len(invocation.calls) == 1


def test_cache_bypass_and_sensitive_default() -> None:
    invocation = Invocation([response("one"), response("two"), response("three")])
    client = sync_client(runtime_config(cache=True), backend=invocation)

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


def test_singleflight_engages_only_for_cache_eligible_requests() -> None:
    class SpyCoordinator:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def execute(self, key: str, producer: Any, follower_loader: Any) -> Any:
            del follower_loader
            self.keys.append(key)

            class Result:
                value = producer()
                shared = False

            return Result()

        async def aexecute(self, key: str, producer: Any, follower_loader: Any) -> Any:
            raise AssertionError("async path is not exercised")

        def close(self) -> None: ...

        async def aclose(self) -> None: ...

    spy = SpyCoordinator()
    invocation = Invocation([response("direct"), response("deduplicated")])
    client = sync_client(runtime_config(cache=True), backend=invocation, singleflight=spy)

    direct = client.chat(
        [HarborChatMessage.user("hello")],
        metadata={"request_id": "shared-id", "tenant_id": "tenant"},
    )
    eligible = client.chat(
        [HarborChatMessage.user("hello")],
        cacheable=True,
        metadata={"request_id": "shared-id", "tenant_id": "tenant"},
    )

    assert direct.text == "direct"
    assert eligible.text == "deduplicated"
    assert len(spy.keys) == 1
    assert "shared-id" not in spy.keys[0]


def test_cache_ttl_and_tenant_isolation() -> None:
    clock = Clock()
    backend = InMemoryModelCache(clock=clock)
    invocation = Invocation([response("a"), response("b"), response("expired")])
    client = sync_client(runtime_config(cache=True, ttl=1), backend=invocation, cache=backend)
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
