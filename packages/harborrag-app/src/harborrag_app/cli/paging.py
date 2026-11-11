"""Paging options shared by the list commands, and ``--all`` as one merged page.

Handles:
- ``--limit``, ``--cursor`` and ``--all`` as ``Annotated`` aliases, with the HTTP list limits
- Following ``next_cursor`` client-side, so ``--all`` prints one ``{"items": [...],
  "next_cursor": null}``

Does NOT call operations itself; the caller passes the fetch of one page.

Start from: collect_pages.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import typer

from harborrag_app.api.v1.admin.connection_schemas import ConnectionPage, ProjectPage
from harborrag_app.api.v1.ingestion.schemas import FailurePage, IngestionPage

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

LimitOption = Annotated[
    int, typer.Option("--limit", min=1, max=MAX_LIMIT, help="Page size, 1 to 100.")
]
CursorOption = Annotated[
    str | None, typer.Option("--cursor", help="next_cursor of the previous page.")
]
AllOption = Annotated[
    bool, typer.Option("--all", help="Follow next_cursor and print every page as one.")
]


async def collect_pages[P: (ProjectPage, ConnectionPage, IngestionPage, FailurePage)](
    fetch: Callable[[str | None], Awaitable[P]], *, cursor: str | None, follow: bool
) -> P:
    """Fetch one page, or with ``follow`` every page from ``cursor`` merged into one.

    Args:
        fetch: Fetches the page that starts at the cursor it is given.
        cursor: Where to start; ``None`` for the first page.
        follow: Keep fetching while a page names a ``next_cursor``.

    Returns:
        The page, or the last page holding every item with ``next_cursor`` ``None``.
    """
    page = await fetch(cursor)
    if not follow:
        return page
    items: list[Any] = list(page.items)
    while page.next_cursor is not None:
        page = await fetch(page.next_cursor)
        items.extend(page.items)
    return page.model_copy(update={"items": items})
