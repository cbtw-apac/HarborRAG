"""Registry of connector providers.

Handles:
- Provider definitions with aliases, config factories and dependencies
- Registration, lookup and removal by name or alias
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .base import BaseConnector
from .document_transform import ConnectorDocumentTransformFactory
from .exceptions import ConnectorNotFoundError

logger = logging.getLogger("harborrag.adapters.connectors.registry")

_PROVIDER_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class ConnectorProviderDefinition:
    """Connector plugin definition: provider class plus composition metadata.

    Attributes:
        name: Canonical provider name.
        provider_cls: Connector class to instantiate.
        aliases: Alternative names that resolve to this provider.
        config_factory: Builds the provider's config object.
        constructor_dependencies: Constructor argument name to runtime dependency name.
        config_path_fields: Config fields that hold filesystem paths.
        document_kind: Kind label for the provider's documents.
        document_transform_factory: Builds the provider's document transform.
    """

    name: str
    provider_cls: type[BaseConnector]
    aliases: tuple[str, ...] = ()
    config_factory: Callable[..., object] | None = None
    constructor_dependencies: Mapping[str, str] = field(default_factory=dict)
    config_path_fields: tuple[str, ...] = ()
    document_kind: str = "document"
    document_transform_factory: ConnectorDocumentTransformFactory | None = None

    def __post_init__(self) -> None:
        """Validate the definition and freeze ``constructor_dependencies`` as read-only.

        Names, aliases and ``document_kind`` must be lowercase identifiers.
        """
        dependencies = dict(self.constructor_dependencies)
        if _PROVIDER_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("connector provider name must be a normalized identifier")
        if any(_PROVIDER_NAME_PATTERN.fullmatch(alias) is None for alias in self.aliases):
            raise ValueError("connector provider aliases must be normalized identifiers")
        if any(not key.strip() or not value.strip() for key, value in dependencies.items()):
            raise ValueError("connector constructor dependency names must be non-empty")
        if any(not field.strip() for field in self.config_path_fields):
            raise ValueError("connector config path fields must be non-empty")
        if _PROVIDER_NAME_PATTERN.fullmatch(self.document_kind) is None:
            raise ValueError("connector document_kind must be a normalized identifier")
        if self.document_transform_factory is not None and not callable(
            self.document_transform_factory
        ):
            raise TypeError("connector document_transform_factory must be callable")
        object.__setattr__(self, "constructor_dependencies", MappingProxyType(dependencies))


class ConnectorRegistry:
    """In-process provider registry behind ``HarborConnector``."""

    def __init__(self) -> None:
        """Create an empty registry."""
        self._providers: dict[str, ConnectorProviderDefinition] = {}

    def register(
        self,
        name: str,
        provider_cls: type[BaseConnector],
        *,
        aliases: list[str] | None = None,
        replace: bool = False,
    ) -> None:
        """Register a connector class under a name and aliases.

        Args:
            name: Canonical provider name.
            provider_cls: Connector class to instantiate.
            aliases: Alternative names for the provider.
            replace: Allow overriding keys owned by a different provider.

        Raises:
            ValueError: If a key belongs to a different provider and ``replace`` is false.
        """
        definition = ConnectorProviderDefinition(
            name=name,
            provider_cls=provider_cls,
            aliases=tuple(aliases or ()),
        )
        self.register_provider(definition, replace=replace)

    def register_provider(
        self,
        definition: ConnectorProviderDefinition,
        *,
        replace: bool = False,
    ) -> None:
        """Register a provider definition under its name and aliases.

        Args:
            definition: Provider definition to register.
            replace: Allow overriding keys owned by a different definition.

        Raises:
            ValueError: If a key belongs to a different definition and ``replace`` is false.
        """
        keys = (definition.name, *definition.aliases)
        for key in keys:
            existing = self._providers.get(key)
            if existing is not None and existing != definition and not replace:
                raise ValueError(
                    f"Connector key {key!r} already registered to "
                    f"{existing.provider_cls.__name__}; pass replace=True to override."
                )
        for key in keys:
            self._providers[key] = definition

    def get_class(self, name: str) -> type[BaseConnector]:
        """Return the connector class for a name or alias.

        Raises:
            ConnectorNotFoundError: If the name is not registered.
        """
        try:
            return self._providers[name].provider_cls
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def get_definition(self, name: str) -> ConnectorProviderDefinition:
        """Return the provider definition for a name or alias.

        Raises:
            ConnectorNotFoundError: If the name is not registered.
        """
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def canonical_name(self, name: str) -> str:
        """Return the canonical provider name for a name or alias."""
        return self.get_definition(name).name

    def canonical_names(self) -> list[str]:
        """Return sorted canonical provider names, without aliases."""
        return sorted({definition.name for definition in self._providers.values()})

    def unregister(self, name: str) -> None:
        """Remove one name or alias. Raises ``ConnectorNotFoundError`` if not registered."""
        try:
            del self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def unregister_provider(self, name: str) -> None:
        """Remove a provider and all its aliases. Raises ``ConnectorNotFoundError`` if unknown."""
        definition = self.get_definition(name)
        for key in tuple(self._providers):
            if self._providers[key] is definition:
                del self._providers[key]

    def create(self, name: str, **kwargs: Any) -> BaseConnector:
        """Instantiate a connector by name or alias, passing ``kwargs`` to its constructor."""
        return self.get_class(name)(**kwargs)

    def names(self) -> list[str]:
        """Return all registered names and aliases, sorted."""
        return sorted(self._providers)


connector_registry = ConnectorRegistry()
