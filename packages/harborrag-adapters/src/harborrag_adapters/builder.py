from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from harborrag_adapters.registry import AdapterRegistry


@dataclass(slots=True)
class AdapterBuilder:
    """Builder for adapters, including connectors, parsers, models, and repositories."""

    registry: AdapterRegistry

    def build_connector(self, provider: str, **kwargs: Any) -> Any:
        # Prefer an explicitly-registered class, then fall back to the shared
        # connector provider registry that the built-in providers register into
        # (previously the builder saw an empty registry and raised for every
        # real provider such as "confluence"/"github").
        try:
            connector_cls = self.registry.get_connector(provider)
        except ValueError:
            from harborrag_adapters.connectors.registry import connector_registry

            return connector_registry.create(provider, **kwargs)
        return connector_cls(**kwargs)

    def build_parser(self, provider: str, **kwargs: Any) -> Any:
        return self.registry.get_parser(provider)(**kwargs)

    def build_model(self, provider: str, **kwargs: Any) -> Any:
        return self.registry.get_model(provider)(**kwargs)

    def build_repository(self, provider: str, **kwargs: Any) -> Any:
        return self.registry.get_repository(provider)(**kwargs)
