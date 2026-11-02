"""Builder that turns a stored connection into its provider config dataclass."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from harborrag_adapters.connectors.environment import without_environment_defaults

from ...errors import ConnectorConfigurationError
from ..providers import ConnectorConfigFactory, coerce_config_values
from .registry import SourceTypeRegistry, source_types
from .spec import FieldIssue, SourceTypeSpec, ValidatedConnection

_PLACEHOLDER_SECRET = "placeholder"


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


def connector_config_issues(
    spec: SourceTypeSpec, validated: ValidatedConnection
) -> list[FieldIssue]:
    """Build the provider config with placeholder secrets and report what it rejects.

    Catches the rules a JSON Schema cannot hold, such as SharePoint's
    ``item_ids`` against ``root_path``, before the values are saved.

    Args:
        spec: The connection's source type.
        validated: Values that passed the source type's schemas.

    Returns:
        One issue naming the rejected field when it can be told, else ``config``;
        none for a type whose ``build_check`` is off.
    """
    if not spec.build_check:
        return []
    placeholders = {
        name: _PLACEHOLDER_SECRET
        for name, value in validated.credentials.items()
        if isinstance(value, Mapping)
    }
    try:
        (
            ConnectorConfigBuilder.for_source_type(spec.source_type)
            .credentials(validated.credentials)
            .config(validated.config)
            .tuning(validated.tuning)
            .secret_values(placeholders)
            .build()
        )
    except ConnectorConfigurationError as error:
        text = str(error.__cause__ or error)
        return [FieldIssue(_field_of(spec, text), text)]
    return []


def _field_of(spec: SourceTypeSpec, text: str) -> str:
    """Name the field a provider error starts with, as ``<part>.<field>``, else ``config``."""
    first = text.split(" ", 1)[0].strip(":,.'\"")
    public = {target: name for name, target in spec.credential_renames.items()}.get(first, first)
    if public in spec.credential_fields:
        return f"credentials.{public}"
    if public in spec.tuning_fields:
        return f"advanced.{public}"
    if public in spec.config_fields:
        return f"config.{public}"
    return "config"
