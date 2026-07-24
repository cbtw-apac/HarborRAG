from __future__ import annotations

import pytest

from harborrag_adapters.models.chat import HarborChatClient
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_core.models.chat import HarborChatMessage

from .chat_client_support import FakeInvocation, response_dict

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_owned_sync_lifecycle_is_idempotent(base_config) -> None:
    invocation = FakeInvocation()
    client = HarborChatClient(base_config, invocation=invocation)

    client.close()
    client.close()

    assert invocation.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.chat([HarborChatMessage.user("hello")])


@pytest.mark.asyncio
async def test_owned_async_context_closes_once(base_config) -> None:
    invocation = FakeInvocation([response_dict()])

    async with HarborChatClient(base_config, invocation=invocation) as client:
        await client.achat([HarborChatMessage.user("hello")])
    await client.aclose()

    assert invocation.aclose_count == 1


def test_borrowed_invocation_is_not_closed(base_config) -> None:
    invocation = FakeInvocation()

    with HarborChatClient(
        base_config,
        invocation=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    ):
        pass

    assert invocation.close_count == 0
