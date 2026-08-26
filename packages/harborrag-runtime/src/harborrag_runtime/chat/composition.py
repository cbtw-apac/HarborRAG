"""Provider-client composition for runtime chat services."""

from __future__ import annotations

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_runtime.config.settings import RuntimeSettings


def build_chat_client(settings: RuntimeSettings) -> AsyncHarborChatClient:
    """Build the async client from the shared model catalog."""

    config = HarborChatClientConfig.from_file(settings.model_config_path)
    return ChatClientFactory.create_async(config)
