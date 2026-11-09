"""The cursor paging every list route shares: ``?limit=`` 1–100 (20 by default) and ``?cursor=``.

A list answers ``{"items": [...], "next_cursor": ...}``; ``next_cursor`` is ``null`` on the
last page. A cursor the server did not issue is ``422 validation_error`` on field ``cursor``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Page:
    """One page request: its size and where the previous page ended."""

    limit: int
    cursor: str | None


def page_query(
    limit: Annotated[
        int, Query(ge=1, le=MAX_LIMIT, description="Page size, 1 to 100")
    ] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None,
        Query(min_length=1, max_length=512, description="next_cursor from the previous page"),
    ] = None,
) -> Page:
    """Read the page a list request asks for."""
    return Page(limit=limit, cursor=cursor)


PageDependency = Annotated[Page, Depends(page_query)]
