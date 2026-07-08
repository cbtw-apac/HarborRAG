from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from harborrag_adapters.connectors.attachments import AttachmentMetadata
from harborrag_adapters.parsers.utils import compact_text, html_to_text
from harborrag_core.domain.source import SourceRecord


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse JIRA timestamp strings into timezone-aware datetimes."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def issue_key_from_record(record: SourceRecord) -> str:
    """Recover the JIRA issue key from a source record."""
    issue_key = record.metadata.get("issue_key") or record.locator
    if not issue_key:
        raise ValueError(f"SourceRecord {record.id!r} does not contain issue_key")
    return str(issue_key).rstrip("/").rsplit("/", 1)[-1]


def issue_url(base_url: str, issue_key: str) -> str:
    """Build the user-facing browse URL for an issue."""
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return urljoin(base, f"browse/{issue_key}")


def build_source_record(issue: dict[str, Any], *, base_url: str) -> SourceRecord:
    """Convert a JIRA search result into a lightweight source record."""
    fields = issue.get("fields", {})
    issue_key = str(issue.get("key") or "")
    project = fields.get("project") or {}
    project_key = str(project.get("key") or issue_key.split("-")[0])

    return SourceRecord(
        id=f"jira://{project_key}/{issue_key}",
        source_type="application/vnd.atlassian.jira.issue+json",
        locator=issue_key,
        updated_at=parse_timestamp(fields.get("updated")),
        metadata={
            "issue_id": str(issue.get("id") or ""),
            "issue_key": issue_key,
            "project_key": project_key,
            "title": fields.get("summary"),
            "issue_type": _name(fields.get("issuetype")),
            "status": _name(fields.get("status")),
            "labels": list(fields.get("labels") or []),
            "url": issue_url(base_url, issue_key),
        },
    )


def build_raw_content(
    issue: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
    include_attachment_text: bool = True,
) -> str:
    """Render a JIRA issue and optional child data into text content."""
    fields = issue.get("fields", {})
    lines = [
        f"# {issue.get('key')} {fields.get('summary') or ''}".strip(),
        "",
        f"Type: {_name(fields.get('issuetype')) or ''}".strip(),
        f"Status: {_name(fields.get('status')) or ''}".strip(),
        f"Priority: {_name(fields.get('priority')) or ''}".strip(),
        "",
        "## Description",
        _field_text(fields.get("description")),
    ]

    if comments:
        lines.extend(["", "## Comments"])
        for comment in comments:
            author = _display_name(comment.get("author") or {})
            body = _field_text(comment.get("body") or comment.get("renderedBody"))
            lines.append(f"{author}: {body}".strip(": "))

    if attachments and include_attachment_text:
        processed = [item for item in attachments if item.text]
        if processed:
            lines.extend(["", "## Attachments"])
            for attachment in processed:
                lines.append(f"### {attachment.title}")
                lines.append(attachment.text or "")

    return compact_text("\n".join(lines))


