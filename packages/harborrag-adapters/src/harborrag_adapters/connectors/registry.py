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
    """One connector plugin and the metadata needed to compose it."""

    name: str
    provider_cls: type[BaseConnector]
    aliases: tuple[str, ...] = ()
    config_factory: Callable[..., object] | None = None
    constructor_dependencies: Mapping[str, str] = field(default_factory=dict)
    config_path_fields: tuple[str, ...] = ()
    document_kind: str = "document"
    document_transform_factory: ConnectorDocumentTransformFactory | None = None

    def __post_init__(self) -> None:
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
    """In-process provider registry used by ``HarborConnector``."""

    def __init__(self) -> None:
        """Create an empty provider mapping."""
        self._providers: dict[str, ConnectorProviderDefinition] = {}

    def register(
        self,
        name: str,
        provider_cls: type[BaseConnector],
        *,
        aliases: list[str] | None = None,
        replace: bool = False,
    ) -> None:
        """Register one connector class under a canonical name and aliases.

        A key already owned by a *different* provider is a misconfiguration
        (e.g. two providers sharing an alias) and is rejected unless
        ``replace=True`` so silent shadowing cannot occur.
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
        """Register a complete connector plugin definition."""

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
        """Return a registered connector class by name (canonical or alias)."""
        try:
            return self._providers[name].provider_cls
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def get_definition(self, name: str) -> ConnectorProviderDefinition:
        """Return plugin metadata by canonical name or alias."""

        try:
            return self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def canonical_name(self, name: str) -> str:
        """Resolve a registered alias to its canonical provider name."""

        return self.get_definition(name).name

    def canonical_names(self) -> list[str]:
        """Return canonical provider names without aliases."""

        return sorted({definition.name for definition in self._providers.values()})

    def unregister(self, name: str) -> None:
        """Remove one canonical connector name or alias from the registry."""
        try:
            del self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(f"Unsupported connector provider: {name}") from exc

    def unregister_provider(self, name: str) -> None:
        """Remove a provider and every alias owned by its definition."""

        definition = self.get_definition(name)
        for key in tuple(self._providers):
            if self._providers[key] is definition:
                del self._providers[key]

    def create(self, name: str, **kwargs: Any) -> BaseConnector:
        """Instantiate a registered connector by name."""
        return self.get_class(name)(**kwargs)

    def names(self) -> list[str]:
        """Return all registered canonical names and aliases."""
        return sorted(self._providers)


connector_registry = ConnectorRegistry()
