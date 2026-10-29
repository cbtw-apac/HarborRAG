"""Builder that turns a stored connection into its provider config dataclass."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from harborrag_adapters.connectors.environment import without_environment_defaults

from ...errors import ConnectorConfigurationError
from ..providers import ConnectorConfigFactory, coerce_config_values
from .registry import SourceTypeRegistry, source_types
from .spec import SourceTypeSpec


class ConnectorConfigBuilder:
    """Assemble provider config keywords step by step, then construct the dataclass."""

    def __init__(self, spec: SourceTypeSpec, factory: ConnectorConfigFactory) -> None:
        self._spec = spec
        self._factory = factory
        self._credentials: Mapping[str, Any] = {}
        self._config: Mapping[str, Any] = {}
        self._tuning: Mapping[str, Any] = {}
        self._secret_values: Mapping[str, str] = {}

    @classmethod
    def for_source_type(cls, source_type: str, registry: SourceTypeRegistry = source_types) -> Self:
        """Start a builder for ``source_type`` (canonical name, provider or alias)."""
        return cls(registry.get(source_type), registry.provider_factory(source_type))

    def credentials(self, value: Mapping[str, Any]) -> Self:
        """Set validated credentials; secrets may be references."""
        self._credentials = value
        return self

    def config(self, value: Mapping[str, Any]) -> Self:
        """Set validated config."""
        self._config = value
        return self

    def tuning(self, value: Mapping[str, Any]) -> Self:
        """Set merged tuning overrides."""
        self._tuning = value
        return self

    def secret_values(self, value: Mapping[str, str]) -> Self:
        """Set resolved plaintext for credential fields held as references."""
        self._secret_values = value
        return self

    def values(self) -> dict[str, Any]:
        """Return the coerced dataclass keywords without constructing the dataclass."""
        flat = self._spec.to_connector_values(
            self._credentials, self._config, self._tuning, self._secret_values
        )
        return coerce_config_values(self._factory, flat)

    def build(self) -> object:
        """Construct the provider config dataclass without process-environment fallbacks.

        Returns:
            An instance of the provider's config dataclass.

        Raises:
            ConnectionValidationError: A secret reference has no resolved value.
            ConnectorConfigurationError: The dataclass rejects the values.
        """
        values = self.values()
        try:
            with without_environment_defaults():
                return self._factory(**values)
        except (TypeError, ValueError) as error:
            raise ConnectorConfigurationError(
                f"{self._spec.source_type} connection config is invalid: {error}"
            ) from error


def to_connector_config(
    source_type: str,
    credentials: Mapping[str, Any],
    config: Mapping[str, Any],
    tuning: Mapping[str, Any],
    secret_values: Mapping[str, str],
) -> object:
    """Build the provider config dataclass for a stored connection.

    Args:
        source_type: Canonical name, provider name or alias.
        credentials: Validated credentials; secrets may be references.
        config: Validated config.
        tuning: Merged tuning overrides.
        secret_values: Resolved plaintext for each secret held as a reference.

    Returns:
        An instance of the provider's config dataclass.

    Raises:
        ConnectionValidationError: The type is unknown or a secret is unresolved.
        ConnectorConfigurationError: The dataclass rejects the values.
    """
    return (
        ConnectorConfigBuilder.for_source_type(source_type)
        .credentials(credentials)
        .config(config)
        .tuning(tuning)
        .secret_values(secret_values)
        .build()
    )
