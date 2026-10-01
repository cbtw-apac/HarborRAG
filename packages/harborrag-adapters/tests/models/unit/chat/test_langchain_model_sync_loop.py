"""The synchronous LangChain bridge must keep one event loop per client."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from harborrag_adapters.models.chat.langchain import HarborChatModel
from harborrag_adapters.models.runtime.config import ConnectionPoolConfig
from harborrag_adapters.models.runtime.connections import SharedConnectionLifecycle

from .chat_client_support import FakeInvocation, async_client, response_dict
from .langchain_model_support import make_model

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class PooledInvocation(FakeInvocation):
    """Fake backend that touches a real loop-pinned pooled connection lifecycle."""

    def __init__(self, responses: list[Any]) -> None:
        super().__init__(responses)
        self.lifecycle = SharedConnectionLifecycle(
            ConnectionPoolConfig(enabled=True),
            session_factory=lambda _config: object(),
        )
        self.loops: list[asyncio.AbstractEventLoop] = []
        self.aclose_loops: list[asyncio.AbstractEventLoop] = []

    async def acomplete(self, **kwargs: Any) -> Any:
        await self.lifecycle.async_session()
        self.loops.append(asyncio.get_running_loop())
        return await super().acomplete(**kwargs)

    async def aclose(self) -> None:
        self.aclose_loops.append(asyncio.get_running_loop())
        await super().aclose()


def test_repeated_sync_invoke_reuses_one_event_loop(config) -> None:
    invocation = PooledInvocation([response_dict("first"), response_dict("second")])
    model = make_model(config, invocation)

    try:
        first = model.invoke("hi")
        second = model.invoke("again")
    finally:
        model.close()

    assert first.content == "first"
    assert second.content == "second"
    assert len(invocation.loops) == 2
    assert invocation.loops[0] is invocation.loops[1]


@pytest.mark.asyncio
async def test_sync_invoke_inside_running_loop_reuses_one_event_loop(config) -> None:
    invocation = PooledInvocation([response_dict("first"), response_dict("second")])
    model = make_model(config, invocation)

    try:
        first = model.invoke("hi")
        second = model.invoke("again")
    finally:
        await model.aclose()

    assert (first.content, second.content) == ("first", "second")
    assert invocation.loops[0] is invocation.loops[1]
    assert invocation.loops[0] is not asyncio.get_running_loop()


def test_models_sharing_a_client_share_one_event_loop(config) -> None:
    invocation = PooledInvocation([response_dict("one"), response_dict("two")])
    client = async_client(config, backend=invocation)
    first_model = HarborChatModel(client, logical_model="primary")
    second_model = HarborChatModel(client, logical_model="primary")

    try:
        assert first_model.invoke("hi").content == "one"
        assert second_model.invoke("hi").content == "two"
    finally:
        client.close()

    assert invocation.loops[0] is invocation.loops[1]


def test_repeated_sync_stream_reuses_one_event_loop(config) -> None:
    invocation = PooledInvocation([response_dict("first"), response_dict("second")])
    model = make_model(config, invocation)

    try:
        first = "".join(str(chunk.content) for chunk in model.stream("hi"))
        second = "".join(str(chunk.content) for chunk in model.stream("again"))
    finally:
        model.close()

    assert (first, second) == ("first", "second")
    assert invocation.loops[0] is invocation.loops[1]


def test_model_close_releases_resources_on_the_runner_loop(config) -> None:
    invocation = PooledInvocation([response_dict("first")])
    model = make_model(config, invocation)
    model.invoke("hi")
    runner = model.client.sync_runner(thread_name="probe")

    model.close()

    assert invocation.aclose_loops == invocation.loops
    assert not runner._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        model.invoke("again")


@pytest.mark.asyncio
async def test_client_aclose_stops_the_sync_runner(config) -> None:
    invocation = PooledInvocation([response_dict("first")])
    client = async_client(config, backend=invocation)
    model = HarborChatModel(client, logical_model="primary")
    model.invoke("hi")
    runner = client.sync_runner(thread_name="probe")

    await client.aclose()

    assert invocation.aclose_loops == invocation.loops
    assert not runner._thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        runner.run(asyncio.sleep(0))


def test_client_close_without_sync_calls_closes_synchronously(config) -> None:
    invocation = PooledInvocation([])
    client = async_client(config, backend=invocation)

    client.close()

    assert invocation.close_count == 1
    assert client._sync_runner is None
