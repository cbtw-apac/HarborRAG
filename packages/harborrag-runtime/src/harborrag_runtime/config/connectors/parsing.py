"""Validate declarative connector configuration and produce typed definitions."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from harborrag_runtime.config.connectors.providers import (
    PYTHON_ONLY_CONFIG_FIELDS,
    SECRET_CONFIG_FIELDS,
    canonical_provider_name,
    config_factory,
    config_field_names,
    supported_provider_names,
    validate_literal_collection_shapes,
)
from harborrag_runtime.config.connectors.schemas import ConnectorDefinition
from harborrag_runtime.config.errors import ConnectorConfigurationError
from harborrag_runtime.config.loading import (
    parse_environment_references,
    reject_unknown_keys,
    require_boolean,
    require_string_mapping,
)

_CONNECTOR_KEYS = frozenset({"enabled", "environment", "provider", "secrets", "settings"})


def parse_connector_definitions(
    raw_connectors: Mapping[str, Any],
    *,
    base_directory: Path,
) -> dict[str, ConnectorDefinition]:
    """Parse all named connector definitions from the top-level mapping."""
    return {
        _validate_name(name): _parse_definition(
            name,
            raw_definition,
            base_directory=base_directory,
        )
        for name, raw_definition in raw_connectors.items()
    }


def _validate_name(name: str) -> str:
    """Require a non-empty application-level connector name."""
    if not name.strip():
        raise ConnectorConfigurationError("Connector names must be non-empty strings")
    return name


def _parse_definition(
    name: str,
    raw: object,
    *,
    base_directory: Path,
) -> ConnectorDefinition:
    """Validate one YAML definition and convert it to a runtime model."""
    definition = require_string_mapping(
        raw,
        label=f"connector {name!r}",
        error_type=ConnectorConfigurationError,
    )
    reject_unknown_keys(
        definition,
        _CONNECTOR_KEYS,
        label=f"connector {name!r}",
        error_type=ConnectorConfigurationError,
    )

    provider = _parse_provider(name, definition.get("provider"))
    enabled = _parse_enabled(name, definition.get("enabled", True))
    settings = dict(
        require_string_mapping(
            definition.get("settings", {}),
            label=f"connector {name!r} settings",
            error_type=ConnectorConfigurationError,
        )
    )
    setting_environment = _parse_setting_environment(
        name,
        definition.get("environment", {}),
    )
    secret_environment = _parse_secret_environment(
        name,
        definition.get("secrets", {}),
    )
    _validate_provider_fields(
        name,
        provider,
        settings,
        setting_environment,
        secret_environment,
    )
    factory = config_factory(provider)
    if factory is not None:
        try:
            validate_literal_collection_shapes(factory, settings)
        except ValueError as exc:
            raise ConnectorConfigurationError(
                f"Connector {name!r} ({provider}) is invalid: {exc}"
            ) from exc
    _validate_yaml_boundaries(
        name,
        settings,
        setting_environment,
        secret_environment,
    )

    return ConnectorDefinition(
        name=name,
        provider=provider,
        enabled=enabled,
        settings=settings,
        setting_environment=setting_environment,
        secret_environment=secret_environment,
        base_directory=base_directory,
    )


def _parse_provider(name: str, raw_provider: object) -> str:
    """Normalize a provider alias and reject unsupported providers."""
    if not isinstance(raw_provider, str) or not raw_provider.strip():
        raise ConnectorConfigurationError(f"Connector {name!r} must define a non-empty provider")
    provider = canonical_provider_name(raw_provider)
    if config_factory(provider) is None:
        supported = ", ".join(supported_provider_names())
        raise ConnectorConfigurationError(
            f"Connector {name!r} uses unsupported provider {raw_provider!r}; "
            f"expected one of: {supported}"
        )
    return provider


def _parse_enabled(name: str, raw_enabled: object) -> bool:
    """Require the optional enabled switch to be a YAML boolean."""
    return require_boolean(
        raw_enabled,
        label=f"Connector {name!r} enabled",
        error_type=ConnectorConfigurationError,
    )


def _parse_secret_environment(name: str, raw: object) -> dict[str, str]:
    """Convert ``<field>_env`` entries to config-field/environment mappings."""
    return parse_environment_references(
        raw,
        label=f"connector {name!r} secrets",
        key_suffix="_env",
        error_type=ConnectorConfigurationError,
    )


def _parse_setting_environment(name: str, raw: object) -> dict[str, str]:
    """Map provider setting names to environment variable names."""
    return parse_environment_references(
        raw,
        label=f"connector {name!r} environment",
        error_type=ConnectorConfigurationError,
    )


def _validate_provider_fields(
    name: str,
    provider: str,
    settings: dict[str, Any],
    setting_environment: dict[str, str],
    secret_environment: dict[str, str],
) -> None:
    """Reject fields absent from the selected provider config dataclass."""
    factory = config_factory(provider)
    if factory is None:  # Provider support was checked before this call.
        raise ConnectorConfigurationError(
            f"Connector {name!r} uses unsupported provider {provider!r}"
        )
    known_fields = config_field_names(factory)
    configured_fields = set(settings) | set(setting_environment) | set(secret_environment)
    unknown = sorted(configured_fields - known_fields)
    if unknown:
        raise ConnectorConfigurationError(
            f"Connector {name!r} has unknown {provider} setting(s): {', '.join(unknown)}"
        )


def _validate_yaml_boundaries(
    name: str,
    settings: dict[str, Any],
    setting_environment: dict[str, str],
    secret_environment: dict[str, str],
) -> None:
    """Keep plaintext secrets and Python objects outside declarative YAML."""
    protected = sorted(set(settings) & SECRET_CONFIG_FIELDS)
    if protected:
        raise ConnectorConfigurationError(
            f"Connector {name!r} must reference secret setting(s) through "
            f"secrets/*_env: {', '.join(protected)}"
        )

    environment_secrets = sorted(set(setting_environment) & SECRET_CONFIG_FIELDS)
    if environment_secrets:
        raise ConnectorConfigurationError(
            f"Connector {name!r} must reference secret setting(s) through "
            f"secrets/*_env: {', '.join(environment_secrets)}"
        )

    configured_fields = set(settings) | set(setting_environment) | set(secret_environment)
    programmatic = sorted(configured_fields & PYTHON_ONLY_CONFIG_FIELDS)
    if programmatic:
        raise ConnectorConfigurationError(
            f"Connector {name!r} setting(s) are Python-only and cannot be loaded "
            f"from YAML: {', '.join(programmatic)}"
        )

    overlaps = {
        "settings and environment": set(settings) & set(setting_environment),
        "settings and secrets": set(settings) & set(secret_environment),
        "environment and secrets": set(setting_environment) & set(secret_environment),
    }
    for locations, overlap in overlaps.items():
        if not overlap:
            continue
        raise ConnectorConfigurationError(
            f"Connector {name!r} defines setting(s) in both {locations}: "
            f"{', '.join(sorted(overlap))}"
        )
