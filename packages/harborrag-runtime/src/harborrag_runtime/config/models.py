"""Summarise the model catalog without exposing adapter types to the CLI."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelCatalogSummary:
    """Default logical models and deployment names of the chat and embed sections.

    Attributes:
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
    """Return whether the catalog at ``path`` names a rerank model.

    A ``rerank`` section whose deployments all expand to an empty ``model`` is an
    optional reranker left off, such as ``model: ${HARBORRAG_RERANKER_MODEL:-}``
    with the variable unset. Any other ``rerank`` section counts, whatever it
    holds, so the loader reports what is wrong with it.

    The loader's errors propagate: ``OSError`` for an unreadable file,
    ``yaml.YAMLError`` for malformed YAML, ``ValueError`` for a document that is
    not a mapping, a duplicate key, an unknown top-level section or a ``model``
    that reads an unset variable with no default.

    Args:
        path: YAML or JSON model catalog.

    Returns:
        Whether a ``rerank`` section is present and names at least one model.

    Raises:
        ValueError: A named rerank model's ``api_key`` reads a variable that is
            empty, such as ``HARBORRAG_RERANKER_API_KEY``. Every provider refuses
            it, because LiteLLM would send an ambient key such as ``COHERE_API_KEY``
            instead. The message names the variable, never a value.
    """
    from harborrag_adapters.models.runtime import catalog_has_rerank_model, load_config_document

    return catalog_has_rerank_model(load_config_document(path))


def require_set_model_variables(values: object) -> None:
    """Refuse a ``${VAR}`` reference in parsed catalog values whose variable is blank.

    ``python -m harborrag_adapters.models validate`` applies the same rule to one
    family's section. A ``${VAR:-default}`` reference is skipped.

    Args:
        values: The catalog, or part of it, as parsed from YAML or JSON.

    Raises:
        ValueError: ``NAME is blank``, naming every blank variable in sorted order.
    """
    from harborrag_adapters.models.runtime import require_set_variables

    require_set_variables(values)


def _deployment_names(models: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(deployment.name)
        for definition in models.values()
        for deployment in definition.deployments
    )
