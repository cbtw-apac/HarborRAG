from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from harborrag_runtime.config.errors import ConfigurationError

type ConfigurationErrorType = type[ConfigurationError]


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


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
        # An absolute container path (e.g. `/app/config/parsers.yaml`) that is
        # "missing" almost always means the file never made it into the
        # running filesystem view rather than a typo in the path itself --
        # name the likely causes so an operator doesn't have to rediscover
        # them from a bare traceback.
        hint = (
            "; check that the image build actually copied this file in (a "
            "stale image predating the file, or a build context/.dockerignore "
            "excluding it), that no volume/bind mount is shadowing the "
            "directory with an empty one (Docker silently creates a missing "
            "bind-mount source as empty), and that the configured path "
            "env var points at the intended file"
            if source_path.is_absolute()
            else ""
        )
        raise error_type(f"{label} file does not exist: {source_path}{hint}")

    try:
        value = yaml.load(
            source_path.read_text(encoding="utf-8"),
            Loader=_UniqueKeyLoader,
        )
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


def require_nonblank_string(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> str:
    """Require a non-empty string with no surrounding whitespace."""
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise error_type(f"{label} must be a non-empty string without outer whitespace")
    return value


def require_optional_nonblank_string(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> str | None:
    """Require either null or a non-empty string with no surrounding whitespace."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise error_type(f"{label} must be null or a non-empty string without outer whitespace")
    return value


def require_integer(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> int:
    """Require an actual integer rather than a boolean or coerced scalar."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(f"{label} must be an integer")
    return value


def require_finite_number(
    value: object,
    *,
    label: str,
    error_type: ConfigurationErrorType = ConfigurationError,
) -> float:
    """Require a finite integer or floating-point value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise error_type(f"{label} must be a number")
    parsed = float(value)
    if not isfinite(parsed):
        raise error_type(f"{label} must be finite")
    return parsed


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
