"""Safe value coercion for provider payloads."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def mapping(value: object) -> Mapping[str, Any]:
    """Return ``value`` if it is a mapping, else an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    """Return the mapping items of a non-string sequence, else an empty tuple."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))
