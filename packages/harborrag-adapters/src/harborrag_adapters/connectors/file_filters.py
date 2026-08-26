"""Shared file-filter normalization used by file-backed connectors."""

from __future__ import annotations


def normalize_extension(value: str) -> str:
    """Normalize extension filters to lowercased dot-prefixed values."""
    normalized = value.lower().strip()
    return normalized if normalized.startswith(".") else f".{normalized}"
