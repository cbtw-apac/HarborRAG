from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse


_PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def is_cloud_hostname(base_url: str) -> bool:
    """Return whether a base URL looks like Atlassian Cloud."""
    try:
        hostname = urlparse(str(base_url)).hostname
    except ValueError:
        return False
    return bool(hostname and hostname.endswith(".atlassian.net"))


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


def build_jql(
    *,
    project_keys: list[str] | None = None,
    issue_types: list[str] | None = None,
    statuses: list[str] | None = None,
    labels: list[str] | None = None,
    updated_after: datetime | None = None,
    raw_jql: str | None = None,
) -> str:
    """Build a deterministic JQL query from shared connector filters."""
    if raw_jql:
        return raw_jql

    clauses: list[str] = []
    if project_keys:
        safe_projects = [quote_jql(validate_project_key(key)) for key in project_keys]
        clauses.append(f"project in ({','.join(safe_projects)})")
    if issue_types:
        clauses.append(f"issuetype in ({_quoted_values(issue_types)})")
    if statuses:
        clauses.append(f"status in ({_quoted_values(statuses)})")
    if labels:
        clauses.append(f"labels in ({_quoted_values(labels)})")
    if updated_after:
        clauses.append(
            f"updated >= {quote_jql(updated_after.strftime('%Y/%m/%d %H:%M'))}"
        )

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
        body["expand"] = ",".join(expand)
    return body


def _quoted_values(values: list[str]) -> str:
    return ",".join(quote_jql(value) for value in values)
