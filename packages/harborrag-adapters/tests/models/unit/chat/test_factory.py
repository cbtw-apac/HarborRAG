from __future__ import annotations

import pytest

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClient,
)

from .chat_client_support import FakeInvocation

pytestmark = [pytest.mark.unit, pytest.mark.graybox]


def test_factory_creates_explicit_sync_and_async_clients(base_config) -> None:
    dependencies = ChatClientDependencies(invocation=FakeInvocation())

    sync_client = ChatClientFactory.create(base_config, dependencies)
    async_client = ChatClientFactory.create_async(base_config, dependencies)

    assert isinstance(sync_client, HarborChatClient)
    assert isinstance(async_client, AsyncHarborChatClient)


def test_factory_preserves_dependency_validation(base_config) -> None:
    dependencies = ChatClientDependencies(
        backend=FakeInvocation(),
        invocation=FakeInvocation(),
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        ChatClientFactory.create(base_config, dependencies)
