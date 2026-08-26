from __future__ import annotations

import asyncio
import threading
from typing import Any

import litellm
import pytest

from harborrag_adapters.models.chat.backends import (
    LiteLLMDirectBackend,
    LiteLLMRouterBackend,
)
from harborrag_adapters.models.embed.invocation import (
    LiteLLMEmbeddingInvocation,
    LiteLLMEmbeddingRouterInvocation,
)
from harborrag_adapters.models.rerank.invocation import LiteLLMRerankInvocation
from harborrag_adapters.models.runtime.config import (
    ConnectionPoolConfig,
    RetryPolicyConfig,
)
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_adapters.models.runtime.responses import (
    coerce_sdk_mapping,
    sdk_hidden_parameters,
)
from harborrag_adapters.models.runtime.retry import RetryController
from harborrag_adapters.models.runtime.sync import (
    AsyncLoopRunner,
    run_awaitable_synchronously,
)

pytestmark = [pytest.mark.unit, pytest.mark.whitebox]


class Router:
    """Expose injectable model-family methods and lifecycle counters."""

    def __init__(self) -> None:
        self.closed = 0
        self.aclosed = 0

    def completion(self, **kwargs: Any) -> dict[str, Any]:
        """Echo one synchronous completion call."""

        return kwargs

    async def acompletion(self, **kwargs: Any) -> dict[str, Any]:
        """Echo one asynchronous completion call."""

        return kwargs

    def embedding(self, **kwargs: Any) -> dict[str, Any]:
        """Echo one synchronous embedding call."""

        return kwargs

    async def aembedding(self, **kwargs: Any) -> dict[str, Any]:
        """Echo one asynchronous embedding call."""

        return kwargs

    def close(self) -> None:
        """Record synchronous closure."""

        self.closed += 1

    async def aclose(self) -> None:
        """Record asynchronous closure."""

        self.aclosed += 1


class SyncOnlyRouter(Router):
    """Hide asynchronous closure to exercise the synchronous fallback."""

    aclose = None


def direct_chat_backend(
    completion=None,
    acompletion=None,
) -> LiteLLMDirectBackend:
    return LiteLLMDirectBackend(
        connections=SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False)),
        connection_ownership=ResourceOwnership.OWNED,
        completion=completion,
        acompletion=acompletion,
    )


def router_chat_backend(router: Any) -> LiteLLMRouterBackend:
    return LiteLLMRouterBackend(
        router,
        connections=SharedConnectionLifecycle(ConnectionPoolConfig(enabled=False)),
        connection_ownership=ResourceOwnership.OWNED,
    )


class MappingModel:
    """Provide a Pydantic-like mapping conversion method."""

    def __init__(self, result: object) -> None:
        self.result = result

    def model_dump(self) -> object:
        """Return the configured conversion result."""

        return self.result


class LegacyMappingModel:
    """Provide the legacy SDK dictionary conversion method."""

    def dict(self) -> dict[str, int]:
        """Return a plain mapping."""

        return {"legacy": 1}


def test_response_mapping_coercion_covers_sdk_shapes() -> None:
    assert coerce_sdk_mapping(None) == {}
    assert coerce_sdk_mapping({"plain": 1}) == {"plain": 1}
    assert coerce_sdk_mapping(MappingModel({"dumped": 1})) == {"dumped": 1}
    assert coerce_sdk_mapping(MappingModel("invalid")) == {}
    assert coerce_sdk_mapping(LegacyMappingModel()) == {"legacy": 1}
    assert coerce_sdk_mapping(object()) == {}

    hidden_object = type("Hidden", (), {"_hidden_params": {"region": "west"}})()
    assert sdk_hidden_parameters(hidden_object, {}) == {"region": "west"}
    assert sdk_hidden_parameters(object(), {"_hidden_params": {"cached": True}}) == {"cached": True}


