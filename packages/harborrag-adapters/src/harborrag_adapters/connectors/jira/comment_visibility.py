"""Jira comment visibility: only comments (and worklogs) every issue reader can see are shared.

An issue becomes one document with one access decision. A comment Jira limits to
a project role or group (``visibility``), or a Jira Service Management internal
note, is narrower than its issue, so it is dropped rather than merged into text
that every reader of the issue would receive. Cloud marks an internal note with
``jsdPublic: false``; Data Center only with the ``sd.public.comment`` comment
property, which the comment endpoint returns when asked to expand ``properties``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

INTERNAL_NOTE_PROPERTY = "sd.public.comment"


def is_restricted_comment(comment: Mapping[str, Any]) -> bool:
    """Return whether Jira shows this comment to fewer readers than its issue.

    Any non-null ``visibility`` counts, including an empty or malformed value:
    Jira only sends the key for restricted comments, so an unexpected shape is
    treated as restricted rather than guessed to be public. The same holds for
    an ``sd.public.comment`` property: only an explicit ``internal: false``
    reads as public.
    """

    if comment.get("visibility") is not None:
        return True
    if comment.get("jsdPublic") is False:
        return True
    return _is_internal_note(comment.get("properties"))


def _is_internal_note(properties: object) -> bool:
    if not isinstance(properties, list):
        return False
    for entry in properties:
        if isinstance(entry, Mapping) and entry.get("key") == INTERNAL_NOTE_PROPERTY:
            value = entry.get("value")
            return not (isinstance(value, Mapping) and value.get("internal") is False)
    return False


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


def keep_verified_comments(
    issue: dict[str, Any], verified: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Narrow an issue's embedded comments to the ones ``verified`` holds, in place.

    Embedded comments never carry comment properties, so on Data Center an
    internal note looks public there. ``fetch_comments`` reads comments with
    their properties; only the ids it kept survive in the issue payload.
    """

    verified_ids = {str(comment.get("id")) for comment in verified}
    fields = issue.get("fields")
    embedded = fields.get("comment") if isinstance(fields, dict) else None
    if isinstance(embedded, dict) and isinstance(embedded.get("comments"), list):
        embedded["comments"] = [
            comment
            for comment in embedded["comments"]
            if isinstance(comment, dict) and str(comment.get("id")) in verified_ids
        ]
    rendered = issue.get("renderedFields")
    if isinstance(rendered, dict) and "comment" in rendered:
        _redact_rendered_block(rendered, "comment", "comments", verified_ids)
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
