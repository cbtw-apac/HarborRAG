"""Shared input checks for the access types."""

from __future__ import annotations

from collections.abc import Iterable


def require_text(value: object, name: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a string with visible content."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def text_items(values: object, name: str) -> tuple[str, ...]:
    """The items of a collection of non-blank strings, in order.

    A bare ``str`` or ``bytes`` is refused rather than read as its characters,
    and so is anything that is not iterable.
    """

    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be a collection of strings")
    items = tuple(values)
    for item in items:
        require_text(item, name)
    return items
