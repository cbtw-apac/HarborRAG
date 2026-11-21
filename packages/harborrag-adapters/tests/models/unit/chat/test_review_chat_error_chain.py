from __future__ import annotations

import pytest
from model_runtime_support import FakeChatInvocation, chat_config

from harborrag_core.models.chat import HarborChatMessage
from harborrag_core.models.errors import HarborChatError, HarborChatInvalidRequestError

from .chat_client_support import async_client, sync_client

pytestmark = pytest.mark.unit


def _refused() -> HarborChatInvalidRequestError:
    return HarborChatInvalidRequestError("request refused", retryable=False)


def _assert_finite_cause_chain(error: BaseException) -> None:
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None:
        assert id(current) not in seen, "the cause chain loops back on itself"
        seen.add(id(current))
        current = current.__cause__


def test_chat_harbor_error_is_not_its_own_cause() -> None:
    client = sync_client(chat_config(), backend=FakeChatInvocation([_refused()]))

    with pytest.raises(HarborChatError) as caught:
        client.chat([HarborChatMessage.user("hello")])

    _assert_finite_cause_chain(caught.value)


def test_chat_stream_harbor_error_is_not_its_own_cause() -> None:
    client = sync_client(chat_config(), backend=FakeChatInvocation([_refused()]))

    with pytest.raises(HarborChatError) as caught:
        list(client.stream([HarborChatMessage.user("hello")]))

    _assert_finite_cause_chain(caught.value)


@pytest.mark.asyncio
async def test_async_chat_stream_harbor_error_is_not_its_own_cause() -> None:
    client = async_client(chat_config(), backend=FakeChatInvocation([_refused()]))

    with pytest.raises(HarborChatError) as caught:
        async for _chunk in client.astream([HarborChatMessage.user("hello")]):
            pass

    _assert_finite_cause_chain(caught.value)
