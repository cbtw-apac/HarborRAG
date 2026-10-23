"""Jira JQL and search payload helpers.

Handles:
- JQL literal quoting and issue or project key validation
- JQL assembly from connector filters
- Search request bodies for Cloud and Data Center
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from harborrag_adapters.connectors.atlassian.query import is_cloud_hostname as is_cloud_hostname


def format_query_timestamp(value: datetime) -> str:
    """Render a datetime for JQL as UTC ``yyyy/MM/dd HH:mm``; naive values are taken as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    # TODO: render in the Jira account timezone, which is how Jira reads bare timestamps.
    return value.astimezone(UTC).strftime("%Y/%m/%d %H:%M")


_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)


def quote_jql(value: str) -> str:
    """Return ``value`` as a double-quoted JQL literal with backslashes and quotes escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_project_key(value: str) -> str:
    """Return ``value`` if it is a valid project key. Raises ``ValueError`` otherwise."""
    if not _PROJECT_RE.fullmatch(value):
        raise ValueError(
            "Invalid JIRA project key: expected uppercase letters, numbers, "
            "or underscore, starting with a letter."
        )
    return value


def validate_issue_key(value: str) -> str:
    """Return the stripped issue key. Raises ``ValueError`` if it is not ``PROJECT-123``."""
    normalized = str(value).strip()
    if not _ISSUE_KEY_RE.fullmatch(normalized):
        raise ValueError(
            "Invalid JIRA issue key: expected PROJECT-123 with an uppercase "
            "project key and positive numeric issue number."
        )
    return normalized


def build_jql(  # noqa: PLR0913 - explicit allowlisted JQL clauses stay auditable
    *,
    project_keys: list[str] | None = None,
    issue_types: list[str] | None = None,
    statuses: list[str] | None = None,
    labels: list[str] | None = None,
    updated_after: datetime | None = None,
    text_search: str | None = None,
    raw_jql: str | None = None,
) -> str:
    """Build a deterministic JQL query, ordered by ``updated ASC, key ASC`` by default.

    Raw JQL keeps its own order. Project keys, when given, also bound raw JQL; raw JQL
    without them is returned as given.

    Args:
        project_keys: Project keys for the ``project in`` clause.
        issue_types: Issue type names.
        statuses: Status names.
        labels: Label names.
        updated_after: Lower bound on ``updated``.
        text_search: Free-text term, always quoted.
        raw_jql: Caller-supplied JQL; replaces the other filters.

    Returns:
        JQL string.

    Raises:
        ValueError: If a project key is invalid or raw JQL has no condition before
            ``ORDER BY``.
    """
    clauses: list[str] = []
    if project_keys:
        safe_projects = [quote_jql(validate_project_key(key)) for key in project_keys]
        clauses.append(f"project in ({','.join(safe_projects)})")
    # TODO: keep the filters with raw JQL and split ORDER BY outside quoted strings.
    if raw_jql:
        raw = raw_jql.strip()
        if not clauses:
            return raw
        order_match = _ORDER_BY_RE.search(raw)
        condition = raw[: order_match.start()].strip() if order_match else raw
        if not condition:
            raise ValueError("Raw JQL must contain a query before ORDER BY")
        ordering = raw[order_match.start() :].strip() if order_match else ""
        scoped = f"{' and '.join(clauses)} and ({condition})"
        return f"{scoped} {ordering or 'order by updated ASC, key ASC'}"
    if issue_types:
        clauses.append(f"issuetype in ({_quoted_values(issue_types)})")
    if statuses:
        clauses.append(f"status in ({_quoted_values(statuses)})")
    if labels:
        clauses.append(f"labels in ({_quoted_values(labels)})")
    if updated_after:
        clauses.append(f"updated >= {quote_jql(format_query_timestamp(updated_after))}")
    if text_search:
        clauses.append(f"text ~ {quote_jql(text_search)}")

    prefix = " and ".join(clauses) if clauses else ""
    suffix = "order by updated ASC, key ASC"
    return f"{prefix} {suffix}".strip() if prefix else suffix


def search_body(
    *,
    jql: str,
    start_at: int,
    max_results: int,
    fields: tuple[str, ...],
    expand: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the JSON body for the Data Center ``/search`` request."""
    body: dict[str, Any] = {
        "jql": jql,
        "startAt": start_at,
        "maxResults": max_results,
        "fields": list(fields),
    }
    if expand:
        # The search POST body expects expand as an array of strings.
        body["expand"] = list(expand)
    return body


def search_jql_body(
    *,
    jql: str,
    max_results: int,
    fields: tuple[str, ...],
    next_page_token: str | None = None,
) -> dict[str, Any]:
    """Build the body for Jira Cloud's ``POST /rest/api/3/search/jql``.

    Pages by opaque ``nextPageToken``. ``expand`` is never sent: the endpoint
    rejects the key.
    """
    body: dict[str, Any] = {
        "jql": jql,
        "maxResults": max_results,
        "fields": list(fields),
    }
    if next_page_token:
        body["nextPageToken"] = next_page_token
    return body


def _quoted_values(values: list[str]) -> str:
    return ",".join(quote_jql(value) for value in values)
