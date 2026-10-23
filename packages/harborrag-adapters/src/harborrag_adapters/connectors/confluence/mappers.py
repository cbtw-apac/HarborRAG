"""Mappings from Confluence API payloads to Harbor domain objects.

Handles:
- Content validation against the configured space
- Source records and loaded-document metadata
- Display URLs for Cloud and Data Center
- Body, label, comment and hierarchy extraction
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.domain.source import SourceRecord

from .config import ConfluenceDeploymentType
from .query import validate_content_id
from .schemas import (
    AttachmentMetadata,
    ConfluenceCommentMetadata,
    ConfluenceHierarchyMetadata,
    ConfluenceMetadata,
    ConfluencePageReference,
)


def validate_content(content: dict[str, Any], content_id: str, *, space_key: str) -> None:
    """Fail fast when content is malformed or outside the configured space.

    Shared by discovery, load and describe so all agree on what counts as valid, in-scope content.

    Args:
        content: Content payload.
        content_id: ID used in error messages.
        space_key: Configured space key.

    Raises:
        DocumentProcessingError: If ``id``, ``title`` or ``space.key`` is missing, or the space
            differs from ``space_key``.
    """
    content_space_key = content.get("space", {}).get("key")
    missing = [
        name
        for name, value in (
            ("id", content.get("id")),
            ("title", content.get("title")),
            ("space.key", content_space_key),
        )
        if not value
    ]
    if missing:
        raise DocumentProcessingError(
            f"Confluence content {content_id} missing required fields: {', '.join(missing)}"
        )
    if str(content_space_key) != space_key:
        raise DocumentProcessingError(
            f"Confluence content {content_id} belongs to space {content_space_key!r}, "
            f"outside configured space {space_key!r}"
        )


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO timestamp (``Z`` allowed) into a datetime, or None if blank or invalid."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def labels_from_content(content: dict[str, Any]) -> list[str]:
    """Extract label names from Confluence content metadata."""
    labels = content.get("metadata", {}).get("labels", {}).get("results", [])
    return [str(label.get("name")) for label in labels if label.get("name")]


def content_id_from_record(record: SourceRecord) -> str:
    """Return the content ID of a source record; raise ``ValueError`` if it has none."""
    content_id = record.metadata.get("content_id") or record.locator
    if not content_id:
        raise ValueError(f"SourceRecord {record.id!r} does not contain content_id")
    return validate_content_id(str(content_id))


def display_url(
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    space_key: str,
    content_id: str,
    title: str,
) -> str:
    """Build the user-facing URL of a page.

    Cloud uses ``spaces/<key>/pages/<id>``; Data Center uses ``display/<key>/<title>``.

    Args:
        base_url: Confluence base URL.
        deployment_type: Cloud or Data Center.
        space_key: Space the page belongs to.
        content_id: Page ID.
        title: Page title, used on Data Center.

    Returns:
        The page URL.
    """
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    if deployment_type == ConfluenceDeploymentType.CLOUD:
        return urljoin(base, f"spaces/{space_key}/pages/{content_id}")
    # TODO: percent-encode a literal ``+`` in the title instead of leaving it unencoded.
    return urljoin(base, f"display/{space_key}/{quote(title.replace(' ', '+'), safe='+')}")


def body_html_from_content(content: dict[str, Any]) -> str:
    """Return the storage body, else the rendered export view, else an empty string."""
    body = content.get("body", {})
    return body.get("storage", {}).get("value") or body.get("export_view", {}).get("value") or ""


def body_representation_from_content(content: dict[str, Any]) -> str | None:
    """Return ``storage`` or ``rendered_html`` for the selected body, else None."""

    body = content.get("body", {})
    if body.get("storage", {}).get("value"):
        return "storage"
    if body.get("export_view", {}).get("value"):
        return "rendered_html"
    return None


def build_source_record(
    content: dict[str, Any],
    *,
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    default_space_key: str,
) -> SourceRecord:
    """Convert a Confluence search result into a lightweight source record.

    Args:
        content: Search result payload.
        base_url: Confluence base URL.
        deployment_type: Cloud or Data Center.
        default_space_key: Space used when the payload names none.

    Returns:
        Record with ``confluence://<space>/<id>`` as its id and the page ID as locator.
    """
    content_id = str(content.get("id") or "")
    content_type = str(content.get("type") or "page")
    title = str(content.get("title") or content_id)
    space_key = str(content.get("space", {}).get("key") or default_space_key)
    labels = labels_from_content(content)
    updated_at = parse_timestamp(content.get("version", {}).get("when"))
    url = display_url(base_url, deployment_type, space_key, content_id, title)

    return SourceRecord(
        id=f"confluence://{space_key}/{content_id}",
        source_type="text/html",
        locator=content_id,
        updated_at=updated_at,
        metadata={
            "content_id": content_id,
            "content_type": content_type,
            "title": title,
            "space_key": space_key,
            "labels": labels,
            "url": url,
        },
    )


def build_document_metadata(
    content: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
) -> ConfluenceMetadata:
    """Build parsed provenance metadata for a loaded Confluence page.

    Args:
        content: Fully expanded content payload.
        comments: Comment payloads to include.
        attachments: Processed attachment metadata to include.

    Returns:
        Page metadata with checksum, hierarchy, comments and attachments.
    """
    content_id = str(content.get("id") or "")
    content_type = str(content.get("type") or "page")
    title = str(content.get("title") or "")
    space_key = str(content.get("space", {}).get("key") or "")
    version = content.get("version", {})
    history = content.get("history", {})
    body_html = body_html_from_content(content)
    attachment_values = attachments or []
    comment_values = comments or []
    hierarchy = _hierarchy_metadata(content)

    checksum = hashlib.sha256(
        f"{content_id}:{version.get('number')}:{body_html}".encode()
    ).hexdigest()

    return ConfluenceMetadata(
        record_id=content_id,
        title=title,
        checksum=checksum,
        created_at=parse_timestamp(history.get("createdDate") or history.get("createdAt")),
        updated_at=parse_timestamp(version.get("when")),
        content_id=content_id,
        content_type=content_type,
        space_key=space_key,
        version=version.get("number"),
        author=_author(content),
        labels=labels_from_content(content),
        comments=[_comment_metadata(comment) for comment in comment_values],
        attachments=attachment_values,
        ancestors=hierarchy.ancestors,
        children=hierarchy.children,
        depth=hierarchy.depth,
        breadcrumb=hierarchy.breadcrumb,
        body_representation=body_representation_from_content(content),
    )


def _author(content: dict[str, Any]) -> str | None:
    """Return the creator's display name, falling back to the last editor's."""
    value = content.get("history", {}).get("createdBy", {}).get("displayName") or content.get(
        "version", {}
    ).get("by", {}).get("displayName")
    return str(value) if value else None


def _comment_metadata(comment: dict[str, Any]) -> ConfluenceCommentMetadata:
    """Map a comment payload to metadata; location ``inline`` marks an inline comment."""
    history = comment.get("history", {})
    version = comment.get("version") or {}
    extensions = comment.get("extensions") or {}
    location = str(extensions.get("location") or "").casefold()
    return ConfluenceCommentMetadata(
        id=comment.get("id"),
        body=comment.get("body", {}).get("storage", {}).get("value", ""),
        author=history.get("createdBy", {}).get("displayName"),
        created_at=history.get("createdDate") or history.get("createdAt"),
        updated_at=(version.get("when") or (history.get("lastUpdated") or {}).get("when")),
        comment_kind=("INLINE_COMMENT" if location == "inline" else "PAGE_COMMENT"),
        parent_comment_id=_parent_comment_id(comment),
        status=(str(comment["status"]) if comment.get("status") is not None else None),
    )


def _parent_comment_id(comment: dict[str, Any]) -> str | None:
    """Return the parent comment id from ``parentId``, else the nearest ``comment`` ancestor."""
    direct = comment.get("parentId") or comment.get("parent_id")
    if direct is not None and str(direct).strip():
        return str(direct)
    ancestors = comment.get("ancestors")
    if not isinstance(ancestors, list):
        return None
    for ancestor in reversed(ancestors):
        if not isinstance(ancestor, dict):
            continue
        if str(ancestor.get("type") or "").casefold() != "comment":
            continue
        identifier = ancestor.get("id")
        if identifier is not None and str(identifier).strip():
            return str(identifier)
    return None


def _hierarchy_metadata(content: dict[str, Any]) -> ConfluenceHierarchyMetadata:
    """Build ancestor, child and breadcrumb metadata; children carry ids only."""
    # Ancestors keep their titles: a restricted ancestor already restricts this page.
    ancestors = [
        ConfluencePageReference(
            id=item.get("id"),
            title=item.get("title"),
            type=item.get("type", "page"),
        )
        for item in content.get("ancestors", [])
    ]
    child_results = content.get("children", {}).get("page", {}).get("results", [])
    # Children carry no title: a child page may be restricted below this one, and
    # its title would otherwise reach every reader of the parent.
    children = [
        ConfluencePageReference(id=item.get("id"), title=None, type=item.get("type", "page"))
        for item in child_results
    ]
    breadcrumb = [str(item.title) for item in ancestors if item.title]
    return ConfluenceHierarchyMetadata(
        ancestors=ancestors,
        children=children,
        depth=len(ancestors),
        breadcrumb=breadcrumb,
    )
