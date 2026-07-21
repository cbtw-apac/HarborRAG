from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from harborrag_runtime.config.errors import ConfigurationError

type ConfigurationErrorType = type[ConfigurationError]


def read_yaml_file(
    path: str | Path,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> tuple[Path, object]:
    """Read a YAML file and return its absolute path and decoded value.

    Args:
        path: File to read. User-home markers and relative segments are resolved.
        label: Human-readable configuration name used in error messages.
        error_type: Configuration exception subclass raised on failure.

    Raises:
        ConfigurationError: If the path is not a file or YAML cannot be decoded.
    """
    source_path = Path(path).expanduser().resolve()
    if not source_path.is_file():
        raise error_type(f"{label} file does not exist: {source_path}")

    try:
        value = yaml.safe_load(source_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise error_type(f"Could not read {label} {source_path}: {exc}") from exc
    return source_path, value


def require_string_mapping(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> Mapping[str, Any]:
    """Require a mapping whose keys are all strings.

    YAML permits non-string keys, but configuration field names must be strings
    so error reporting and strict unknown-field checks remain deterministic.
    """
    if not isinstance(value, Mapping):
        raise error_type(f"{label} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise error_type(f"{label} keys must be strings")
    return value


def reject_unknown_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> None:
    """Reject mapping fields outside an explicitly supported schema."""
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise error_type(f"{label} has unknown field(s): {', '.join(unknown)}")


def require_boolean(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> bool:
    """Require an actual YAML boolean instead of a truthy scalar."""
    if not isinstance(value, bool):
        raise error_type(f"{label} must be a boolean")
    return value


def require_schema_version(
    value: object,
    *,
    expected: int,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> int:
    """Validate an integer configuration schema version."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{label} version must be {expected}")
    if value != expected:
        raise error_type(f"Unsupported {label.lower()} version {value}; expected {expected}")
    return value


def parse_environment_references(
    value: object,
    *,
    label: str,
    key_suffix: str | None = None,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> dict[str, str]:
    """Validate environment references and optionally strip a key suffix."""
    references = require_string_mapping(
        value,
        label=label,
        error_type=error_type,
    )
    parsed: dict[str, str] = {}
    for key, variable_name in references.items():
        if not key.strip():
            raise error_type(f"{label} keys must be non-empty strings")
        if key_suffix is not None:
            if not key.endswith(key_suffix) or len(key) <= len(key_suffix):
                raise error_type(f"{label} keys must use the <setting>{key_suffix} form")
            target = key.removesuffix(key_suffix)
        else:
            if key != key.strip():
                raise error_type(f"{label} keys must not have surrounding whitespace: {key!r}")
            target = key
            if target in parsed:
                raise error_type(f"{label} defines {target!r} more than once")
        if not isinstance(variable_name, str) or not variable_name.strip():
            raise error_type(f"{label} reference {key!r} must name an environment variable")
        parsed[target] = variable_name.strip()
    return parsed
