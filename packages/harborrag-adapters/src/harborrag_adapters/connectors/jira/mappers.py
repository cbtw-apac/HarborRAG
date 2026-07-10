"""Mappings from JIRA API payloads to Harbor domain objects."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from harborrag_core.domain.source import SourceRecord

from harborrag_adapters.connectors.shared.attachments import AttachmentMetadata

from .content import custom_field_metadata, field_text
from .schemas import (
    JiraChangelogItemMetadata,
    JiraChangelogMetadata,
    JiraCommentMetadata,
    JiraIssueLinkMetadata,
    JiraIssueReference,
    JiraMetadata,
)
from .utils import validate_issue_key


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
    return validate_issue_key(str(issue_key))


def issue_url(base_url: str, issue_key: str) -> str:
    """Build the user-facing browse URL for an issue."""
    issue_key = validate_issue_key(issue_key)
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
        body=field_text(comment.get("body") or comment.get("renderedBody")),
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
