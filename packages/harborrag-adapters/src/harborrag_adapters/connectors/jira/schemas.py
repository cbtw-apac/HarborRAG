"""Structured metadata emitted by the JIRA connector."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from harborrag_adapters.connectors.attachments.processing import AttachmentMetadata
from harborrag_adapters.connectors.schemas import ConnectorMetadata


@dataclass(slots=True)
class JiraIssueReference:
    """Compact issue reference used for relationships."""

    id: Any
    key: Any
    summary: Any
    status: str | None
    issue_type: str | None


@dataclass(slots=True)
class JiraIssueLinkMetadata:
    """Normalized linked issue relationship."""

    id: Any
    type: str | None
    direction: str
    issue: JiraIssueReference


@dataclass(slots=True)
class JiraCommentMetadata:
    """Normalized JIRA comment metadata."""

    id: Any
    author: str | None
    created_at: Any
    updated_at: Any
    body: str


@dataclass(slots=True)
class JiraChangelogItemMetadata:
    """One changed field inside a changelog entry."""

    field: Any
    from_value: Any
    to_value: Any


@dataclass(slots=True)
class JiraChangelogMetadata:
    """Normalized JIRA changelog entry."""

    id: Any
    author: str | None
    created_at: Any
    items: list[JiraChangelogItemMetadata]


@dataclass(slots=True)
class JiraCustomFieldMetadata:
    """Custom field value preserved with JIRA field metadata."""

    field_id: str
    name: str
    schema_type: Any
    custom_type: Any
    value: Any
    text: str


@dataclass(slots=True, kw_only=True)
class JiraMetadata(ConnectorMetadata):
    """Structured metadata for one loaded JIRA issue."""

    source_system: ClassVar[str] = "jira"

    issue_id: str
    issue_key: str
    project_key: Any
    project_name: Any
    issue_type: str | None
    status: str | None
    status_category: str | None
    priority: str | None
    assignee: str | None
    reporter: str | None
    creator: str | None
    labels: list[Any]
    components: list[str | None]
    fix_versions: list[str | None]
    affected_versions: list[str | None]
    resolved_at: datetime | None
    due_date: Any
    parent: JiraIssueReference | None
    subtasks: list[JiraIssueReference]
    issue_links: list[JiraIssueLinkMetadata]
    comments: list[JiraCommentMetadata]
    attachments: list[AttachmentMetadata]
    changelog: list[JiraChangelogMetadata]
    custom_fields: list[JiraCustomFieldMetadata]
