from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def coerce_sdk_mapping(value: Any) -> dict[str, Any]:
    """Convert SDK mappings and model-like response objects into plain dictionaries."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("model_dump", "dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            result = method()
            return dict(result) if isinstance(result, Mapping) else {}
    return {}


def sdk_hidden_parameters(value: Any, data: Mapping[str, Any]) -> dict[str, Any]:
    """Extract LiteLLM hidden parameters from an object or normalized response mapping."""

    hidden = getattr(value, "_hidden_params", None)
    if isinstance(hidden, Mapping):
        return dict(hidden)
    return coerce_sdk_mapping(data.get("_hidden_params"))
