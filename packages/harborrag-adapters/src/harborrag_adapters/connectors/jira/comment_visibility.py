"""Jira comment visibility: only comments every issue reader can see are shared.

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


def redact_restricted_comments(issue: dict[str, Any]) -> dict[str, Any]:
    """Drop restricted comments from an issue's embedded comment pages, in place.

    ``get_issue`` expands ``renderedFields``, whose ``comment`` block repeats every
    comment as rendered HTML, restricted ones included, and may omit
    ``visibility``. Rendered entries are kept only when their id survived the
    ``fields.comment`` filter; without that list the rendered block is dropped.
    """

    fields = issue.get("fields")
    embedded = fields.get("comment") if isinstance(fields, dict) else None
    shareable_ids: set[str] | None = None
    if isinstance(embedded, dict) and isinstance(embedded.get("comments"), list):
        embedded["comments"] = shareable_comments(embedded["comments"])
        shareable_ids = {str(comment.get("id")) for comment in embedded["comments"]}
    rendered = issue.get("renderedFields")
    if isinstance(rendered, dict) and "comment" in rendered:
        _redact_rendered_comments(rendered, shareable_ids)
    return issue


def _redact_rendered_comments(rendered: dict[str, Any], shareable_ids: set[str] | None) -> None:
    block = rendered.get("comment")
    if (
        shareable_ids is None
        or not isinstance(block, dict)
        or not isinstance(block.get("comments"), list)
    ):
        rendered.pop("comment", None)
        return
    block["comments"] = [
        comment
        for comment in block["comments"]
        if isinstance(comment, dict) and str(comment.get("id")) in shareable_ids
    ]
