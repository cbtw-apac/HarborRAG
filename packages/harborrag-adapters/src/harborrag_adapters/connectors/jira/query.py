"""Pure query, URL, and payload helpers for JIRA."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from harborrag_adapters.connectors.atlassian.query import is_cloud_hostname as is_cloud_hostname


def format_query_timestamp(value: datetime) -> str:
    """Render a datetime for JQL as UTC ``yyyy/MM/dd HH:mm``.

    Bare JQL timestamps are evaluated in the API user's timezone; normalizing to
    UTC keeps the incremental-sync watermark from drifting by that offset.
    Deployments should set the integration account timezone to UTC.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y/%m/%d %H:%M")


_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_ORDER_BY_RE = re.compile(r"\border\s+by\b", re.IGNORECASE)


def quote_jql(value: str) -> str:
    """Quote a JQL literal with the escaping JIRA expects."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_project_key(value: str) -> str:
    """Validate project keys before interpolating them into generated JQL."""
    if not _PROJECT_RE.fullmatch(value):
        raise ValueError(
            "Invalid JIRA project key: expected uppercase letters, numbers, "
            "or underscore, starting with a letter."
        )
    return value


def validate_issue_key(value: str) -> str:
    """Validate issue keys before interpolating them into REST paths."""
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
    """Build a deterministic JQL query from shared connector filters.

    ``text_search`` is a free-text term and is always escaped. Raw JQL remains
    available, but configured/requested project keys are still applied as a
    mandatory safety boundary around it.
    """
    clauses: list[str] = []
    if project_keys:
        safe_projects = [quote_jql(validate_project_key(key)) for key in project_keys]
        clauses.append(f"project in ({','.join(safe_projects)})")
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
    """Build the JSON request body used by JIRA issue search."""
    body: dict[str, Any] = {
        "jql": jql,
        "startAt": start_at,
        "maxResults": max_results,
        "fields": list(fields),
    }
    if expand:
        # The search POST body expects expand as an array of strings; a
        # comma-joined string is the GET query-param convention only and is
        # rejected (or silently ignored) by the search endpoint.
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

    The legacy ``/search`` endpoint (offset ``startAt``/``total`` paging) was
    removed on Jira Cloud in 2025; the replacement paginates with an opaque
    ``nextPageToken`` and returns ``isLast`` instead of a total count. Unlike
    the legacy endpoint, this one rejects the request outright (400 Invalid
    request payload) if an ``expand`` key is present at all, so it is never
    added here even when empty.
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
