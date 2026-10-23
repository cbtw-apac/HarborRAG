"""Jira comment and worklog visibility filtering.

Handles:
- Detecting role-restricted comments and Service Management internal notes
- Dropping restricted entries from embedded issue pages and ``renderedFields``
- Narrowing embedded comments to a verified list

An issue is one document with one access decision, so entries narrower than the
issue are dropped, never merged into its text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

INTERNAL_NOTE_PROPERTY = "sd.public.comment"


def is_restricted_comment(comment: Mapping[str, Any]) -> bool:
    """Return whether Jira shows this comment to fewer readers than its issue.

    Fails closed: any non-null ``visibility``, ``jsdPublic: false``, or an
    ``sd.public.comment`` property other than an explicit ``internal: false`` counts
    as restricted.

    Args:
        comment: Comment payload, with ``properties`` expanded on Data Center.

    Returns:
        ``True`` when the comment must be withheld.
    """

    if comment.get("visibility") is not None:
        return True
    if comment.get("jsdPublic") is False:
        return True
    return _is_internal_note(comment.get("properties"))


def _is_internal_note(properties: object) -> bool:
    """Return whether the first ``sd.public.comment`` entry marks the comment internal.

    Anything but an explicit ``internal: false`` counts as internal.
    """

    if not isinstance(properties, list):
        return False
    for entry in properties:
        if isinstance(entry, Mapping) and entry.get("key") == INTERNAL_NOTE_PROPERTY:
            value = entry.get("value")
            return not (isinstance(value, Mapping) and value.get("internal") is False)
    return False


def shareable_comments(comments: Iterable[object]) -> list[dict[str, Any]]:
    """Return the comments every reader of the issue may see, in source order."""

    return [
        comment
        for comment in comments
        if isinstance(comment, dict) and not is_restricted_comment(comment)
    ]


# Embedded blocks whose entries carry their own ``visibility``: (field, list key).
_RESTRICTABLE_BLOCKS = (("comment", "comments"), ("worklog", "worklogs"))


def redact_restricted_comments(issue: dict[str, Any]) -> dict[str, Any]:
    """Drop restricted comments and worklogs from an issue's embedded pages, in place.

    ``renderedFields`` entries are kept only when their id survived the ``fields``
    filter; without a filtered list the rendered block is dropped.

    Args:
        issue: Issue payload, modified in place.

    Returns:
        The same ``issue``.
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
    """Narrow an issue's embedded comments to the verified ones, in place.

    Embedded comments carry no properties, so a Data Center internal note looks
    public there; only ids present in ``verified`` survive.

    Args:
        issue: Issue payload, modified in place.
        verified: Comments already read with their properties and filtered.

    Returns:
        The same ``issue``.
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
    """Keep rendered entries with shareable ids; drop the block if ``shareable_ids`` is ``None``."""

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
