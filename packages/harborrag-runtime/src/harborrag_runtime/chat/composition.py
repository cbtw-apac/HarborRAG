"""Provider-client composition for runtime chat services."""

from __future__ import annotations

import logging

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientFactory,
    HarborChatClientConfig,
)
from harborrag_adapters.models.embed import HarborEmbedClientConfig
from harborrag_core.contracts.errors import HarborConfigurationError
from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.chat.composition")


def build_chat_client(settings: RuntimeSettings) -> AsyncHarborChatClient:
    """Build the async client from the shared model catalog."""

    config = HarborChatClientConfig.from_file(settings.model_config_path)
    return ChatClientFactory.create_async(config)


def validate_serving_model_config(settings: RuntimeSettings) -> None:
    """Load the serving model catalogue so a gap fails the boot, not a request.

    Both clients a chat completion needs are built lazily on first use -- the
    chat client in ``RuntimeChatService._configured_client`` and the embedding
    client in ``connect_retrieval_service``. An unset ``${HARBOR_*}`` reference
    in the catalogue therefore surfaced as a per-request 503 whose real cause
    was logged only at DEBUG. Parsing both sections here turns that into a
    startup failure. This is parsing only: no provider, vector store, or graph
    connection is opened, and no secret is logged.
    """

    for section, config in (
        ("chat", HarborChatClientConfig),
        ("embed", HarborEmbedClientConfig),
    ):
        try:
            config.from_file(settings.model_config_path)
        except Exception as exc:
            logger.error(
                "Serving model configuration is incomplete section=%s path=%s error_type=%s",
                section,
                settings.model_config_path,
                type(exc).__name__,
            )
            raise HarborConfigurationError(
                f"{section} model configuration is incomplete; inspect the startup logs"
            ) from exc
