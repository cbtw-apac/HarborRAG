"""Isolate the bad items of a failing batch read.

A bulk endpoint (Jira ``issue/bulkfetch``) fails as a whole when one item in
the batch cannot be served or decoded. Splitting the batch in halves until
the failure is pinned down costs one ``ItemFailure`` for the bad item
instead of losing the batch. Run-level errors are not item problems and
propagate untouched.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from harborrag_core.sources import (
    AuthError,
    ConfigError,
    DocKey,
    Forbidden,
    ItemFailure,
    RateLimited,
)

from .errors import UnexpectedStatus

RUN_LEVEL_ERRORS: tuple[type[BaseException], ...] = (AuthError, Forbidden, RateLimited)
"""Errors that end the run rather than mark one item."""


def _is_run_level(error: BaseException) -> bool:
    if isinstance(error, RUN_LEVEL_ERRORS):
        return True
    # A bad query or blocked host is a configuration problem for the whole
    # run; an unexpected 4xx may be caused by one item, so it is bisected.
    return isinstance(error, ConfigError) and not isinstance(error, UnexpectedStatus)


async def bisect_batch[I, R](
    items: Sequence[I],
    fetch: Callable[[Sequence[I]], Awaitable[Sequence[R]]],
    *,
    key_of: Callable[[I], DocKey],
    reason: str = "batch_read_failed",
) -> AsyncIterator[R | ItemFailure]:
    """Yield ``fetch`` results for every readable item and one failure per bad item."""

    pending: list[Sequence[I]] = [items] if items else []
    while pending:
        batch = pending.pop()
        try:
            results = await fetch(batch)
        except Exception as error:
            if _is_run_level(error):
                raise
            if len(batch) == 1:
                yield ItemFailure(reason, detail=str(error)[:300], key=key_of(batch[0]))
                continue
            middle = len(batch) // 2
            pending.extend((batch[middle:], batch[:middle]))
            continue
        for result in results:
            yield result
