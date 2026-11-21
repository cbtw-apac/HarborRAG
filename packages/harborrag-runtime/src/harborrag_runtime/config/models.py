"""Summarise the model catalog without exposing adapter types to the CLI."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_VARIABLE_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


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
        ValueError: A named rerank model's provider requires an API key, and its
            ``api_key`` reads a variable that is empty, so the rerank client would
            refuse it. A provider whose registry entry needs no key, such as
            ``vllm``, ``infinity`` or ``litellm_proxy``, may leave it empty, and the
            rerank client accepts that.
    """
    from harborrag_adapters.models.runtime import expand_environment, load_config_document

    document = load_config_document(path)
    if "rerank" not in document:
        return False
    deployments = _rerank_deployments(document["rerank"])
    if deployments is None:
        return True
    named = [item for item in deployments if str(expand_environment(item["model"])).strip()]
    for deployment in named:
        _require_rerank_key(deployment)
    return bool(named)


def _rerank_deployments(section: object) -> list[Mapping[str, Any]] | None:
    """Return the rerank deployments that name a ``model``, or None for another shape."""
    definitions = section.get("models") if isinstance(section, Mapping) else None
    if not isinstance(definitions, Mapping) or not definitions:
        return None
    found: list[Mapping[str, Any]] = []
    for definition in definitions.values():
        deployments = definition.get("deployments") if isinstance(definition, Mapping) else None
        if not isinstance(deployments, list) or not deployments:
            return None
        for deployment in deployments:
            if not isinstance(deployment, Mapping) or "model" not in deployment:
                return None
            found.append(deployment)
    return found


def _require_rerank_key(deployment: Mapping[str, Any]) -> None:
    """Refuse an empty key variable when the deployment's provider requires a key.

    An unknown provider is left to the loader, which names the supported ones.
    """
    from harborrag_adapters.models.runtime import expand_environment

    key = deployment.get("api_key")
    variables = _VARIABLE_REFERENCE.findall(key) if isinstance(key, str) else []
    if not variables or str(expand_environment(key)).strip():
        return
    if _rerank_provider_requires_key(deployment.get("provider")):
        raise ValueError(f"{', '.join(variables)} is empty, and the rerank model needs it")


def _rerank_provider_requires_key(provider: object) -> bool:
    """Whether the rerank provider registry marks ``provider`` as needing an API key."""
    from harborrag_adapters.models.rerank.registry import (
        HarborRerankProvider,
        RerankProviderRegistry,
    )
    from harborrag_adapters.models.runtime import expand_environment

    name = str(expand_environment(provider)).strip() if isinstance(provider, str) else ""
    try:
        known = HarborRerankProvider(name)
    except ValueError:
        return False
    return RerankProviderRegistry.default().get(known).requires_api_key


def _deployment_names(models: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(deployment.name)
        for definition in models.values()
        for deployment in definition.deployments
    )
