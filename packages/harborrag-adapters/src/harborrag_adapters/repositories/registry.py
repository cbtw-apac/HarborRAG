from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from harborrag_adapters.repositories.errors import (
    HarborStorageConfigurationError,
    StorageErrorContext,
)
from harborrag_adapters.repositories.plugin import (
    RepositoryConfig,
    RepositoryDependencies,
    RepositoryPlugin,
)
from harborrag_core.storage import StorageFamily


class ProviderMap[ProductT]:
    """Stores and resolves provider plugins for one storage family."""

    def __init__(self, family: StorageFamily) -> None:
        self._family = family
        self._plugins: dict[str, RepositoryPlugin[Any, ProductT]] = {}

    def register(self, plugin: RepositoryPlugin[Any, ProductT]) -> None:
        """Register one provider after validating its family and name."""
        if plugin.family != self._family:
            raise ValueError(
                f"plugin {plugin.name!r} belongs to {plugin.family}, not {self._family}"
            )
        if plugin.name in self._plugins:
            raise ValueError(f"backend {plugin.name!r} already registered for {self._family}")
        self._plugins[plugin.name] = plugin

    def names(self) -> tuple[str, ...]:
        """Return registered provider names in deterministic order."""
        return tuple(sorted(self._plugins))

    def capabilities(self, backend: str) -> BaseModel | None:
        """Return capabilities published by one provider."""
        return self._require(backend).capabilities

    def create(
        self,
        *,
        backend: str,
        instance_name: str,
        options: Mapping[str, object] | None,
        dependencies: RepositoryDependencies | None,
    ) -> ProductT:
        """Validate provider-owned options and create one unconnected product."""
        plugin = self._require(backend)
        payload = {
            "backend": backend,
            "instance_name": instance_name,
            **dict(options or {}),
        }
        config: RepositoryConfig = plugin.config_type.model_validate(payload)
        try:
            return plugin.create(config, dependencies or RepositoryDependencies())
        except ImportError as exc:
            # Providers perform explicit optional-dependency checks and use this
            # exact message. Re-raise every other ImportError so constructor bugs
            # retain their original traceback instead of masquerading as extras.
            if re.fullmatch(r"[A-Za-z0-9_.-]+ is not installed", str(exc).strip()) is None:
                raise
            extra = plugin.optional_dependency or plugin.name
            raise HarborStorageConfigurationError(
                f"backend {plugin.name!r} requires optional dependency extra {extra!r}",
                context=StorageErrorContext(
                    family=self._family,
                    backend=plugin.name,
                    instance_name=instance_name,
                    operation="create",
                ),
                original=exc,
            ) from exc

    def create_from_config(
        self,
        config: RepositoryConfig,
        dependencies: RepositoryDependencies | None,
    ) -> ProductT:
        """Create a product from an already validated provider configuration."""
        plugin = self._require(config.backend)
        if not isinstance(config, plugin.config_type):
            raise TypeError(
                f"backend {config.backend!r} requires {plugin.config_type.__name__}, "
                f"received {type(config).__name__}"
            )
        return plugin.create(config, dependencies or RepositoryDependencies())

    def _require(self, backend: str) -> RepositoryPlugin[Any, ProductT]:
        try:
            return self._plugins[backend]
        except KeyError as exc:
            raise HarborStorageConfigurationError(
                f"unsupported {self._family} backend {backend!r}; available: {self.names()}",
                context=StorageErrorContext(
                    family=self._family,
                    backend=backend,
                    instance_name="unknown",
                    operation="provider_lookup",
                ),
            ) from exc
