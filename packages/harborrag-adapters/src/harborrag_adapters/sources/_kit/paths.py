"""Gitignore-style path rules for include and exclude filters.

Patterns follow gitignore syntax (via ``pathspec``): ``**/x`` matches a
root-level ``x``, ``*`` does not cross ``/``, and ``dir/`` covers everything
below it. A path the source reports is never rewritten: it is only
NFC-normalised for matching, so a ``\\`` in a Git file name stays a name
character and never becomes a separator. Blank lines and ``#`` comments are
dropped, so a list holding only those admits everything, and an invalid
pattern is a :class:`~harborrag_core.sources.ConfigError`.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

import pathspec

from harborrag_core.sources import ConfigError


def _spec(patterns: Iterable[str]) -> pathspec.GitIgnoreSpec | None:
    lines = [
        unicodedata.normalize("NFC", p) for p in patterns if p.strip() and not p.startswith("#")
    ]
    for line in lines:  # compile one by one so the error names the pattern
        try:
            pathspec.GitIgnoreSpec.from_lines([line])
        except ValueError as error:
            raise ConfigError(f"invalid path pattern {line!r}") from error
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