def build_document_metadata(
    issue: dict[str, Any],
    *,
    base_url: str,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
    changelog: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build parsed provenance metadata for a loaded JIRA issue."""
    fields = issue.get("fields", {})
    issue_key = str(issue.get("key") or "")
    project = fields.get("project") or {}
    content = build_raw_content(
        issue,
        comments=comments,
        attachments=attachments,
        include_attachment_text=True,
    )

    return {
        "source_system": "jira",
        "issue_id": str(issue.get("id") or ""),
        "issue_key": issue_key,
        "title": fields.get("summary"),
        "project_key": project.get("key"),
        "project_name": project.get("name"),
        "issue_type": _name(fields.get("issuetype")),
        "status": _name(fields.get("status")),
        "status_category": _status_category(fields.get("status")),
        "priority": _name(fields.get("priority")),
        "assignee": _display_name(fields.get("assignee")),
        "reporter": _display_name(fields.get("reporter")),
        "creator": _display_name(fields.get("creator")),
        "labels": list(fields.get("labels") or []),
        "components": [_name(item) for item in fields.get("components") or []],
        "fix_versions": [_name(item) for item in fields.get("fixVersions") or []],
        "affected_versions": [_name(item) for item in fields.get("versions") or []],
        "created_at": parse_timestamp(fields.get("created")),
        "updated_at": parse_timestamp(fields.get("updated")),
        "resolved_at": parse_timestamp(fields.get("resolutiondate")),
        "due_date": fields.get("duedate"),
        "url": issue_url(base_url, issue_key),
        "checksum": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "parent": _parent(fields.get("parent")),
        "subtasks": [_issue_ref(item) for item in fields.get("subtasks") or []],
        "issue_links": [_issue_link(link) for link in fields.get("issuelinks") or []],
        "comments": [_comment_metadata(comment) for comment in comments or []],
        "attachments": [asdict(attachment) for attachment in attachments or []],
        "attachments_summary": _attachment_summary(attachments or []),
        "changelog": changelog or [],
    }


def changelog_histories(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize both Cloud and Data Center changelog response shapes."""
    histories = response.get("histories") or response.get("values") or []
    return [
        {
            "id": history.get("id"),
            "author": _display_name(history.get("author")),
            "created_at": history.get("created"),
            "items": [
                {
                    "field": item.get("field"),
                    "from": item.get("fromString"),
                    "to": item.get("toString"),
                }
                for item in history.get("items", [])
            ],
        }
        for history in histories
    ]


def _field_text(value: Any) -> str:
    """Extract readable text from JIRA strings, HTML, ADF, or lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return html_to_text(value) if "<" in value and ">" in value else compact_text(value)
    if isinstance(value, dict):
        return compact_text("".join(_walk_adf(value)))
    if isinstance(value, list):
        return compact_text("\n".join(_field_text(item) for item in value))
    return compact_text(str(value))


def _walk_adf(node: Any) -> list[str]:
    """Walk Atlassian Document Format nodes into plain-text fragments."""
    if isinstance(node, str):
        return [node]
    if isinstance(node, list):
        parts: list[str] = []
        for child in node:
            parts.extend(_walk_adf(child))
        return parts
    if not isinstance(node, dict):
        return []

    node_type = node.get("type")
    if node_type == "text":
        return [str(node.get("text") or "")]
    if node_type == "hardBreak":
        return ["\n"]

    parts: list[str] = []
    for child in node.get("content", []) or []:
        parts.extend(_walk_adf(child))
    if node_type in {"paragraph", "heading", "listItem"} and parts:
        parts.append("\n")
    return parts


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("name")
    return None


def _display_name(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return value.get("displayName") or value.get("name") or value.get("emailAddress")


def _status_category(status: Any) -> str | None:
    if not isinstance(status, dict):
        return None
    return _name(status.get("statusCategory"))


def _parent(parent: Any) -> dict[str, Any] | None:
    if not isinstance(parent, dict):
        return None
    return _issue_ref(parent)


def _issue_ref(issue: Any) -> dict[str, Any]:
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    return {
        "id": issue.get("id") if isinstance(issue, dict) else None,
        "key": issue.get("key") if isinstance(issue, dict) else None,
        "summary": fields.get("summary"),
        "status": _name(fields.get("status")),
        "issue_type": _name(fields.get("issuetype")),
    }


def _issue_link(link: dict[str, Any]) -> dict[str, Any]:
    linked_issue = link.get("outwardIssue") or link.get("inwardIssue") or {}
    direction = "outward" if link.get("outwardIssue") else "inward"
    return {
        "id": link.get("id"),
        "type": _name(link.get("type")),
        "direction": direction,
        "issue": _issue_ref(linked_issue),
    }


def _comment_metadata(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": comment.get("id"),
        "author": _display_name(comment.get("author")),
        "created_at": comment.get("created"),
        "updated_at": comment.get("updated"),
        "body": _field_text(comment.get("body") or comment.get("renderedBody")),
    }


def _attachment_summary(attachments: list[AttachmentMetadata]) -> dict[str, int]:
    return {
        status: sum(1 for attachment in attachments if attachment.status == status)
        for status in ("processed", "skipped", "unsupported", "failed")
    }
