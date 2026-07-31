"""Connector/parser catalog loading and connector construction from config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from harborrag_runtime.config import (
    ConnectorConfigurationError,
    load_connector_catalog,
    load_parser_catalog,
)
from harborrag_runtime.config.connectors.providers import config_factory

from .paths import CONFIG_DIR

if TYPE_CHECKING:
    from harborrag_adapters.connectors import HarborConnector
    from harborrag_adapters.parsers import HarborParserRegistry

_ATTACHMENT_PROVIDERS = frozenset({"confluence", "jira"})


def _catalog_source(filename: str):
    """Prefer a real `config/<filename>.yaml`; fall back to its example."""
    real = CONFIG_DIR / f"{filename}.yaml"
    if real.exists():
        return real
    example = CONFIG_DIR / f"{filename}.example.yaml"
    print(f"[config] {real.name} not found; falling back to {example.name}")
    return example


def connector_catalog():
    return load_connector_catalog(_catalog_source("connectors"))


def parser_catalog():
    return load_parser_catalog(_catalog_source("parsers"))


def _connector_environment(provider: str) -> dict[str, str]:
    """Copy `os.environ` with smoke-only aliases resolved for one provider."""
    values = dict(os.environ)
    if provider == "jira" and not values.get("JIRA_TOKEN") and values.get("JIRA_API_TOKEN"):
        values["JIRA_TOKEN"] = values["JIRA_API_TOKEN"]
    return values


def build_connector(
    name: str,
    *,
    include_attachments: bool,
    parser: HarborParserRegistry | None = None,
) -> HarborConnector:
    """Build one configured connector from `config/connectors.yaml`.

    Raises:
        ConnectorConfigurationError: If the connector is undefined or a
            referenced environment variable is missing/empty.
    """
    from harborrag_adapters.connectors import HarborConnector

    from .ocr_parser import attachment_custom_parsers

    definition = connector_catalog().get(name)
    if not definition.enabled:
        raise ConnectorConfigurationError(
            f"Connector {name!r} is disabled (enabled: false) and cannot be built"
        )
    overrides: dict[str, Any] = {}
    if definition.provider in _ATTACHMENT_PROVIDERS:
        overrides["include_attachments"] = include_attachments
        if include_attachments:
            overrides["custom_parsers"] = attachment_custom_parsers()

    values = definition.resolve_settings(
        environment=_connector_environment(definition.provider),
        overrides=overrides,
    )
    factory = config_factory(definition.provider)
    try:
        provider_config = factory(**values)
    except (TypeError, ValueError) as exc:
        raise ConnectorConfigurationError(
            f"Connector {name!r} ({definition.provider}) is invalid: {exc}"
        ) from exc

    extra = {"parser": parser} if definition.provider in _ATTACHMENT_PROVIDERS else {}
    return HarborConnector(definition.provider, config=provider_config, **extra)
