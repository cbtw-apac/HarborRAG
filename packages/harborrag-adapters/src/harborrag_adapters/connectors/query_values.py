from __future__ import annotations

from collections.abc import Sequence


def normalized_string_list(
    value: object,
    *,
    default: Sequence[str],
) -> list[str]:
    """Normalize one connector query field without mutating its defaults."""

    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        raise ValueError("connector list filter must be a string or sequence")
    return [str(item) for item in value]
