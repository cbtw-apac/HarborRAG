"""File-filter normalization shared by file-backed connectors."""

from __future__ import annotations


def normalize_extension(value: str) -> str:
    """Return an extension filter lowercased, stripped and dot-prefixed."""
    normalized = value.lower().strip()
    return normalized if normalized.startswith(".") else f".{normalized}"
