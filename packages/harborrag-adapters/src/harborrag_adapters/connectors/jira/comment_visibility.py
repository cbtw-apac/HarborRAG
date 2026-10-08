"""Jira comment visibility: only comments (and worklogs) every issue reader can see are shared.

An issue becomes one document with one access decision. A comment Jira limits to
a project role or group (``visibility``), or a Jira Service Management internal
note (``jsdPublic: false``), is narrower than its issue, so it is dropped rather
than merged into text that every reader of the issue would receive.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def is_restricted_comment(comment: Mapping[str, Any]) -> bool:
    """Return whether Jira shows this comment to fewer readers than its issue.

    Any non-null ``visibility`` counts, including an empty or malformed value:
    Jira only sends the key for restricted comments, so an unexpected shape is
    treated as restricted rather than guessed to be public.
    """

    if comment.get("visibility") is not None:
        return True
    return comment.get("jsdPublic") is False


def shareable_comments(comments: Iterable[object]) -> list[dict[str, Any]]:
    """Keep the comments every reader of the issue may see, in source order."""

    return [
        comment
        for comment in comments
        if isinstance(comment, dict) and not is_restricted_comment(comment)
    ]


# Embedded blocks whose entries carry their own ``visibility``: (field, list key).
_RESTRICTABLE_BLOCKS = (("comment", "comments"), ("worklog", "worklogs"))


def redact_restricted_comments(issue: dict[str, Any]) -> dict[str, Any]:
    """Drop restricted comments and worklogs from an issue's embedded pages, in place.

    Worklogs carry a ``comment`` and the same ``visibility`` restriction as
    comments, so they are filtered by the same predicate. ``get_issue`` expands
    ``renderedFields``, whose ``comment`` and ``worklog`` blocks repeat every entry
    as rendered HTML, restricted ones included, and may omit ``visibility``.
    Rendered entries are kept only when their id survived the ``fields`` filter;
    without that list the rendered block is dropped.
    """

    fields = issue.get("fields")
    rendered = issue.get("renderedFields")
    for block_name, list_key in _RESTRICTABLE_BLOCKS:
        embedded = fields.get(block_name) if isinstance(fields, dict) else None
        shareable_ids: set[str] | None = None
        if isinstance(embedded, dict) and isinstance(embedded.get(list_key), list):
            embedded[list_key] = shareable_comments(embedded[list_key])
            shareable_ids = {str(entry.get("id")) for entry in embedded[list_key]}
        if isinstance(rendered, dict) and block_name in rendered:
            _redact_rendered_block(rendered, block_name, list_key, shareable_ids)
    return issue


def _redact_rendered_block(
    rendered: dict[str, Any], block_name: str, list_key: str, shareable_ids: set[str] | None
) -> None:
    block = rendered.get(block_name)
    if (
        shareable_ids is None
        or not isinstance(block, dict)
        or not isinstance(block.get(list_key), list)
    ):
        rendered.pop(block_name, None)
        return
    block[list_key] = [
        entry
        for entry in block[list_key]
        if isinstance(entry, dict) and str(entry.get("id")) in shareable_ids
    ]