@pytest.mark.asyncio
async def test_default_litellm_invocations_use_sdk_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(litellm, "completion", lambda **kwargs: ("chat", kwargs))

    async def acompletion(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "achat", kwargs

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    chat = direct_chat_backend()
    assert chat.complete(model="one") == ("chat", {"model": "one"})
    assert await chat.acomplete(model="two") == ("achat", {"model": "two"})

    monkeypatch.setattr(litellm, "embedding", lambda **kwargs: ("embed", kwargs))

    async def aembedding(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "aembed", kwargs

    monkeypatch.setattr(litellm, "aembedding", aembedding)
    embed = LiteLLMEmbeddingInvocation()
    assert embed.embed(model="one") == ("embed", {"model": "one"})
    assert await embed.aembed(model="two") == ("aembed", {"model": "two"})
    assert embed.close() is None
    assert await embed.aclose() is None

    monkeypatch.setattr(litellm, "rerank", lambda **kwargs: ("rerank", kwargs))

    async def arerank(**kwargs: Any) -> tuple[str, dict[str, Any]]:
        return "arerank", kwargs

    monkeypatch.setattr(litellm, "arerank", arerank)
    rerank = LiteLLMRerankInvocation()
    assert rerank.rerank(model="one") == ("rerank", {"model": "one"})
    assert await rerank.arerank(model="two") == ("arerank", {"model": "two"})
    assert rerank.close() is None
    assert await rerank.aclose() is None


@pytest.mark.asyncio
async def test_router_invocations_forward_lifecycle_methods() -> None:
    router = Router()
    chat = router_chat_backend(router)
    assert chat.complete(model="chat") == {"model": "chat"}
    chat.close()
    await chat.aclose()
    assert (router.closed, router.aclosed) == (1, 0)

    embed = LiteLLMEmbeddingRouterInvocation(router)
    assert embed.embed(model="embed") == {"model": "embed"}
    embed.close()
    await embed.aclose()
    assert (router.closed, router.aclosed) == (2, 1)

    sync_router = SyncOnlyRouter()
    await router_chat_backend(sync_router).aclose()
    await LiteLLMEmbeddingRouterInvocation(sync_router).aclose()
    assert sync_router.closed == 2


def test_chat_stream_cleanup_accepts_sync_and_async_close() -> None:
    invocation = direct_chat_backend(lambda **_: None, lambda **_: None)
    state = {"sync": False, "async": False}

    class SyncStream:
        def close(self) -> None:
            state["sync"] = True

    class AsyncStream:
        async def aclose(self) -> None:
            state["async"] = True

    invocation.close_stream(SyncStream())
    invocation.close_stream(AsyncStream())
    invocation.close_stream(object())
    assert state == {"sync": True, "async": True}


@pytest.mark.asyncio
async def test_async_stream_cleanup_accepts_async_and_sync_fallbacks() -> None:
    invocation = direct_chat_backend(lambda **_: None, lambda **_: None)
    state = {"sync": False, "async": False, "awaited": False}

    class AsyncStream:
        async def aclose(self) -> None:
            state["async"] = True

    class SyncStream:
        def close(self) -> None:
            state["sync"] = True

    class AwaitableSyncStream:
        async def _close(self) -> None:
            state["awaited"] = True

        def close(self) -> Any:
            return self._close()

    await invocation.aclose_stream(AsyncStream())
    await invocation.aclose_stream(SyncStream())
    await invocation.aclose_stream(AwaitableSyncStream())
    await invocation.aclose_stream(object())
    assert all(state.values())


def test_async_loop_runner_owns_loop_and_rejects_work_after_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_thread_id = threading.get_ident()
    loop_creation_thread_ids: list[int] = []
    new_event_loop = asyncio.new_event_loop

    def record_loop_creation() -> asyncio.AbstractEventLoop:
        loop_creation_thread_ids.append(threading.get_ident())
        return new_event_loop()

    monkeypatch.setattr(asyncio, "new_event_loop", record_loop_creation)
    runner = AsyncLoopRunner(thread_name="harbor-test-loop")

    async def loop_identity() -> tuple[int, int]:
        return id(asyncio.get_running_loop()), threading.get_ident()

    assert runner.run(asyncio.sleep(0, result=3)) == 3
    assert runner.submit(asyncio.sleep(0, result=4)).result() == 4
    first_identity = runner.run(loop_identity())
    assert first_identity == runner.run(loop_identity())
    assert loop_creation_thread_ids == [first_identity[1]]
    assert loop_creation_thread_ids != [caller_thread_id]
    runner.stop()
    runner.stop()
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(asyncio.sleep(0, result=5))


@pytest.mark.asyncio
async def test_sync_awaitable_bridge_propagates_thread_errors() -> None:
    async def fail() -> None:
        raise LookupError("bridge failure")

    with pytest.raises(LookupError, match="bridge failure"):
        run_awaitable_synchronously(fail(), thread_name="harbor-test-bridge")


@pytest.mark.asyncio
async def test_retry_controller_sync_and_async_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = RetryPolicyConfig(base_delay_seconds=1, max_delay_seconds=2, jitter_ratio=0)
    controller = RetryController(policy)
    sync_delays: list[float] = []
    async_delays: list[float] = []

    monkeypatch.setattr("harborrag_adapters.models.runtime.retry.time.sleep", sync_delays.append)

    async def record_sleep(delay: float) -> None:
        async_delays.append(delay)

    monkeypatch.setattr("harborrag_adapters.models.runtime.retry.asyncio.sleep", record_sleep)
    assert controller.delay_seconds(-1) == 1
    assert controller.delay_seconds(3) == 2
    controller.sleep_sync(1)
    await controller.sleep(2)
    assert sync_delays == [1]
    assert async_delays == [2]
