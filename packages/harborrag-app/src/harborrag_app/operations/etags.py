"""Weak ETags of the form ``W/"rev-<version>"``, the wire form of a resource version.

Imports no web framework: the HTTP routes and the CLI both send and read these.
"""

from __future__ import annotations

import re

from harborrag_engine.control_plane.connection_views import ANY_VERSION

_ETAG = re.compile(r'^(?:W/)?"rev-(\d+)"$')


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
