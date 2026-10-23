"""Normalization of list-valued connector query fields."""

from __future__ import annotations

from collections.abc import Sequence


def normalized_string_list(
    value: object,
    *,
    default: Sequence[str],
) -> list[str]:
    """Return a query filter value as a list of strings.

    Args:
        value: ``None``, a single string, or a sequence of values.
        default: Used, copied, when ``value`` is ``None``.

    Raises:
        ValueError: If ``value`` is neither a string nor a sequence.
    """
    if value is None:
        return list(default)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence):
        raise ValueError("connector list filter must be a string or sequence")
    return [str(item) for item in value]
