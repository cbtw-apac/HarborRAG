from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

from .config import ConfluenceDeploymentType


def extract_hierarchy_info(content: dict[str, Any]) -> dict[str, Any]:
    hierarchy_info = {
        "ancestors": [],
        "parent_id": None,
        "parent_title": None,
        "children": [],
        "depth": 0,
        "breadcrumb": [],
    }

    ancestors = content.get("ancestors", [])
    if ancestors:
        ancestor_chain = []
        breadcrumb = []
        for ancestor in ancestors:
            ancestor_info = {
                "id": ancestor.get("id"),
                "title": ancestor.get("title"),
                "type": ancestor.get("type", "page"),
            }
            ancestor_chain.append(ancestor_info)
            breadcrumb.append(ancestor.get("title", "Unknown"))
        hierarchy_info["ancestors"] = ancestor_chain
        hierarchy_info["breadcrumb"] = breadcrumb
        hierarchy_info["depth"] = len(ancestor_chain)
        if ancestor_chain:
            immediate_parent = ancestor_chain[-1]
            hierarchy_info["parent_id"] = immediate_parent["id"]
            hierarchy_info["parent_title"] = immediate_parent["title"]

    children_data = content.get("children", {})
    if "page" in children_data:
        child_pages = children_data["page"].get("results", [])
        children_info = []
        for child in child_pages:
            children_info.append(
                {
                    "id": child.get("id"),
                    "title": child.get("title"),
                    "type": child.get("type", "page"),
                }
            )
        hierarchy_info["children"] = children_info

    return hierarchy_info


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse a Confluence timestamp string into a timezone-aware datetime.

    Confluence emits ISO 8601 timestamps in a couple of shapes (with a `Z`
    suffix or an explicit `+HH:MM` offset, with or without microseconds).
    `datetime.fromisoformat` handles all of them once `Z` is normalized to
    `+00:00`.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def construct_canonical_page_url(base_url: str, space: str, content_id: str, content_type: str) -> str:
    """Build a stable, ID-based URL that works for both Cloud and Data Center."""
    base = base_url if base_url.endswith("/") else base_url + "/"
    segment = "blog" if content_type == "blogpost" else "pages"
    return urljoin(base, f"spaces/{space}/{segment}/{content_id}")


def construct_display_page_url(
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    space: str,
    content_id: str,
    title: str,
) -> str:
    """Build a human-readable URL for browsing (title-based on Data Center)."""
    base = base_url if base_url.endswith("/") else base_url + "/"
    if deployment_type == ConfluenceDeploymentType.CLOUD:
        return urljoin(base, f"spaces/{space}/pages/{content_id}")
    encoded_title = quote(title.replace(" ", "+"), safe="+")
    return urljoin(base, f"display/{space}/{encoded_title}")


def build_document_metadata(
    content: dict[str, Any],
    *,
    base_url: str,
    deployment_type: ConfluenceDeploymentType,
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Assemble the full metadata dict for one Confluence content item.

    Centralizes everything a downstream parser/normalizer or a future
    `ConfluenceGraphMapper` needs: identity, authorship, timestamps, labels,
    comments, and page hierarchy (ancestors/parent/children/breadcrumb) for
    building graph edges without re-fetching from Confluence.

    `comments` should come from a dedicated depth=all fetch (see
    connector._fetch_comments), not from a `children.comment` expand on the
    content endpoint -- that expand only returns one level and silently
    drops nested replies. Pass `None`/`[]` if include_comments is disabled.
    """
    content_id = content.get("id")
    title = content.get("title")
    space = content.get("space", {}).get("key")
    content_type = content.get("type", "page")

    version = content.get("version", {})
    version_number = version.get("number", 1) if isinstance(version, dict) else 1

    author = content.get("history", {}).get("createdBy", {}).get("displayName")
    if not author:
        author = content.get("version", {}).get("by", {}).get("displayName")

    history = content.get("history", {})
    created_at = parse_timestamp(history.get("createdDate") or history.get("createdAt"))
    updated_at = parse_timestamp(version.get("when") if isinstance(version, dict) else None)

    labels = [
        label["name"]
        for label in content.get("metadata", {}).get("labels", {}).get("results", [])
    ]

    comment_texts = [
        {
            "body": comment.get("body", {}).get("storage", {}).get("value", ""),
            "author": comment.get("history", {}).get("createdBy", {}).get("displayName", ""),
            "created_at": comment.get("history", {}).get("createdDate", ""),
        }
        for comment in (comments or [])
    ]

    body = content.get("body", {}).get("export_view", {}).get("value")
    hierarchy = extract_hierarchy_info(content)

    return {
        "source_system": "confluence",
        "content_id": content_id,
        "title": title,
        "space_key": space,
        "content_type": content_type,
        "version": version_number,
        "author": author,
        "created_at": created_at,
        "updated_at": updated_at,
        "labels": labels,
        "canonical_url": construct_canonical_page_url(base_url, space or "", content_id or "", content_type),
        "display_url": construct_display_page_url(
            base_url, deployment_type, space or "", content_id or "", title or ""
        ),
        "comments": comment_texts,
        "parent_id": hierarchy["parent_id"],
        "parent_title": hierarchy["parent_title"],
        "ancestors": hierarchy["ancestors"],
        "children": hierarchy["children"],
        "depth": hierarchy["depth"],
        "breadcrumb": hierarchy["breadcrumb"],
        "breadcrumb_text": " > ".join(hierarchy["breadcrumb"]) if hierarchy["breadcrumb"] else "",
        "body_missing": not bool(body),
    }