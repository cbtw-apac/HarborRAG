"""Jira discovery policy: query translation, issue records and project scope.

Handles:
- Issue keys and JQL derived from a connector query
- Source records for explicit issue keys
- Issue validation against the configured projects
- Empty-search permission probing
"""

from __future__ import annotations

import logging
from typing import Any

from harborrag_adapters.connectors.attachments import attachment_ids_from_filters
from harborrag_adapters.connectors.exceptions import (
    AuthorizationError,
    DocumentProcessingError,
    FetchError,
)
from harborrag_adapters.connectors.query_values import normalized_string_list
from harborrag_adapters.connectors.schemas import ConnectorQuery
from harborrag_core.domain.source import SourceRecord

from .config import JiraProjectConfig
from .issues import JiraIssueAPI
from .mappers import issue_url
from .query import build_jql, validate_issue_key


def issue_keys_from_query(query: ConnectorQuery) -> list[str]:
    """Return the validated keys from the ``issue_keys`` or ``keys`` filter.

    Args:
        query: Connector query to read the filter from.

    Returns:
        Issue keys, empty when neither filter is set.

    Raises:
        ValueError: If a key is not a valid issue key.
    """

    values = query.filters.get("issue_keys") or query.filters.get("keys")
    if values is None:
        return []
    if isinstance(values, str):
        return [validate_issue_key(values)]
    return [validate_issue_key(str(value)) for value in values]


class JiraDiscoveryPolicy:
    """Query-to-JQL translation, record construction and project scope checks for discovery."""

    def __init__(self, config: JiraProjectConfig, *, base_url: str) -> None:
        self._config = config
        self._base_url = base_url

    def effective_project_keys(self, query: ConnectorQuery) -> list[str]:
        """Return the project keys a search is bounded to: query filters or path, else configured."""
        filters = query.filters
        return normalized_string_list(
            filters.get("project_keys") or filters.get("project_key") or query.path,
            default=self._config.project_keys,
        )

    def jql(self, query: ConnectorQuery) -> str:
        """Build the search JQL from the query filters, with configured values as defaults."""

        filters = query.filters
        return build_jql(
            project_keys=self.effective_project_keys(query),
            issue_types=normalized_string_list(
                filters.get("issue_types") or filters.get("issue_type"),
                default=self._config.issue_types,
            ),
            statuses=normalized_string_list(
                filters.get("statuses") or filters.get("status"),
                default=self._config.statuses,
            ),
            labels=normalized_string_list(
                filters.get("labels") or filters.get("label"),
                default=self._config.labels,
            ),
            updated_after=query.updated_after,
            text_search=query.pattern,
            raw_jql=filters.get("jql"),
        )

    def record_for_key(self, issue_key: str, query: ConnectorQuery) -> SourceRecord:
        """Build a source record for an explicit issue key without calling Jira.

        Args:
            issue_key: Issue key such as ``ENG-1``.
            query: Query whose per-issue switches are copied onto the record.

        Returns:
            Source record for the issue.

        Raises:
            ValueError: If the key is invalid.
            DocumentProcessingError: If the key's project is outside the configured
                projects.
        """

        issue_key = validate_issue_key(issue_key)
        project_key = issue_key.split("-", 1)[0]
        self._check_project_scope(project_key, issue_key)
        return self.apply_query(
            SourceRecord(
                id=f"jira://{project_key}/{issue_key}",
                source_type="application/vnd.atlassian.jira.issue+json",
                locator=issue_key,
                metadata={
                    "issue_key": issue_key,
                    "project_key": project_key,
                    "url": issue_url(self._base_url, issue_key),
                    "include_attachments": query.include_attachments,
                },
            ),
            query,
        )

    @staticmethod
    def apply_query(record: SourceRecord, query: ConnectorQuery) -> SourceRecord:
        """Copy the query's per-issue switches onto the record metadata, in place.

        ``include_comments`` and ``build_graph`` default to true; a non-empty attachment
        selection is stored as ``_selected_attachment_ids``.
        """

        record.metadata["include_attachments"] = query.include_attachments
        record.metadata["include_comments"] = bool(query.filters.get("include_comments", True))
        record.metadata["build_graph"] = bool(query.filters.get("build_graph", True))
        selected = attachment_ids_from_filters(query.filters)
        if selected:
            record.metadata["_selected_attachment_ids"] = selected
        return record

    def validate_issue(self, issue: dict[str, Any], issue_key: str) -> None:
        """Check an issue has id, key and summary and lies in the configured projects.

        Raises:
            DocumentProcessingError: If a required field is missing or the project
                is outside the configured projects.
        """

        fields = issue.get("fields", {})
        missing = [
            name
            for name, value in (
                ("id", issue.get("id")),
                ("key", issue.get("key")),
                ("fields.summary", fields.get("summary")),
            )
            if not value
        ]
        if missing:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} missing required fields: {', '.join(missing)}"
            )
        self._check_project_scope(fields.get("project", {}).get("key"), issue_key)

    def _check_project_scope(self, project_key: str | None, issue_key: str) -> None:
        """Raise ``DocumentProcessingError`` if configured keys exclude ``project_key``."""

        # TODO: check the query's own project scope, not only the configured keys.
        if self._config.project_keys and project_key not in self._config.project_keys:
            raise DocumentProcessingError(
                f"JIRA issue {issue_key} belongs to project {project_key!r}, "
                f"outside configured projects {self._config.project_keys!r}"
            )


def verify_empty_search_result(
    query: ConnectorQuery,
    *,
    policy: JiraDiscoveryPolicy,
    issues: JiraIssueAPI,
    logger_: logging.Logger,
) -> None:
    """Rule out a missing browse permission behind an empty search result.

    Jira answers 200 with no issues when the credential cannot browse the searched
    projects. An inconclusive probe (``FetchError``) leaves the empty result standing.

    Args:
        query: Query whose project scope is probed; nothing is checked when it resolves no
            project keys.
        policy: Policy that resolves the query's project keys.
        issues: Issue API used for the permission probe.
        logger_: Logger for the inconclusive-probe warning.

    Raises:
        AuthorizationError: If the credential lacks ``BROWSE_PROJECTS`` on every
            project in scope.
    """
    project_keys = policy.effective_project_keys(query)
    if not project_keys:
        return
    try:
        permitted = any(issues.has_project_permission(key) for key in project_keys)
    except FetchError:
        logger_.warning(
            "JIRA permission probe inconclusive for projects=%r; "
            "treating empty search result as genuine",
            project_keys,
        )
        return
    if permitted:
        return
    raise AuthorizationError(
        f"JIRA credential lacks BROWSE_PROJECTS permission on {project_keys!r}"
    )
