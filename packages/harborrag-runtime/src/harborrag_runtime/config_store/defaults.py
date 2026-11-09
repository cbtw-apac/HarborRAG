"""Registry defaults that depend on the boot configuration."""

from __future__ import annotations

import logging

from harborrag_core.config_store import SettingsRegistry
from harborrag_runtime.config.settings import RuntimeSettings

logger = logging.getLogger("harborrag.runtime.config_store")

EMBEDDING_KEY = "models.embedding"


def embedding_default(settings: RuntimeSettings) -> str | None:
    """Return ``provider:model`` of the embedding model in use, or ``None`` when unreadable."""
    from harborrag_adapters.models.embed import HarborEmbedClientConfig

    try:
        config = HarborEmbedClientConfig.from_file(settings.model_config_path)
        _, model = config.model_for(settings.embedding_model or config.default_model)
        deployment = model.deployments[0]
    except (OSError, ValueError, KeyError, IndexError) as error:
        logger.warning("settings_embedding_default_unreadable error_type=%s", type(error).__name__)
        return None
    return f"{deployment.provider}:{deployment.model}"


def registry_for(settings: RuntimeSettings) -> SettingsRegistry:
    """Return the shipped registry with the embedding default derived from ``settings``."""
    return SettingsRegistry.default().with_defaults({EMBEDDING_KEY: embedding_default(settings)})
