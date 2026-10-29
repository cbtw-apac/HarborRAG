"""The registry of source types HarborRAG can connect to.

Handles:
- resolving canonical names, provider names and aliases to one ``SourceTypeSpec``
- the provider config dataclass behind each source type
- the module-level ``validate`` entry point
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from inspect import signature
from typing import Any

from harborrag_adapters.connectors import connector_registry
from harborrag_adapters.connectors.exceptions import ConnectorNotFoundError

from ..providers import ConnectorConfigFactory
from .atlassian import CONFLUENCE, JIRA
from .github import GITHUB
from .local_file import LOCAL_FILE
from .sharepoint import SHAREPOINT
from .spec import (
    Category,
    ConnectionValidationError,
    FieldIssue,
    SourceTypeSpec,
    ValidatedConnection,
)


class SourceTypeRegistry:
    """Look up source types by canonical name, provider name or alias."""

    def __init__(self, specs: Iterable[SourceTypeSpec]) -> None:
        self._specs = {spec.source_type: spec for spec in specs}
        self._names = {
            name: spec.source_type
            for spec in self._specs.values()
            for name in (spec.source_type, spec.provider, *spec.aliases)
        }

    def canonical(self, name: str) -> str | None:
        """Return the canonical source type ``name`` refers to, or ``None``."""
        return self._names.get(name.strip().lower())

    def get(self, name: str) -> SourceTypeSpec:
        """Return the spec ``name`` refers to.

        Raises:
            ConnectionValidationError: ``name`` is not a known source type.
        """
        canonical = self.canonical(name)
        if canonical is None:
            raise ConnectionValidationError([FieldIssue("source_type", "unknown source type")])
        return self._specs[canonical]

    def all(self, category: Category | None = None) -> list[SourceTypeSpec]:
        """Every registered spec, optionally of one category, in registration order."""
        return [
            spec for spec in self._specs.values() if category is None or spec.category == category
        ]

    def provider_factory(self, name: str) -> ConnectorConfigFactory:
        """Return the provider config dataclass behind a source type or provider name.

        Raises:
            ConnectionValidationError: ``name`` is unknown or its provider is not installed.
        """
        spec = self.get(name)
        try:
            factory = connector_registry.get_definition(spec.provider).config_factory
        except ConnectorNotFoundError:
            factory = None
        if factory is None:
            raise ConnectionValidationError([FieldIssue("source_type", "provider not installed")])
        return factory

    def dataclass_fields(self, name: str) -> frozenset[str]:
        """Keyword arguments the provider config dataclass accepts."""
        return frozenset(signature(self.provider_factory(name)).parameters)

    def secret_dataclass_fields(self, name: str) -> frozenset[str]:
        """Dataclass keywords that receive a secret value."""
        spec = self.get(name)
        return frozenset(spec.credential_renames.get(f, f) for f in spec.secret_fields)


source_types = SourceTypeRegistry((JIRA, CONFLUENCE, SHAREPOINT, LOCAL_FILE, GITHUB))


def validate(
    source_type: str,
    credentials: Mapping[str, Any],
    config: Mapping[str, Any],
    tuning: Mapping[str, Any] | None = None,
) -> ValidatedConnection:
    """Validate a connection against its source type.

    Args:
        source_type: Canonical name, provider name or alias.
        credentials: Effective credentials, secrets as values or references.
        config: What to sync.
        tuning: Per-connection overrides.

    Returns:
        The normalized values.

    Raises:
        ConnectionValidationError: The type is unknown or any field fails;
            ``details["errors"]`` lists every ``{field, issue}``.
    """
    return source_types.get(source_type).validate(credentials, config, tuning)
