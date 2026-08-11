"""Connector configuration data shapes and construction recipes."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from harborrag_adapters.connectors import HarborConnector, connector_registry
from harborrag_runtime.config.connectors.providers import (
    coerce_config_values,
    config_factory,
)
from harborrag_runtime.config.errors import ConnectorConfigurationError


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    """One named connector and the inputs needed to construct it.

    ``settings`` contains literal provider options. ``setting_environment`` and
    ``secret_environment`` map provider config fields to environment variable
    names. The mappings are copied and exposed as read-only views so a loaded
    definition remains a stable construction recipe.
    """

    name: str
    provider: str
    enabled: bool = True
    settings: Mapping[str, Any] = field(default_factory=dict, repr=False)
    setting_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    secret_environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    base_directory: Path = field(default_factory=Path.cwd, repr=False)

    def __post_init__(self) -> None:
        """Detach stored mappings from mutable dictionaries owned by callers."""
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))
        object.__setattr__(
            self,
            "setting_environment",
            MappingProxyType(dict(self.setting_environment)),
        )
        object.__setattr__(
            self,
            "secret_environment",
            MappingProxyType(dict(self.secret_environment)),
        )

    def resolve_settings(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve the effective provider settings without building a connector.

        Precedence is provider defaults, YAML settings, referenced environment
        values, then explicit code overrides. Every referenced variable must be
        present and non-empty.
        """
        values = dict(self.settings)
        source_environment = os.environ if environment is None else environment

        for references in (self.setting_environment, self.secret_environment):
            for config_field, variable_name in references.items():
                value = source_environment.get(variable_name)
                if value is None or value == "":
                    raise ConnectorConfigurationError(
                        f"Connector {self.name!r} requires environment variable "
                        f"{variable_name!r} for {config_field!r}"
                    )
                values[config_field] = value

        if overrides:
            values.update(overrides)

        self._resolve_config_paths(values, overrides=overrides)
        return values

    def build(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        connector_kwargs: Mapping[str, Any] | None = None,
    ) -> HarborConnector:
        """Resolve settings, run provider validation, and build the connector.

        Raises:
            ConnectorConfigurationError: If the connector is disabled, a
                secret is missing, or provider settings fail the provider
                config dataclass validation.
        """
        if not self.enabled:
            raise ConnectorConfigurationError(
                f"Connector {self.name!r} is disabled (enabled: false) and cannot be built"
            )
        factory = config_factory(self.provider)
        if factory is None:  # Defensive for manually-created definitions.
            raise ConnectorConfigurationError(
                f"Connector {self.name!r} uses unsupported provider {self.provider!r}"
            )

        try:
            values = coerce_config_values(
                factory,
                self.resolve_settings(environment=environment, overrides=overrides),
            )
            provider_config = factory(**values)
            return HarborConnector(
                self.provider,
                config=provider_config,
                **dict(connector_kwargs or {}),
            )
        except (TypeError, ValueError) as exc:
            raise ConnectorConfigurationError(
                f"Connector {self.name!r} ({self.provider}) is invalid: {exc}"
            ) from exc

    def _resolve_config_paths(
        self,
        values: dict[str, Any],
        *,
        overrides: Mapping[str, Any] | None,
    ) -> None:
        """Resolve plugin-declared path settings relative to their origin."""

        definition = connector_registry.get_definition(self.provider)
        for field_name in definition.config_path_fields:
            value = values.get(field_name)
            if not isinstance(value, (str, Path)):
                continue
            candidate = Path(value).expanduser()
            if candidate.is_absolute():
                continue
            overridden = overrides is not None and field_name in overrides
            from_environment = field_name in self.setting_environment and not overridden
            base_directory = Path.cwd() if from_environment else self.base_directory
            values[field_name] = base_directory / candidate


@dataclass(frozen=True, slots=True)
class ConnectorCatalog:
    """A versioned, immutable collection of named connector definitions."""

    connectors: Mapping[str, ConnectorDefinition]
    source_path: Path
    version: int
    errors: Mapping[str, ConnectorConfigurationError] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Copy the connector and error maps into read-only views."""
        object.__setattr__(
            self,
            "connectors",
            MappingProxyType(dict(self.connectors)),
        )
        object.__setattr__(
            self,
            "errors",
            MappingProxyType(dict(self.errors)),
        )

    def get(self, name: str) -> ConnectorDefinition:
        """Return one definition or raise a configuration-specific error.

        If ``name`` failed to parse when the catalog was loaded, this raises
        that connector's own error rather than a generic "unknown" error.
        """
        if name in self.errors:
            raise self.errors[name]
        try:
            return self.connectors[name]
        except KeyError as exc:
            available = ", ".join(self.names()) or "none"
            raise ConnectorConfigurationError(
                f"Unknown configured connector: {name!r}. Available connector IDs: {available}"
            ) from exc

    def names(self, *, enabled_only: bool = False) -> list[str]:
        """Return configured names alphabetically, optionally filtering disabled."""
        return sorted(
            name
            for name, definition in self.connectors.items()
            if definition.enabled or not enabled_only
        )

    def build(
        self,
        name: str,
        *,
        environment: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
        connector_kwargs: Mapping[str, Any] | None = None,
    ) -> HarborConnector:
        """Build one configured connector by application-level name."""
        return self.get(name).build(
            environment=environment,
            overrides=overrides,
            connector_kwargs=connector_kwargs,
        )

    def build_enabled(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, HarborConnector]:
        """Build all enabled definitions, preserving their YAML order."""
        return {
            name: definition.build(environment=environment)
            for name, definition in self.connectors.items()
            if definition.enabled
        }
