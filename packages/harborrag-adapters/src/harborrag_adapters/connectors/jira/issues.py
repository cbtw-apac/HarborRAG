"""JIRA issue search and nested-resource pagination."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.policies.validation import truncate_with_limit

from .client import JiraClient
from .config import JiraDeploymentType, JiraProjectConfig
from .mappers import changelog_histories
from .query import search_body, search_jql_body, validate_issue_key

logger = logging.getLogger("harborrag.adapters.connectors.jira")

ISSUE_EXPAND = ("renderedFields", "names", "schema")
DESCRIPTOR_FIELDS = (
    "summary",
    "issuetype",
    "status",
    "labels",
    "updated",
    "project",
    "parent",
    "subtasks",
    "issuelinks",
    "attachment",
)
DISCOVERY_FIELDS = DESCRIPTOR_FIELDS
DISCOVERY_DESCRIPTOR_KEY = "_jira_discovery_descriptor"
_MAX_PROVIDER_PAGES = 10_000


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

    def has_project_permission(
        self,
        project_key: str,
        *,
        permission: str = "BROWSE_PROJECTS",
    ) -> bool:
        """Return whether the authenticated credential can browse one project.

        Used to disambiguate a zero-issue search result: Jira's search
        endpoints return HTTP 200 with an empty ``issues`` list rather than
        403 when the credential lacks this permission on every project in
        scope, so a real permission gap otherwise looks identical to "no
        matching issues".

        ``mypermissions`` predates the Cloud/Data Center API version split
        and keeps the same ``projectKey``/``permissions`` query params and
        ``permissions.<KEY>.havePermission`` response shape on both
        ``/rest/api/2/`` and ``/rest/api/3/``.
        """
        response = self.client.get_json(
            "mypermissions",
            params={"projectKey": project_key, "permissions": permission},
        )
        permissions = response.get("permissions") or {}
        return bool((permissions.get(permission) or {}).get("havePermission"))

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

    def get_issue_descriptor(self, issue_key: str) -> dict[str, Any]:
        """Fetch admission and relation fields without requesting issue prose."""

        issue_key = validate_issue_key(issue_key)
        return self.client.get_json(
            f"issue/{issue_key}",
            params={"fields": ",".join(DESCRIPTOR_FIELDS)},
        )

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
            total = response.get("total")
            logger.debug(
                "JIRA comments page fetched issue_key=%s start=%d records=%d total=%s",
                issue_key,
                start_at,
                len(values),
                total,
            )
            truncated = truncate_with_limit(comments, values, limit=self.config.max_comments)
            if truncated:
                return comments
            start_at += len(values)
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
            logger.debug(
                "JIRA changelog page fetched issue_key=%s start=%d records=%d total=%s",
                issue_key,
                start_at,
                len(values),
                response.get("total"),
            )
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
        seen_tokens: set[str] = set()
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_PROVIDER_PAGES:
                raise FetchError("JIRA search exceeded the pagination limit")
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
            logger.debug(
                "JIRA search page fetched deployment=cloud records=%d has_next=%s",
                len(issues),
                bool(response.get("nextPageToken")) and not bool(response.get("isLast")),
            )
            yield from issues

            raw_token = response.get("nextPageToken")
            if response.get("isLast") or not raw_token:
                return
            next_page_token = str(raw_token)
            if (
                next_page_token in seen_tokens
                or len(next_page_token) > 4096
                or any(ord(character) < 32 for character in next_page_token)
            ):
                raise FetchError("JIRA search pagination did not advance")
            seen_tokens.add(next_page_token)

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
            logger.debug(
                "JIRA search page fetched deployment=datacenter start=%d records=%d total=%s",
                start_at,
                len(issues),
                response.get("total"),
            )
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
