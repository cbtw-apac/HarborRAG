"""Deterministic conversion of runtime values into JSON-compatible data."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


def to_json_value(value: object) -> JsonValue:
    """Convert supported configuration and source metadata without lossy fallbacks."""

    if value is None or isinstance(value, (bool, float, int, str)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        converted = [to_json_value(item) for item in value]
        return sorted(
            converted,
            key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True),
        )
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [to_json_value(item) for item in value]
    raise TypeError(f"{type(value).__name__} is not JSON serializable")
