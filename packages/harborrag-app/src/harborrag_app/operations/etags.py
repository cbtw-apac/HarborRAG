"""Weak ETags of the form ``W/"rev-<version>"``, the wire form of a resource version.

Imports no web framework: the HTTP routes and the CLI both send and read these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harborrag_engine.control_plane.connection_views import ANY_VERSION

_ETAG = re.compile(r'^(?:W/)?"rev-(\d+)"$')


@dataclass(frozen=True, slots=True)
class Preconditions:
    """The ``If-Match`` and ``If-None-Match`` headers of a conditional write.

    Args:
        if_match: ETag the stored resource must still carry, or ``*``; ``None`` skips the check.
        if_none_match: ``*`` makes the write create-only.
    """

    if_match: str | None = None
    if_none_match: str | None = None


def etag(version: int) -> str:
    """Return the weak ETag of a resource version."""
    return f'W/"rev-{version}"'


def version_from_etag(value: str | None) -> int | None:
    """Parse ``If-Match``: ``None`` absent, ``ANY_VERSION`` for ``*``, ``-1`` for a foreign tag."""
    if value is None:
        return None
    if value.strip() == "*":
        return ANY_VERSION
    match = _ETAG.match(value.strip())
    return int(match.group(1)) if match else -1
