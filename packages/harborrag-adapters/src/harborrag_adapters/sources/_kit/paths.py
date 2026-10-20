"""Gitignore-style path rules for include and exclude filters.

Patterns follow gitignore syntax (via ``pathspec``): ``**/x`` matches a
root-level ``x``, ``*`` does not cross ``/``, and ``dir/`` covers everything
below it. A path the source reports is never rewritten: it is only
NFC-normalised for matching, so a ``\\`` in a Git file name stays a name
character and never becomes a separator.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import pathspec


def _spec(patterns: Iterable[str]) -> pathspec.GitIgnoreSpec | None:
    lines = [unicodedata.normalize("NFC", p) for p in patterns]
    return pathspec.GitIgnoreSpec.from_lines(lines) if lines else None


@dataclass(frozen=True)
class PathRules:
    """Include and exclude patterns; exclude wins, empty include admits all."""

    _include: pathspec.GitIgnoreSpec | None
    _exclude: pathspec.GitIgnoreSpec | None

    @classmethod
    def of(cls, include: Iterable[str] = (), exclude: Iterable[str] = ()) -> PathRules:
        return cls(_spec(include), _spec(exclude))

    def admits(self, path: str) -> bool:
        path = unicodedata.normalize("NFC", path)
        if self._exclude is not None and self._exclude.match_file(path):
            return False
        return self._include is None or self._include.match_file(path)


def is_within(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or below it, compared segment by segment."""
    root = root.strip("/")
    if not root:
        return True
    path = path.strip("/")
    return path == root or path.startswith(root + "/")
