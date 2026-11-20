"""Success wrappers an operation returns when the transport needs more than the body.

Each transport chooses its own status codes and header names.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Tagged[T]:
    """A body read with its entity tag.

    Args:
        body: The response body.
        etag: Entity tag of the body.
    """

    body: T
    etag: str


@dataclass(frozen=True, slots=True)
class Saved[T]:
    """A body written with its new entity tag.

    Args:
        body: The response body.
        etag: Entity tag of the stored resource.
        created: Whether the write created the resource rather than replaced it.
    """

    body: T
    etag: str
    created: bool


@dataclass(frozen=True, slots=True)
class Accepted[T]:
    """A body for work that continues after the call returns.

    Args:
        body: The response body.
        location: Path of the resource that tracks the work.
    """

    body: T
    location: str
