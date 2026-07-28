from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

_ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def expand_environment(value: Any) -> Any:
    """Recursively expand ``${VAR}`` and ``${VAR:-default}`` references."""

    if isinstance(value, str):
        full_match = _ENVIRONMENT_REFERENCE.fullmatch(value)
        if full_match:
            return _environment_value(*full_match.groups())
        return _ENVIRONMENT_REFERENCE.sub(
            lambda match: _environment_value(*match.groups()),
            value,
        )
    if isinstance(value, Mapping):
        return {key: expand_environment(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_environment(item) for item in value]
    if isinstance(value, tuple):
        return tuple(expand_environment(item) for item in value)
    return value


def _environment_value(name: str, default: str | None) -> str:
    resolved = os.getenv(name, default)
    if resolved is None:
        raise ValueError(f"environment variable {name} is not set")
    return resolved
