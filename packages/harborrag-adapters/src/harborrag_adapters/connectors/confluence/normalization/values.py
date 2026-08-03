"""Safe value coercion shared by Confluence normalization stages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def mapping(value: object) -> Mapping[str, Any]:
    """Return mapping values while replacing other provider values with an empty mapping."""
    return value if isinstance(value, Mapping) else {}


def mapping_sequence(value: object) -> tuple[Mapping[str, Any], ...]:
    """Keep only mapping items from a non-string provider sequence."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))
