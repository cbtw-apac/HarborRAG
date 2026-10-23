"""Jira issue search and nested-resource pagination.

Handles:
- Issue search for Cloud page tokens and Data Center offsets
- Issue and descriptor fetches with restricted comments removed
- Comment and changelog paging up to the configured caps
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.policies.validation import truncate_with_limit

from .client import JiraClient
from .comment_visibility import redact_restricted_comments, shareable_comments
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
    "security",
    "parent",
    "subtasks",
    "issuelinks",
    "attachment",
    # Admission versions every comment by id and timestamp.
    "comment",
)
DISCOVERY_FIELDS = DESCRIPTOR_FIELDS
DISCOVERY_DESCRIPTOR_KEY = "_jira_discovery_descriptor"
_MAX_PROVIDER_PAGES = 10_000


class JiraIssueAPI:
    """Gateway for Jira issue search and nested resources."""

    def __init__(self, client: JiraClient, config: JiraProjectConfig) -> None:
        self.client = client
        self.config = config

    def search(self, jql: str) -> Iterator[dict[str, Any]]:
        """Yield every search result, using the Cloud or Data Center endpoint."""
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
        """Fetch one native search page: Cloud page token or Data Center offset.

        Args:
            jql: Search query.
            cursor: ``token:<value>`` (Cloud) or ``offset:<n>`` (Data Center), or
                ``None`` for the first page.
            limit: Maximum issues, capped at the configured page size.

        Returns:
            Issues with restricted comments removed, and the next cursor, ``None`` on
            the last page.

        Raises:
            ValueError: If ``limit`` is below 1 or the cursor is invalid.
        """

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
            issues = [redact_restricted_comments(issue) for issue in response.get("issues") or []]
            next_token = response.get("nextPageToken")
            return (
                issues,
                None if response.get("isLast") or not next_token else f"token:{next_token}",
            )

        # TODO: page by a stable cursor; offsets over ``updated ASC`` can skip issues.
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
        issues = [redact_restricted_comments(issue) for issue in response.get("issues") or []]
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
        """Return whether the credential holds ``permission`` on one project.

        Args:
            project_key: Project to probe.
            permission: Jira permission key.

        Returns:
            ``True`` when ``mypermissions`` reports ``havePermission``.
        """
        response = self.client.get_json(
            "mypermissions",
            params={"projectKey": project_key, "permissions": permission},
        )
        permissions = response.get("permissions") or {}
        return bool((permissions.get(permission) or {}).get("havePermission"))

    def get_issue(self, issue_key: str) -> dict[str, Any]:
        """Fetch one issue with the configured fields, restricted comments removed.

        Raises:
            ValueError: If ``issue_key`` is invalid.
        """
        issue_key = validate_issue_key(issue_key)
        response = self.client.get_json(
            f"issue/{issue_key}",
            params={
                "fields": ",".join(self.config.requested_fields()),
                "expand": ",".join(self.issue_expand()),
            },
        )
        return redact_restricted_comments(response)

    def get_issue_descriptor(self, issue_key: str) -> dict[str, Any]:
        """Fetch only the admission and relation fields, restricted comments removed.

        Raises:
            ValueError: If ``issue_key`` is invalid.
        """

        issue_key = validate_issue_key(issue_key)
        return redact_restricted_comments(
            self.client.get_json(
                f"issue/{issue_key}",
                params={"fields": ",".join(DESCRIPTOR_FIELDS)},
            )
        )

    def issue_comments(self, issue_key: str, issue: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the issue's shareable comments, fetching only if the embedded list is partial.

        Capped at ``max_comments``. On Data Center the embedded list cannot detect
        internal notes.

        Args:
            issue_key: Key used for the fallback fetch.
            issue: Issue payload that may embed the comment page.

        Returns:
            Shareable comments, as ``fetch_comments`` returns them.
        """

        embedded = (issue.get("fields") or {}).get("comment")
        if isinstance(embedded, dict):
            values = embedded.get("comments")
            total = embedded.get("total")
            if (
                isinstance(values, list)
                and isinstance(total, int)
                and not isinstance(total, bool)
                and total <= len(values)
            ):
                comments: list[dict[str, Any]] = []
                truncate_with_limit(
                    comments, shareable_comments(values), limit=self.config.max_comments
                )
                return comments
        return self.fetch_comments(issue_key)

    def fetch_comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch an issue's shareable comments, capped at ``max_comments``.

        Raises:
            ValueError: If ``issue_key`` is invalid.
        """
        issue_key = validate_issue_key(issue_key)
        comments: list[dict[str, Any]] = []
        start_at = 0
        while True:
            # `properties` carries `sd.public.comment`, the only mark of a
            # Service Management internal note on Data Center.
            response = self.client.get_json(
                f"issue/{issue_key}/comment",
                params={
                    "startAt": start_at,
                    "maxResults": self.config.page_size,
                    "expand": "properties",
                },
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
            truncated = truncate_with_limit(
                comments, shareable_comments(values), limit=self.config.max_comments
            )
            if truncated:
                return comments
            start_at += len(values)
            if total is not None and start_at >= int(total):
                return comments
            if len(values) < self.config.page_size:
                return comments

    def fetch_changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Fetch an issue's changelog histories, capped at ``max_changelog_items``.

        Raises:
            ValueError: If ``issue_key`` is invalid.
        """
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
        """Return the expansions needed for body rendering and custom fields."""
        return ISSUE_EXPAND

    def _search_cloud(self, jql: str) -> Iterator[dict[str, Any]]:
        """Yield issues from Cloud's token-based ``/search/jql``; ``FetchError`` on a stuck page."""
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
            yield from (redact_restricted_comments(issue) for issue in issues)

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
        """Yield issues from Data Center's offset-based ``/search``."""
        # TODO: page by a stable cursor; offsets over ``updated ASC`` can skip issues.
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
            yield from (redact_restricted_comments(issue) for issue in issues)

            start_at += len(issues)
            total = response.get("total")
            if total is not None and start_at >= int(total):
                return
            if len(issues) < self.config.page_size:
                return


def _cursor_value(cursor: str | None, *, expected_kind: str) -> str | None:
    """Return the value of a ``<kind>:<value>`` cursor, or ``None`` for no cursor.

    Raises ``ValueError`` on a wrong kind or an empty, oversized or control-character value.
    """

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
