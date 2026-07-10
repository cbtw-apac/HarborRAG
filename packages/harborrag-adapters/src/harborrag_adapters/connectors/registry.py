from __future__ import annotations

import logging
from typing import Any

from .base import BaseConnector
from .exceptions import ConnectorNotFoundError

logger = logging.getLogger("harborrag.adapters.connectors.registry")


class ConnectorRegistry:
    """In-process provider registry used by ``HarborConnector``."""

    def __init__(self) -> None:
        """Create an empty provider mapping."""
        self._providers: dict[str, type[BaseConnector]] = {}

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
        for key in (name, *(aliases or [])):
            existing = self._providers.get(key)
            if existing is not None and existing is not provider_cls and not replace:
                raise ValueError(
                    f"Connector key {key!r} already registered to "
                    f"{existing.__name__}; pass replace=True to override."
                )
            self._providers[key] = provider_cls

    def get_class(self, name: str) -> type[BaseConnector]:
        """Return a registered connector class by name (canonical or alias)."""
        try:
            return self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(
                f"Unsupported connector provider: {name}"
            ) from exc

    def unregister(self, name: str) -> None:
        """Remove one canonical connector name or alias from the registry."""
        try:
            del self._providers[name]
        except KeyError as exc:
            raise ConnectorNotFoundError(
                f"Unsupported connector provider: {name}"
            ) from exc

    def create(self, name: str, **kwargs: Any) -> BaseConnector:
        """Instantiate a registered connector by name."""
        return self.get_class(name)(**kwargs)

    def names(self) -> list[str]:
        """Return all registered canonical names and aliases."""
        return sorted(self._providers)


connector_registry = ConnectorRegistry()
