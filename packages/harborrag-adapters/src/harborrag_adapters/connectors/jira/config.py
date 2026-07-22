"""Validated configuration for JIRA project connectors."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from harborrag_adapters.connectors.shared.attachments import (
    DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
    CustomAttachmentParser,
    FileType,
)
from harborrag_adapters.connectors.utils.helpers import (
    DEFAULT_MAX_NESTED_ITEMS,
    validate_http_tuning,
    validate_https_url,
    validate_non_negative_limit,
)

from .utils import is_cloud_hostname


class JiraDeploymentType(StrEnum):
    """Supported JIRA auth/API deployment modes."""

    CLOUD = "cloud"
    DATACENTER = "datacenter"


DEFAULT_ISSUE_FIELDS = (
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "creator",
    "labels",
    "project",
    "parent",
    "components",
    "fixVersions",
    "versions",
    "created",
    "updated",
    "resolutiondate",
    "duedate",
    "attachment",
    "issuelinks",
    "subtasks",
)


@dataclass(slots=True)
class JiraProjectConfig:
    """Configuration for one JIRA source scope.

    The config collects auth, JQL defaults, nested issue-data limits, and
    attachment parsing controls so the connector can stay mostly procedural.
    """

    base_url: str
    token: str | None = field(default=None, repr=False)  # secret: keep out of repr/logs
    email: str | None = None
    deployment_type: JiraDeploymentType | str | None = None
    project_keys: list[str] = field(default_factory=list)
    issue_types: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    include_comments: bool = True
    include_attachments: bool = False
    include_changelog: bool = False
    include_attachment_text_in_content: bool = True
    include_all_fields: bool = True
    fields: tuple[str, ...] = DEFAULT_ISSUE_FIELDS
    custom_parsers: dict[FileType, CustomAttachmentParser] = field(default_factory=dict)
    process_attachment_callback: Callable[[str, int, str], tuple[bool, str]] | None = None
    max_attachment_size_bytes: int | None = DEFAULT_MAX_ATTACHMENT_SIZE_BYTES
    max_comments: int | None = DEFAULT_MAX_NESTED_ITEMS
    max_attachments: int | None = DEFAULT_MAX_NESTED_ITEMS
    max_changelog_items: int | None = DEFAULT_MAX_NESTED_ITEMS
    fail_on_error: bool = False
    requests_per_minute: int = 60
    page_size: int = 50
    request_timeout_seconds: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 0.5

    def __post_init__(self) -> None:
        """Normalize env-backed credentials and validate query/load limits."""
        self.base_url = str(self.base_url).rstrip("/")
        validate_https_url("base_url", self.base_url)
        self.token = self.token or os.getenv("JIRA_TOKEN") or os.getenv("JIRA_API_TOKEN")
        self.email = self.email or os.getenv("JIRA_EMAIL")
        self.project_keys = self._string_list(self.project_keys)
        self.issue_types = self._string_list(self.issue_types)
        self.statuses = self._string_list(self.statuses)
        self.labels = self._string_list(self.labels)
        self.fields = tuple(
            dict.fromkeys(str(field_name) for field_name in self.fields if field_name)
        )

        if self.deployment_type is None:
            self.deployment_type = (
                JiraDeploymentType.CLOUD
                if is_cloud_hostname(self.base_url)
                else JiraDeploymentType.DATACENTER
            )
        elif isinstance(self.deployment_type, str):
            self.deployment_type = JiraDeploymentType(self.deployment_type.lower())

        if self.deployment_type == JiraDeploymentType.CLOUD and not self.email:
            raise ValueError("email is required for JIRA Cloud API token auth")
        if not self.token:
            raise ValueError("token is required for JIRA")
        validate_non_negative_limit(
            "max_attachment_size_bytes",
            self.max_attachment_size_bytes,
        )
        validate_non_negative_limit("max_comments", self.max_comments)
        validate_non_negative_limit("max_attachments", self.max_attachments)
        validate_non_negative_limit("max_changelog_items", self.max_changelog_items)
        validate_http_tuning(
            requests_per_minute=self.requests_per_minute,
            request_timeout_seconds=self.request_timeout_seconds,
            max_retries=self.max_retries,
            backoff_factor=self.backoff_factor,
        )
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

    def requested_fields(self) -> tuple[str, ...]:
        """Return the JIRA field selector for search and issue loads."""
        if self.include_all_fields:
            return ("*all",)
        return self.fields

    @staticmethod
    def _string_list(value: list[str] | str) -> list[str]:
        """Accept lists from Python/YAML and comma-separated dotenv values."""

        values = value.split(",") if isinstance(value, str) else value
        return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))
