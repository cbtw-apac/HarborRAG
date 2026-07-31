"""JIRA issue search and nested-resource pagination."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.policies.validation import truncate_with_limit

from .client import JiraClient
from .config import JiraDeploymentType, JiraProjectConfig
from .mappers import changelog_histories
from .query import search_body, search_jql_body, validate_issue_key

DISCOVERY_FIELDS = (
    "summary",
    "issuetype",
    "status",
    "labels",
    "updated",
    "project",
)
ISSUE_EXPAND = ("renderedFields", "names", "schema")


class JiraIssueAPI:
    """JIRA issue search and nested-resource pagination."""

    def __init__(self, client: JiraClient, config: JiraProjectConfig) -> None:
        """Bind issue traversal to a client and validated config."""
        self.client = client
        self.config = config

    def search(self, jql: str) -> Iterator[dict[str, Any]]:
        """Iterate search results using the endpoint appropriate to deployment."""
        if self.config.deployment_type == JiraDeploymentType.CLOUD:
            yield from self._search_cloud(jql)
        else:
            yield from self._search_datacenter(jql)

    def search_page(
        self,
        jql: str,
        *,
        cursor: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch one native Jira Cloud-token or Data Center-offset page."""

        if limit < 1:
            raise ValueError("JIRA page limit must be positive")
        provider_limit = min(limit, self.config.page_size)
        if self.config.deployment_type == JiraDeploymentType.CLOUD:
            token = _cursor_value(cursor, expected_kind="token")
            response = self.client.post_json(
                "search/jql",
                json=search_jql_body(
                    jql=jql,
                    max_results=provider_limit,
                    fields=DISCOVERY_FIELDS,
                    next_page_token=token,
                ),
            )
            issues = list(response.get("issues") or [])
            next_token = response.get("nextPageToken")
            return (
                issues,
                None if response.get("isLast") or not next_token else f"token:{next_token}",
            )

        raw_offset = _cursor_value(cursor, expected_kind="offset")
        start_at = int(raw_offset or 0)
        if start_at < 0:
            raise ValueError("invalid JIRA discovery cursor")
        response = self.client.post_json(
            "search",
            json=search_body(
                jql=jql,
                start_at=start_at,
                max_results=provider_limit,
                fields=DISCOVERY_FIELDS,
            ),
        )
        issues = list(response.get("issues") or [])
        next_offset = start_at + len(issues)
        total = response.get("total")
        done = (
            not issues
            or (total is not None and next_offset >= int(total))
            or len(issues) < provider_limit
        )
        return issues, None if done else f"offset:{next_offset}"

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch one issue with configured fields and expansion settings."""
        issue_key = validate_issue_key(issue_key)
        response = self.client.get_json(
            f"issue/{issue_key}",
            params={
                "fields": ",".join(self.config.requested_fields()),
                "expand": ",".join(self.issue_expand()),
            },
        )
        return response

    def fetch_comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch comments for one issue, truncated to the configured cap."""
        issue_key = validate_issue_key(issue_key)
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.client.get_json(
                f"issue/{issue_key}/comment",
                params={"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = response.get("comments", [])
            truncated = truncate_with_limit(comments, values, limit=self.config.max_comments)
            if truncated:
                return comments
            start_at += len(values)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return comments
            if len(values) < self.config.page_size:
                return comments

    def fetch_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch issue changelog pages, truncated to the configured cap."""
        issue_key = validate_issue_key(issue_key)
        histories: list[dict[str, Any]] = []
        start_at = 0
        while True:
            response = self.client.get_json(
                f"issue/{issue_key}/changelog",
                params={"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = response.get("values") or response.get("histories") or []
            truncated = truncate_with_limit(
                histories,
                changelog_histories(response),
                limit=self.config.max_changelog_items,
            )
            if truncated:
                return histories
            start_at += len(values)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return histories
            if len(values) < self.config.page_size:
                return histories

    def issue_expand(self) -> tuple[str, ...]:
        """Return JIRA expansions needed for body rendering and custom fields."""
        return ISSUE_EXPAND

    def _search_cloud(self, jql: str) -> Iterator[dict[str, Any]]:
        """Paginate Jira Cloud's token-based ``/search/jql`` endpoint."""
        next_page_token: str | None = None
        while True:
            response = self.client.post_json(
                "search/jql",
                json=search_jql_body(
                    jql=jql,
                    max_results=self.config.page_size,
                    fields=DISCOVERY_FIELDS,
                    next_page_token=next_page_token,
                ),
            )
            issues = response.get("issues") or []
            yield from issues

            next_page_token = response.get("nextPageToken")
            if response.get("isLast") or not next_page_token:
                return

    def _search_datacenter(self, jql: str) -> Iterator[dict[str, Any]]:
        """Paginate Jira Data Center's offset-based ``/search`` endpoint."""
        start_at = 0
        while True:
            response = self.client.post_json(
                "search",
                json=search_body(
                    jql=jql,
                    start_at=start_at,
                    max_results=self.config.page_size,
                    fields=DISCOVERY_FIELDS,
                ),
            )
            issues = response.get("issues") or []
            if not issues:
                return
            yield from issues

            start_at += len(issues)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return
            if len(issues) < self.config.page_size:
                return


def _cursor_value(cursor: str | None, *, expected_kind: str) -> str | None:
    if cursor is None:
        return None
    kind, separator, value = cursor.partition(":")
    if (
        not separator
        or kind != expected_kind
        or not value
        or len(value) > 4096
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError("invalid JIRA discovery cursor")
    return value
