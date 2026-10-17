"""Shared input checks for the access types."""

from __future__ import annotations


def require_text(value: object, name: str) -> None:
    """Raise ``ValueError`` unless ``value`` is a string with visible content."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
