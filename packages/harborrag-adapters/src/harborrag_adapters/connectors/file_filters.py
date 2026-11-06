"""Shared file-filter normalization used by file-backed connectors."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


def normalize_extension(value: str) -> str:
    """Normalize extension filters to lowercased dot-prefixed values."""
    normalized = value.lower().strip()
    return normalized if normalized.startswith(".") else f".{normalized}"


@dataclass(frozen=True, slots=True)
class ExtensionFilter:
    """Keep files by extension; an excluded extension wins over an allowed one."""

    allowed: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()

    @classmethod
    def of(cls, allowed: Iterable[str] = (), excluded: Iterable[str] = ()) -> ExtensionFilter:
        """Build a filter from configured values such as ``pdf`` or ``.PDF``."""
        return cls(
            allowed=frozenset(normalize_extension(value) for value in allowed if value.strip()),
            excluded=frozenset(normalize_extension(value) for value in excluded if value.strip()),
        )

    def rejection(self, extension: str) -> str | None:
        """Return why a file with ``extension`` is left out, or None when it is kept.

        Args:
            extension: Dot-prefixed lowercase extension, or empty when the file has none.
        """
        if extension in self.excluded:
            return f"extension {extension} is in excluded_extensions"
        if self.allowed and extension not in self.allowed:
            return f"extension {extension or '(none)'} is not in allowed_extensions"
        return None
