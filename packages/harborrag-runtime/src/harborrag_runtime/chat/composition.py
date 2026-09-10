"""Provider-client composition for runtime chat services."""

from __future__ import annotations

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_adapters.models.runtime import ResourceOwnership
from harborrag_runtime.config.settings import RuntimeSettings
from harborrag_runtime.ingestion.observability import build_model_telemetry


def build_chat_client(settings: RuntimeSettings) -> AsyncHarborChatClient:
    """Build the async client from the shared model catalog, with telemetry.

    Chat used to be the one model family whose calls no sink ever saw. It now
    gets the same dispatcher the embed client does, declared ``OWNED`` so the
    client disposes it on ``aclose`` -- nothing else holds a reference to it.
    """

    config = HarborChatClientConfig.from_file(settings.model_config_path)
    return build_chat_client_for(config, settings)


def build_chat_client_for(
    config: HarborChatClientConfig,
    settings: RuntimeSettings,
) -> AsyncHarborChatClient:
    """Wrap an already-loaded configuration in the same telemetry and ownership.

    A tenant's own client is composed through here so it is observable exactly
    like the process-wide one, and so its dispatcher is disposed with it when
    the cache evicts or replaces it.
    """

    return ChatClientFactory.create_async(
        config,
        ChatClientDependencies(
            telemetry=build_model_telemetry(
                config,
                langfuse_enabled=settings.langfuse_enabled,
            ),
            telemetry_ownership=ResourceOwnership.OWNED,
        ),
    )
