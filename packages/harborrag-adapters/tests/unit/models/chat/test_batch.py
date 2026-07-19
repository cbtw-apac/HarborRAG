from __future__ import annotations

import asyncio
from typing import Any

import pytest
from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.chat.batch import BatchFailureMode
from harborrag_core.models.chat import HarborChatMessage, HarborChatRequest
from harborrag_core.models.errors import HarborChatProviderError
from model_runtime_support import FakeChatInvocation, chat_config

from .test_client_execution import raw_chat

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def requests(count: int) -> tuple[HarborChatRequest, ...]:
    return tuple(
        HarborChatRequest(
            messages=(HarborChatMessage.user(f"request-{index}"),),
            logical_model="primary",
        )
        for index in range(count)
    )


def test_chat_many_preserves_order_and_collects_item_errors() -> None:
    invocation = FakeChatInvocation(
        [raw_chat("first"), ValueError("bad provider result"), raw_chat("third")]
    )
    client = HarborChatClient(chat_config(), invocation=invocation)

    result = client.chat_many(requests(3), concurrency=1)

    assert [item.index for item in result.items] == [0, 1, 2]
    assert [response.text for response in result.responses] == ["first", "third"]
    assert len(result.errors) == 1


def test_chat_many_fail_fast_raises_normalized_error() -> None:
    invocation = FakeChatInvocation([ValueError("provider failed")])
    client = HarborChatClient(chat_config(), invocation=invocation)

    with pytest.raises(HarborChatProviderError):
        client.chat_many(
            requests(1),
            concurrency=1,
            failure_mode=BatchFailureMode.FAIL_FAST,
        )


@pytest.mark.asyncio
async def test_achat_many_bounds_concurrency_and_preserves_order() -> None:
    active = 0
    maximum = 0

    class Invocation(FakeChatInvocation):
        async def acomplete(self, **kwargs: Any) -> Any:
            nonlocal active, maximum
            self.calls.append(kwargs)
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return raw_chat(kwargs["messages"][0]["content"])

    client = HarborChatClient(chat_config(), invocation=Invocation([]))

    result = await client.achat_many(requests(5), concurrency=2)

    assert maximum == 2
    assert [response.text for response in result.responses] == [
        f"request-{index}" for index in range(5)
    ]


@pytest.mark.asyncio
async def test_achat_many_fail_fast_cancels_sibling_tasks() -> None:
    cancelled = asyncio.Event()

    class Invocation(FakeChatInvocation):
        async def acomplete(self, **kwargs: Any) -> Any:
            content = kwargs["messages"][0]["content"]
            if content == "request-0":
                raise ValueError("stop batch")
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return raw_chat(content)

    client = HarborChatClient(chat_config(), invocation=Invocation([]))

    with pytest.raises(HarborChatProviderError):
        await client.achat_many(
            requests(2),
            concurrency=2,
            failure_mode=BatchFailureMode.FAIL_FAST,
        )
    assert cancelled.is_set()


def test_chat_many_rejects_empty_requests_and_invalid_concurrency() -> None:
    client = HarborChatClient(chat_config(), invocation=FakeChatInvocation([]))

    with pytest.raises(Exception, match="at least one"):
        client.chat_many(())
    with pytest.raises(Exception, match="positive integer"):
        client.chat_many(requests(1), concurrency=0)
