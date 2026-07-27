from __future__ import annotations

from .async_client import AsyncHarborChatClient
from .client import HarborChatClient
from .configs import HarborChatClientConfig
from .schemas import ChatClientDependencies


class ChatClientFactory:
    """Create a ready sync or async client from validated configuration."""

    @staticmethod
    def create(
        config: HarborChatClientConfig,
        dependencies: ChatClientDependencies | None = None,
    ) -> HarborChatClient:
        return HarborChatClient(config, dependencies)

    @staticmethod
    def create_async(
        config: HarborChatClientConfig,
        dependencies: ChatClientDependencies | None = None,
    ) -> AsyncHarborChatClient:
        return AsyncHarborChatClient(config, dependencies)
