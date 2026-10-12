"""Paginators that expose a resume token after every page.

A connector stores ``Page.next_token`` in its checkpoint; resuming from any
token yields exactly the remaining pages. A server that hands back a token
it already gave, or that never stops, is cut off with :class:`Transient`
instead of looping forever.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from harborrag_core.sources import Transient

DEFAULT_MAX_PAGES = 10_000
_LINK = re.compile(r"<([^>]*)>\s*((?:;\s*[^;,]+)*)")
_REL = re.compile(r"""rel\s*=\s*"?([^";]+)"?""", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of items and the token that fetches the page after it."""

    items: Sequence[T]
    next_token: str | None


async def iterate_pages[T](
    fetch_page: Callable[[str | None], Awaitable[Page[T]]],
    start_token: str | None = None,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> AsyncIterator[Page[T]]:
    """Yield pages from ``start_token`` until a page has no next token."""

    seen: set[str] = set() if start_token is None else {start_token}
    token = start_token
    for _ in range(max_pages):
        page = await fetch_page(token)
        yield page
        if page.next_token is None:
            return
        if page.next_token in seen:
            raise Transient("the source repeated a pagination token; stopping to avoid a loop")
        seen.add(page.next_token)
        token = page.next_token
    raise Transient(f"pagination exceeded {max_pages} pages")


def next_link(headers: Mapping[str, str]) -> str | None:
    """The ``rel="next"`` target of an RFC 8288 ``Link`` header (GitHub, GitLab)."""

    value = headers.get("link")
    if not value:
        return None
    for match in _LINK.finditer(value):
        url, params = match.groups()
        for rel in _REL.findall(params):
            if "next" in rel.lower().split():
                return url
    return None


def offset_token(offset: int) -> str:
    if offset < 0:
        raise ValueError("offset must not be negative")
    return str(offset)


def parse_offset(token: str | None) -> int:
    """Invert :func:`offset_token`; ``None`` is the first page."""

    if token is None:
        return 0
    if not token.isdigit():
        raise ValueError(f"not an offset token: {token!r}")
    return int(token)
