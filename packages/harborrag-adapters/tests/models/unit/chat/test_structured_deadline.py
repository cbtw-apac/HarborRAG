"""Deadlines must span routing retries and validation repair, not reset per call."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel

from harborrag_adapters.models.runtime import operation_deadline as deadline_module
from harborrag_adapters.models.runtime.config import TimeoutConfig
from harborrag_core.models.capabilities import HarborChatCapabilities
from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import HarborChatTimeoutError

from .chat_client_support import FakeInvocation, async_client, response_dict, sync_client
from .test_topology_model_prerequisites import configured

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


class Answer(BaseModel):
    answer: str


def bounded_config(base_config, seconds=0.05):
    config = configured(base_config, capabilities=HarborChatCapabilities(structured_output=True))
    return config.model_copy(
        update={"timeouts": TimeoutConfig(request_seconds=17, operation_seconds=seconds)}
    )


@pytest.mark.asyncio
async def test_async_deadline_cancels_repair_and_releases_admission(base_config) -> None:
    blocked = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending():
        try:
            await blocked.wait()
        finally:
            cancelled.set()

    config = bounded_config(base_config)
    config = configured(config, max_parallel_requests=1)
    backend = FakeInvocation(
        [response_dict("invalid"), pending, response_dict('{"answer":"next"}')]
    )
    client = async_client(config, backend=backend)
    try:
        with pytest.raises(HarborChatTimeoutError, match="operation deadline"):
            await client.achat_structured(
                [HarborChatMessage.user("extract")], response_model=Answer
            )
        assert cancelled.is_set()
        assert len(backend.async_calls) == 2
        result = await client.achat_structured(
            [HarborChatMessage.user("retry independently")], response_model=Answer
        )
        assert result.answer == "next"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_deadline_includes_retry_backoff(base_config) -> None:
    config = bounded_config(base_config)
    config = config.model_copy(
        update={
            "retry": config.retry.model_copy(update={"base_delay_seconds": 1, "jitter_ratio": 0})
        }
    )
    backend = FakeInvocation([TimeoutError("provider timed out")])
    client = async_client(config, backend=backend)
    try:
        with pytest.raises(HarborChatTimeoutError, match="operation deadline"):
            await client.achat_structured(
                [HarborChatMessage.user("extract")], response_model=Answer
            )
        assert len(backend.async_calls) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_async_deadline_includes_waiting_for_local_admission(base_config) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def occupying_call():
        entered.set()
        await release.wait()
        return response_dict("ordinary chat")

    config = configured(bounded_config(base_config), max_parallel_requests=1)
    backend = FakeInvocation([occupying_call])
    client = async_client(config, backend=backend)
    occupying = asyncio.create_task(client.achat([HarborChatMessage.user("occupy")]))
    try:
        await entered.wait()
        with pytest.raises(HarborChatTimeoutError, match="operation deadline"):
            await client.achat_structured(
                [HarborChatMessage.user("extract")], response_model=Answer
            )
        assert len(backend.async_calls) == 1
    finally:
        release.set()
        await occupying
        await client.aclose()


def test_sync_deadline_carries_remaining_budget_into_repairs(base_config, monkeypatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(deadline_module.time, "monotonic", lambda: clock[0])

    def first():
        clock[0] += 2
        return response_dict("invalid")

    def second():
        clock[0] += 4
        return response_dict('{"answer":"too late"}')

    backend = FakeInvocation([first, second])
    config = bounded_config(base_config, seconds=5)
    with pytest.raises(HarborChatTimeoutError, match="operation deadline"):
        sync_client(config, backend=backend).chat_structured(
            [HarborChatMessage.user("extract")], response_model=Answer
        )
    assert [call["timeout"] for call in backend.calls] == [5.0, 3.0]
    assert deadline_module.remaining_timeout() is None
