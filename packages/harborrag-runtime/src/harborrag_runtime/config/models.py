"""Summarise the model catalog without exposing adapter types to the CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelCatalogSummary:
    """Default logical models and deployment names of the chat and embed sections.

    Args:
        chat_default: Default logical chat model.
        chat_deployments: Every chat deployment name, in catalog order.
        embed_default: Default logical embedding model.
        embed_deployments: Every embedding deployment name, in catalog order.
    """

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


def has_rerank_model(path: str | Path) -> bool:
    """Return whether the catalog at ``path`` has a ``rerank`` section.

    The loader's errors propagate: ``OSError`` for an unreadable file,
    ``yaml.YAMLError`` for malformed YAML, ``ValueError`` for a document that is
    not a mapping.

    Args:
        path: YAML or JSON model catalog.

    Returns:
        Whether a ``rerank`` section is present, whatever it holds.
    """
    from harborrag_adapters.models.runtime import load_config_document

    return "rerank" in load_config_document(path)


def _deployment_names(models: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(deployment.name)
        for definition in models.values()
        for deployment in definition.deployments
    )
