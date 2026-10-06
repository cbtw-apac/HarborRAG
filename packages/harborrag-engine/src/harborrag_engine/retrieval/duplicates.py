"""Collapse search hits that carry the same text.

A Jira tenant is full of identical short chunks -- "Bounced - Unable to Send
Mailing" posted on hundreds of issues, a three-word "Senior Java Engineer"
comment -- and identical text embeds identically, so they rank together and
can fill every slot of a result page with one sentence. A second copy tells the
caller nothing the first did not, so only the best-ranked one is kept and the
search window behind it supplies the next distinct hit.

Identity is the payload's ``content_hash``: the projection's own hash of the
chunk text. A hit without one is never collapsed, because guessing equality
from anything else could merge two different pieces of evidence.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence


def collapse_duplicates[T](
    items: Sequence[T],
    content_hash: Callable[[T], object],
    *,
    limit: int | None = None,
) -> tuple[tuple[T, ...], int]:
    """Keep the first item per content hash, in rank order.

    Returns the distinct items and how many duplicates were dropped on the way to
    the ``limit``-th distinct one. Duplicates past that point never displaced a
    returned hit, so they are not counted as collapsed.
    """

    seen: set[str] = set()
    kept: list[T] = []
    collapsed = 0
    for item in items:
        value = content_hash(item)
        if isinstance(value, str) and value:
            if value in seen:
                if limit is None or len(kept) < limit:
                    collapsed += 1
                continue
            seen.add(value)
        kept.append(item)
    return tuple(kept), collapsed


__all__ = ["collapse_duplicates"]
