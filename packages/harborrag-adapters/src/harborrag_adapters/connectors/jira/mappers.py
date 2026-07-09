from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from harborrag_adapters.connectors.attachments import AttachmentMetadata
from harborrag_adapters.parsers.utils import compact_text, html_to_text
from harborrag_core.domain.source import SourceRecord

from .schemas import (
    JiraChangelogItemMetadata,
    JiraChangelogMetadata,
    JiraCommentMetadata,
    JiraCustomFieldMetadata,
    JiraIssueLinkMetadata,
    JiraIssueReference,
    JiraMetadata,
)


CUSTOM_FIELD_PREFIX = "customfield_"


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

    custom_fields = custom_field_metadata(issue)
    custom_field_lines = [
        f"{field.name}: {field.text}".strip()
        for field in custom_fields
        if field.text
    ]
    if custom_field_lines:
        lines.extend(["", "## Custom Fields", *custom_field_lines])

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
    content: str,
    comments: list[dict[str, Any]] | None = None,
    attachments: list[AttachmentMetadata] | None = None,
    changelog: list[dict[str, Any]] | None = None,
) -> JiraMetadata:
    """Build parsed provenance metadata for a loaded JIRA issue."""
    fields = issue.get("fields", {})
    issue_key = str(issue.get("key") or "")
    project = fields.get("project") or {}

    return JiraMetadata(
        source_system="jira",
        issue_id=str(issue.get("id") or ""),
        issue_key=issue_key,
        title=fields.get("summary"),
        project_key=project.get("key"),
        project_name=project.get("name"),
        issue_type=_name(fields.get("issuetype")),
        status=_name(fields.get("status")),
        status_category=_status_category(fields.get("status")),
        priority=_name(fields.get("priority")),
        assignee=_display_name(fields.get("assignee")),
        reporter=_display_name(fields.get("reporter")),
        creator=_display_name(fields.get("creator")),
        labels=list(fields.get("labels") or []),
        components=[_name(item) for item in fields.get("components") or []],
        fix_versions=[_name(item) for item in fields.get("fixVersions") or []],
        affected_versions=[_name(item) for item in fields.get("versions") or []],
        created_at=parse_timestamp(fields.get("created")),
        updated_at=parse_timestamp(fields.get("updated")),
        resolved_at=parse_timestamp(fields.get("resolutiondate")),
        due_date=fields.get("duedate"),
        checksum=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        parent=_parent(fields.get("parent")),
        subtasks=[_issue_ref(item) for item in fields.get("subtasks") or []],
        issue_links=[_issue_link(link) for link in fields.get("issuelinks") or []],
        comments=[_comment_metadata(comment) for comment in comments or []],
        attachments=attachments or [],
        changelog=[_changelog_metadata(history) for history in changelog or []],
        custom_fields=custom_field_metadata(issue),
    )


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


def custom_field_metadata(issue: dict[str, Any]) -> list[JiraCustomFieldMetadata]:
    """Return all custom fields present on an issue with names and rendered text."""
    fields = issue.get("fields", {})
    names = issue.get("names", {}) or {}
    schemas = issue.get("schema", {}) or {}
    rendered_fields = issue.get("renderedFields", {}) or {}

    values: list[JiraCustomFieldMetadata] = []
    for field_id, value in fields.items():
        if not str(field_id).startswith(CUSTOM_FIELD_PREFIX):
            continue
        schema = schemas.get(field_id) or {}
        rendered_value = rendered_fields.get(field_id)
        text_source = rendered_value if rendered_value not in (None, "") else value
        values.append(
            JiraCustomFieldMetadata(
                field_id=str(field_id),
                name=str(names.get(field_id) or field_id),
                schema_type=schema.get("type") if isinstance(schema, dict) else None,
                custom_type=schema.get("custom") if isinstance(schema, dict) else None,
                value=value,
                text=_field_text(text_source),
            )
        )
    return values


def _field_text(value: Any) -> str:
    """Extract readable text from JIRA strings, HTML, ADF, or lists."""
    if value is None:
        return ""
    if isinstance(value, str):
        return (
            html_to_text(value)
            if "<" in value and ">" in value
            else compact_text(value)
        )
    if isinstance(value, dict):
        adf_text = compact_text("".join(_walk_adf(value)))
        if adf_text:
            return adf_text
        for key in ("displayName", "name", "value", "key", "emailAddress"):
            if value.get(key):
                return compact_text(str(value[key]))
        parts = [_field_text(item) for item in value.values()]
        return compact_text("\n".join(part for part in parts if part))
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


def _parent(parent: Any) -> JiraIssueReference | None:
    if not isinstance(parent, dict):
        return None
    return _issue_ref(parent)


def _issue_ref(issue: Any) -> JiraIssueReference:
    fields = issue.get("fields", {}) if isinstance(issue, dict) else {}
    return JiraIssueReference(
        id=issue.get("id") if isinstance(issue, dict) else None,
        key=issue.get("key") if isinstance(issue, dict) else None,
        summary=fields.get("summary"),
        status=_name(fields.get("status")),
        issue_type=_name(fields.get("issuetype")),
    )


def _issue_link(link: dict[str, Any]) -> JiraIssueLinkMetadata:
    linked_issue = link.get("outwardIssue") or link.get("inwardIssue") or {}
    direction = "outward" if link.get("outwardIssue") else "inward"
    return JiraIssueLinkMetadata(
        id=link.get("id"),
        type=_name(link.get("type")),
        direction=direction,
        issue=_issue_ref(linked_issue),
    )


def _comment_metadata(comment: dict[str, Any]) -> JiraCommentMetadata:
    return JiraCommentMetadata(
        id=comment.get("id"),
        author=_display_name(comment.get("author")),
        created_at=comment.get("created"),
        updated_at=comment.get("updated"),
        body=_field_text(comment.get("body") or comment.get("renderedBody")),
    )


def _changelog_metadata(history: dict[str, Any]) -> JiraChangelogMetadata:
    return JiraChangelogMetadata(
        id=history.get("id"),
        author=history.get("author"),
        created_at=history.get("created_at"),
        items=[
            JiraChangelogItemMetadata(
                field=item.get("field"),
                from_value=item.get("from"),
                to_value=item.get("to"),
            )
            for item in history.get("items", [])
        ],
    )
