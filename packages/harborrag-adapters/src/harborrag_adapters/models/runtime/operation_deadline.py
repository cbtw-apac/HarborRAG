"""Optional monotonic budget shared by retries within a model operation."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import overload

_deadline: ContextVar[float | None] = ContextVar("model_operation_deadline", default=None)


class OperationDeadlineExceeded(TimeoutError):
    """The enclosing operation has exhausted its total time budget."""


@contextmanager
def operation_deadline(seconds: float | None) -> Iterator[None]:
    """Share the earliest deadline with nested work and restore caller state."""

    inherited = _deadline.get()
    selected = time.monotonic() + seconds if seconds is not None else inherited
    if inherited is not None and selected is not None:
        selected = min(inherited, selected)
    token = _deadline.set(selected)
    try:
        remaining_timeout()
        yield
        remaining_timeout()
    finally:
        _deadline.reset(token)


@overload
def remaining_timeout(limit: float) -> float: ...


@overload
def remaining_timeout(limit: None = None) -> float | None: ...


def remaining_timeout(limit: float | None = None) -> float | None:
    """Clip a provider timeout to the operation budget, or fail before more work."""

    deadline = _deadline.get()
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OperationDeadlineExceeded("model operation deadline exceeded")
    return min(limit, remaining) if limit is not None else remaining


def sleep_with_deadline(seconds: float) -> None:
    """Bound synchronous retry backoff by the same operation deadline."""

    delay = remaining_timeout(seconds)
    if delay:
        time.sleep(delay)
    remaining_timeout()
