"""Process-environment fallbacks for connector credentials.

Handles:
- ``environment_default``: the ``os.getenv`` fallback config dataclasses use for unset credentials
- ``without_environment_defaults``: a scope where that fallback is off, so a
  connection built from stored values never borrows the operator's credentials
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_FALLBACK_ENABLED: ContextVar[bool] = ContextVar(
    "harborrag_connector_environment_fallback", default=True
)


def environment_default(*names: str) -> str | None:
    """Return the first non-empty variable among ``names`` while fallbacks are on.

    Args:
        names: Environment variable names, in priority order.

    Returns:
        The value, or ``None`` when fallbacks are off or no variable is set.
    """
    if not _FALLBACK_ENABLED.get():
        return None
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


@contextmanager
def without_environment_defaults() -> Iterator[None]:
    """Disable ``environment_default`` for config dataclasses built inside this scope."""
    token = _FALLBACK_ENABLED.set(False)
    try:
        yield
    finally:
        _FALLBACK_ENABLED.reset(token)
