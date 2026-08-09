"""Provider-response normalization shared by model families."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SAFE_PROVIDER_METADATA = ("region_name", "cache_hit", "model_id", "response_ms")


def nonnegative_int(value: Any) -> int:
    """Coerce non-negative numeric counters while treating invalid values as zero."""
    return int(value) if isinstance(value, int | float) and value >= 0 else 0


def safe_provider_metadata(hidden: Mapping[str, Any]) -> dict[str, Any]:
    """Select scalar, non-secret provider metadata for normalized responses."""
    return {
        key: hidden[key]
        for key in _SAFE_PROVIDER_METADATA
        if key in hidden
        and (isinstance(hidden.get(key), str | int | float | bool) or hidden.get(key) is None)
    }
