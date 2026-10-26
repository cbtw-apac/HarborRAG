"""Summarise the model catalog without exposing adapter types to the CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelCatalogSummary:
    chat_default: str
    chat_deployments: tuple[str, ...]
    embed_default: str
    embed_deployments: tuple[str, ...]


def describe_model_catalog(path: str | Path) -> ModelCatalogSummary:
    """Load the chat and embed sections eagerly; every ``${VAR}`` must resolve."""

    from harborrag_adapters.models.chat.configs import HarborChatClientConfig
    from harborrag_adapters.models.embed.configs import HarborEmbedClientConfig

    chat = HarborChatClientConfig.from_file(path)
    embed = HarborEmbedClientConfig.from_file(path)
    return ModelCatalogSummary(
        chat_default=chat.default_model,
        chat_deployments=_deployment_names(chat.models),
        embed_default=embed.default_model,
        embed_deployments=_deployment_names(embed.models),
    )


def _deployment_names(models: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(deployment.name)
        for definition in models.values()
        for deployment in definition.deployments
    )


__all__ = ["ModelCatalogSummary", "describe_model_catalog"]
