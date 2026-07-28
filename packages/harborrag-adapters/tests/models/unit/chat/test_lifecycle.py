from __future__ import annotations

import pytest

from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_core.models.chat import HarborChatMessage

from .chat_client_support import (
    FakeInvocation,
    async_client,
    response_dict,
    sync_client,
)

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_owned_sync_lifecycle_is_idempotent(base_config) -> None:
    invocation = FakeInvocation()
    client = sync_client(base_config, backend=invocation)

    client.close()
    client.close()

    assert invocation.close_count == 1
    with pytest.raises(RuntimeError, match="closed"):
        client.chat([HarborChatMessage.user("hello")])


@pytest.mark.asyncio
async def test_owned_async_context_closes_once(base_config) -> None:
    invocation = FakeInvocation([response_dict()])

    async with async_client(base_config, backend=invocation) as client:
        await client.achat([HarborChatMessage.user("hello")])
    await client.aclose()

    assert invocation.aclose_count == 1


def test_borrowed_invocation_is_not_closed(base_config) -> None:
    invocation = FakeInvocation()

    with sync_client(
        base_config,
        backend=invocation,
        resource_ownership=ResourceOwnership.BORROWED,
    ):
        pass

    assert invocation.close_count == 0
