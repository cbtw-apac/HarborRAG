"""Registry defaults that depend on the boot configuration."""

from __future__ import annotations

import logging
from dataclasses import replace

import yaml

from harborrag_core.config_store import SettingsRegistry
from harborrag_runtime.config.models import has_rerank_model
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


def rerank_available(settings: RuntimeSettings) -> bool:
    """Return whether the model catalog has a rerank model, or ``False`` when unreadable."""
    try:
        return has_rerank_model(settings.model_config_path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        logger.warning("settings_rerank_model_unreadable error_type=%s", type(error).__name__)
        return False


def registry_for(settings: RuntimeSettings) -> SettingsRegistry:
    """Return the shipped registry with the embedding default and rerank availability."""
    registry = SettingsRegistry.default().with_defaults(
        {EMBEDDING_KEY: embedding_default(settings)}
    )
    return replace(registry, rerank_available=rerank_available(settings))
