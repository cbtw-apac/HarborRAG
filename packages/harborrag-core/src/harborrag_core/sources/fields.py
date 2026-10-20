"""Naming rule for filterable source fields."""

from __future__ import annotations

import re

_FIELD_KEY_SEPARATORS = re.compile(r"[^a-z0-9]+")


def field_key(name: str) -> str:
    """The payload key one custom field is filtered by: ``Skill Set`` -> ``skill_set``.

    ASCII-only on purpose: stored payloads are keyed this way, so widening it
    would re-key them.
    """

    return _FIELD_KEY_SEPARATORS.sub("_", name.casefold()).strip("_")
