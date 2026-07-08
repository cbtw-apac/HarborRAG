from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

from harborrag_core.domain.source import SourceRecord

from .config import ConfluenceDeploymentType
from .schemas import AttachmentMetadata


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse Confluence timestamp strings into timezone-aware datetimes."""
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
    """Recover the Confluence content ID from a source record."""
    content_id = record.metadata.get("content_id") or record.locator
    if not content_id:
        raise ValueError(f"SourceRecord {record.id!r} does not contain content_id")
    return str(content_id).rstrip("/").rsplit("/", 1)[-1]


def canonical_url(base_url: str, space_key: str, content_id: str) -> str:
    """Build the canonical Cloud-style URL used as stable provenance."""
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return urljoin(base, f"spaces/{space_key}/pages/{content_id}")


def display_url(
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    space_key: str,
    content_id: str,
    title: str,
) -> str:
    """Build the user-facing Confluence URL for Cloud or Data Center."""
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    if deployment_type == ConfluenceDeploymentType.CLOUD:
        return urljoin(base, f"spaces/{space_key}/pages/{content_id}")
    return urljoin(base, f"display/{space_key}/{quote(title.replace(' ', '+'), safe='+')}")


def body_html_from_content(content: dict[str, Any]) -> str:
    """Extract the best HTML body representation from expanded content."""
    body = content.get("body", {})
    return (
        body.get("export_view", {}).get("value")
        or body.get("storage", {}).get("value")
        or ""
    )


def build_source_record(
    content: dict[str, Any],
    *,
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    default_space_key: str,
) -> SourceRecord:
    """Convert a Confluence search result into a lightweight source record."""
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
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
) -> dict[str, Any]:
    """Build parsed provenance metadata for a loaded Confluence page."""
    content_id = str(content.get("id") or "")
    content_type = str(content.get("type") or "page")
    title = str(content.get("title") or "")
    space_key = str(content.get("space", {}).get("key") or "")
    version = content.get("version", {})
    history = content.get("history", {})
    body_html = body_html_from_content(content)

    attachment_values = [asdict(attachment) for attachment in attachments or []]
    checksum = hashlib.sha256(
        f"{content_id}:{version.get('number')}:{body_html}".encode("utf-8")
    ).hexdigest()

    return {
        "source_system": "confluence",
        "content_id": content_id,
        "content_type": content_type,
        "title": title,
        "space_key": space_key,
        "version": version.get("number"),
        "author": _author(content),
        "created_at": parse_timestamp(
            history.get("createdDate") or history.get("createdAt")
        ),
        "updated_at": parse_timestamp(version.get("when")),
        "labels": labels_from_content(content),
        "canonical_url": canonical_url(base_url, space_key, content_id),
        "display_url": display_url(
            base_url,
            deployment_type,
            space_key,
            content_id,
            title,
        ),
        "checksum": checksum,
        "comments": [_comment_metadata(comment) for comment in comments or []],
        "attachments": attachment_values,
        "attachments_summary": _attachment_summary(attachments or []),
        **_hierarchy_metadata(content),
    }


def _author(content: dict[str, Any]) -> str | None:
    return (
        content.get("history", {}).get("createdBy", {}).get("displayName")
        or content.get("version", {}).get("by", {}).get("displayName")
    )


def _comment_metadata(comment: dict[str, Any]) -> dict[str, Any]:
    history = comment.get("history", {})
    return {
        "id": comment.get("id"),
        "body": comment.get("body", {}).get("storage", {}).get("value", ""),
        "author": history.get("createdBy", {}).get("displayName"),
        "created_at": history.get("createdDate") or history.get("createdAt"),
    }


def _hierarchy_metadata(content: dict[str, Any]) -> dict[str, Any]:
    ancestors = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("type", "page"),
        }
        for item in content.get("ancestors", [])
    ]
    children = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "type": item.get("type", "page"),
        }
        for item in content.get("children", {}).get("page", {}).get("results", [])
    ]
    breadcrumb = [str(item["title"]) for item in ancestors if item.get("title")]
    parent = ancestors[-1] if ancestors else {}
    return {
        "parent_id": parent.get("id"),
        "parent_title": parent.get("title"),
        "ancestors": ancestors,
        "children": children,
        "depth": len(ancestors),
        "breadcrumb": breadcrumb,
        "breadcrumb_text": " > ".join(breadcrumb),
    }


def _attachment_summary(attachments: list[AttachmentMetadata]) -> dict[str, int]:
    return {
        status: sum(1 for attachment in attachments if attachment.status == status)
        for status in ("processed", "skipped", "unsupported", "failed")
    }
