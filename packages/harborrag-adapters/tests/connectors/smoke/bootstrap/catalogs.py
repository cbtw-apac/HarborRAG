"""Connector/parser catalog loading and connector construction from config."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from harborrag_runtime.config import (
    ConnectorConfigurationError,
    load_connector_catalog,
    load_parser_catalog,
)

from .paths import CONFIG_DIR

if TYPE_CHECKING:
    from harborrag_adapters.connectors import HarborConnector
    from harborrag_adapters.parsers import HarborParserRegistry
    from harborrag_runtime.config import ConnectorDefinition

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


def connector_definition(
    identifier: str,
    *,
    expected_provider: str | None = None,
) -> ConnectorDefinition:
    """Resolve a connection ID, with unique provider names as shorthand.

    Connector catalog keys are application-level ``connection_id`` values
    such as ``harborrag-workspace`` or ``jira-main``. Direct provider names
    remain convenient for standalone entry points, but only when exactly one
    enabled connection uses that provider.
    """

    catalog = connector_catalog()
    definition = catalog.connectors.get(identifier)
    if definition is None:
        provider_matches = [
            item for item in catalog.connectors.values() if item.provider == identifier
        ]
        enabled_matches = [item for item in provider_matches if item.enabled]
        candidates = enabled_matches or provider_matches
        if len(candidates) != 1:
            available = ", ".join(catalog.names(enabled_only=True)) or "none"
            if candidates:
                matching = ", ".join(sorted(item.name for item in candidates))
                raise ConnectorConfigurationError(
                    f"Provider {identifier!r} has multiple configured connections: "
                    f"{matching}. Pass a connection ID explicitly"
                )
            raise ConnectorConfigurationError(
                f"Unknown connection ID or provider: {identifier!r}. "
                f"Available enabled connection IDs: {available}"
            )
        definition = candidates[0]

    if expected_provider is not None and definition.provider != expected_provider:
        raise ConnectorConfigurationError(
            f"Connection {definition.name!r} uses provider {definition.provider!r}; "
            f"expected {expected_provider!r}"
        )
    return definition


def build_connector(
    identifier: str,
    *,
    include_attachments: bool,
    parser: HarborParserRegistry | None = None,
    expected_provider: str | None = None,
) -> HarborConnector:
    """Build one configured connection from `config/connectors.yaml`.

    Raises:
        ConnectorConfigurationError: If the connection is undefined,
            ambiguous, disabled, for the wrong provider, or references a
            missing/empty environment variable.
    """
    from .ocr_parser import attachment_custom_parsers

    definition = connector_definition(
        identifier,
        expected_provider=expected_provider,
    )
    overrides: dict[str, object] = {}
    if definition.provider in _ATTACHMENT_PROVIDERS:
        overrides["include_attachments"] = include_attachments
        if include_attachments:
            overrides["custom_parsers"] = attachment_custom_parsers()

    connector_kwargs = {"parser": parser} if definition.provider in _ATTACHMENT_PROVIDERS else None
    return definition.build(
        environment=_connector_environment(definition.provider),
        overrides=overrides,
        connector_kwargs=connector_kwargs,
    )
