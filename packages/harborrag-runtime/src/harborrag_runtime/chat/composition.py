"""Provider-client composition for runtime chat services."""

from __future__ import annotations

from collections.abc import Collection

from harborrag_adapters.models.chat import (
    AsyncHarborChatClient,
    ChatClientDependencies,
    ChatClientFactory,
    HarborChatClient,
    HarborChatClientConfig,
)
from harborrag_adapters.models.runtime import ResourceOwnership, TelemetryDispatcher
from harborrag_runtime.config.errors import ParserConfigurationError
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

    telemetry = build_model_telemetry(
        config,
        langfuse_enabled=settings.langfuse_enabled,
    )
    try:
        return ChatClientFactory.create_async(
            config,
            ChatClientDependencies(
                telemetry=telemetry,
                telemetry_ownership=ResourceOwnership.OWNED,
            ),
        )
    except BaseException:
        telemetry.close()
        raise


def build_parser_chat_client(
    settings: RuntimeSettings,
    models: Collection[str],
) -> HarborChatClient:
    """Build the synchronous client PDF engines send pages through.

    Parsing runs in worker threads, so this client is the sync one. It gets
    the same telemetry as the chat client and owns it, so ``close`` disposes it.
    Checking the models here fails startup instead of every PDF in a run.

    Args:
        settings: Runtime settings naming models.yaml.
        models: Chat models the parser configuration reads pages with.

    Returns:
        A client for those models; the caller closes it.

    Raises:
        ParserConfigurationError: A model is missing from models.yaml, or one of
            its deployments does not declare ``multimodal: true``.
    """
    config = HarborChatClientConfig.from_file(settings.model_config_path)
    unreadable = sorted(model for model in models if not _reads_images(config, model))
    if unreadable:
        raise ParserConfigurationError(
            "PDF engine models must be chat models in models.yaml whose deployments "
            f"all declare multimodal: true; not usable: {', '.join(unreadable)}"
        )
    telemetry = build_model_telemetry(config, langfuse_enabled=settings.langfuse_enabled)
    return _owning_sync_client(config, telemetry)


def _reads_images(config: HarborChatClientConfig, model: str) -> bool:
    try:
        _, model_config = config.model_for(model)
    except KeyError:
        return False
    return all(deployment.capabilities.multimodal for deployment in model_config.deployments)


def _owning_sync_client(
    config: HarborChatClientConfig,
    telemetry: TelemetryDispatcher,
) -> HarborChatClient:
    """Create the sync client that owns ``telemetry``, closing it if creation fails."""
    try:
        return ChatClientFactory.create(
            config,
            ChatClientDependencies(
                telemetry=telemetry,
                telemetry_ownership=ResourceOwnership.OWNED,
            ),
        )
    except BaseException:
        telemetry.close()
        raise
