"""Provider-client composition for runtime chat services."""

from __future__ import annotations

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_adapters.models.runtime.lifecycle import ResourceOwnership
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.observability import build_model_telemetry


def build_chat_client(settings: RuntimeSettings) -> AsyncHarborChatClient:
    """Build the async client from the shared model catalog."""

    config = HarborChatClientConfig.from_file(settings.model_config_path)
    telemetry = build_model_telemetry(config, langfuse_enabled=settings.langfuse_enabled)
    try:
        return ChatClientFactory.create_async(
            config,
            ChatClientDependencies(
                telemetry=telemetry, telemetry_ownership=ResourceOwnership.OWNED
            ),
        )
    except BaseException:
        telemetry.close()
        raise
