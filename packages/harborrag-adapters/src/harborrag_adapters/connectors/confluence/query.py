"""CQL building, search parameters and ``expand`` sets for Confluence.

Handles:
- UTC query timestamps and CQL literal quoting
- Token and content ID validation
- Search expression and search parameter building
- Cloud cursor extraction and ``expand`` field sets
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from harborrag_adapters.connectors.atlassian.query import is_cloud_hostname as is_cloud_hostname


def format_query_timestamp(value: datetime) -> str:
    """Render a datetime for CQL as UTC ``yyyy/MM/dd HH:mm``.

    Bare CQL timestamps are read in the API user's timezone, so that account should be set to UTC.

    Args:
        value: Datetime to render; naive values are treated as UTC.

    Returns:
        The UTC timestamp string.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y/%m/%d %H:%M")


READ_RESTRICTIONS_EXPAND = (
    "restrictions.read.restrictions.user,restrictions.read.restrictions.group"
)
CONTENT_EXPAND = (
    "body.storage,body.export_view,version,metadata.labels,history,space,"
    f"extensions.position,ancestors,children.page,{READ_RESTRICTIONS_EXPAND}"
)
CLOUD_CONTENT_EXPAND = f"body.atlas_doc_format,{CONTENT_EXPAND}"
LIGHT_EXPAND = "version,metadata.labels,space"
DESCRIPTOR_EXPAND = (
    f"version,metadata.labels,history,space,ancestors,children.page,{READ_RESTRICTIONS_EXPAND}"
)
COMMENT_EXPAND = "body.storage,history"
DEFAULT_PAGE_SIZE = 25

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTENT_ID_RE = re.compile(r"^[0-9]+$")


def extract_cursor(next_url: str | None) -> str | None:
    """Return the ``cursor`` value of a Cloud ``next`` link, or None."""
    if not next_url:
        return None
    values = parse_qs(urlparse(next_url).query).get("cursor")
    return values[0] if values else None


def quote_cql(value: str) -> str:
    """Quote a CQL literal, escaping backslashes and double quotes."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_token(value: str, *, field_name: str) -> str:
    """Return a value that is a plain CQL token.

    Args:
        value: Candidate token.
        field_name: Name used in the error message.

    Returns:
        The unchanged value.

    Raises:
        ValueError: If it has characters other than letters, digits, underscore or hyphen.
    """
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(
            f"Invalid Confluence {field_name}: only letters, numbers, "
            "underscore and hyphen are allowed."
        )
    return value


def validate_content_id(value: str) -> str:
    """Return the stripped numeric content ID; raise ``ValueError`` unless it is digits only."""
    normalized = str(value).strip()
    if not _CONTENT_ID_RE.fullmatch(normalized):
        raise ValueError("Invalid Confluence content ID: expected digits only")
    return normalized


def build_cql(
    *,
    space_key: str | None = None,
    content_types: list[str] | None = None,
    labels: list[str] | None = None,
    updated_after: datetime | None = None,
    text_search: str | None = None,
    raw_cql: str | None = None,
) -> str:
    """Build a CQL search expression from the shared filters.

    ``text_search`` is always quoted into a ``text ~`` clause. ``raw_cql`` is combined only with
    the space clause, which stays because it bounds the connection's tenant scope.

    Args:
        space_key: Configured space; validated and always included when given.
        content_types: Content types for the ``type in`` clause.
        labels: Labels for the ``label in`` clause.
        updated_after: Lower bound for ``lastmodified``.
        text_search: Free-text term.
        raw_cql: Caller-supplied CQL used in place of the other clauses.

    Returns:
        CQL expression; ``type in ("page","blogpost")`` when no filter applies.

    Raises:
        ValueError: If the space key, a content type or a label is not a plain token.
    """
    # TODO: keep the time window and handle ``ORDER BY`` when wrapping raw CQL.
    if raw_cql:
        if space_key:
            safe_space = validate_token(space_key, field_name="space key")
            return f"({raw_cql}) and space = {quote_cql(safe_space)}"
        return raw_cql

    clauses: list[str] = []
    if space_key:
        safe_space = validate_token(space_key, field_name="space key")
        clauses.append(f"space = {quote_cql(safe_space)}")
    if content_types:
        safe_types = [
            quote_cql(validate_token(value, field_name="content type")) for value in content_types
        ]
        clauses.append(f"type in ({','.join(safe_types)})")
    if labels:
        safe_labels = [quote_cql(validate_token(value, field_name="label")) for value in labels]
        clauses.append(f"label in ({','.join(safe_labels)})")
    if updated_after:
        clauses.append(f"lastmodified >= {quote_cql(format_query_timestamp(updated_after))}")
    if text_search:
        clauses.append(f"text ~ {quote_cql(text_search)}")

    return " and ".join(clauses) or 'type in ("page","blogpost")'


def build_search_params(
    *,
    cql: str,
    limit: int = DEFAULT_PAGE_SIZE,
    start: int | None = None,
    cursor: str | None = None,
    expand: str = LIGHT_EXPAND,
) -> dict[str, Any]:
    """Build Confluence search params for cursor or offset pagination.

    Args:
        cql: CQL expression.
        limit: Maximum results per page.
        start: Offset for Data Center paging; ignored when ``cursor`` is given.
        cursor: Cloud pagination cursor.
        expand: ``expand`` field set to request.

    Returns:
        Query parameters for ``content/search``.
    """
    params: dict[str, Any] = {"cql": cql, "limit": limit, "expand": expand}
    if cursor:
        params["cursor"] = cursor
    elif start is not None:
        params["start"] = start
    return params
