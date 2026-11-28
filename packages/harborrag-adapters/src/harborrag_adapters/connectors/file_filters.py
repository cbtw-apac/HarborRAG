"""Shared file-name and file-filter normalization used by file-backed connectors.

Extensions are read from a file name with the whitespace and zero-width
characters around it removed, so every connector filters a file the same way.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath

_EDGE_BLANKS = re.compile(r"^[\s\u200b-\u200d\u2060\ufeff]+|[\s\u200b-\u200d\u2060\ufeff]+$")


def trim_file_name(name: str) -> str:
    """Return a file name without the whitespace and zero-width characters around it.

    Names from uploads, pasted text and other systems often end in a space, a
    no-break space or an invisible zero-width character. None of them is part of
    the name a user sees, and each would hide the file's extension.
    """
    return _EDGE_BLANKS.sub("", name)


def file_name_extension(name: str) -> str:
    """Return the lowercased extension of a file name or path, or ``""`` without one.

    The last path segment is trimmed with ``trim_file_name`` first, so a name
    such as ``notes.md`` followed by a space gives ``.md``.
    """
    file_name = trim_file_name(PurePosixPath(name).name)
    return PurePosixPath(file_name).suffix.lower() if "." in file_name else ""


def normalize_extension(value: str) -> str:
    """Normalize extension filters to lowercased dot-prefixed values."""
    normalized = value.lower().strip()
    return normalized if normalized.startswith(".") else f".{normalized}"


@dataclass(frozen=True, slots=True)
class ExtensionFilter:
    """Keep files by extension; an excluded extension wins over an allowed one.

    Attributes:
        allowed: Dot-prefixed lowercase extensions to keep; empty keeps every one.
        excluded: Dot-prefixed lowercase extensions to leave out.
    """

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

        Returns:
            The reason the file is left out, or None when the filter keeps it.
        """
        if extension in self.excluded:
            return f"extension {extension} is in excluded_extensions"
        if self.allowed and extension not in self.allowed:
            return f"extension {extension or '(none)'} is not in allowed_extensions"
        return None
