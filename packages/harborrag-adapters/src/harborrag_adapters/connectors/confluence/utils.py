"""Pure URL, query, and payload helpers for Confluence."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse


def format_query_timestamp(value: datetime) -> str:
    """Render a datetime for CQL/JQL as UTC ``yyyy/MM/dd HH:mm``.

    Bare CQL/JQL timestamps are interpreted in the API user's configured
    timezone, so a naive local-time render silently shifts the incremental-sync
    watermark by that offset (missing or re-ingesting documents). We normalize
    to UTC — deployments must set the integration account's timezone to UTC (and
    ideally apply a safety-overlap window) for exact incremental behavior.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y/%m/%d %H:%M")


CONTENT_EXPAND = (
    "body.export_view,body.storage,version,metadata.labels,history,space,"
    "extensions.position,ancestors,children.page"
)
LIGHT_EXPAND = "version,metadata.labels,space"
COMMENT_EXPAND = "body.storage,history"
DEFAULT_PAGE_SIZE = 25

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CONTENT_ID_RE = re.compile(r"^[0-9]+$")


def is_cloud_hostname(base_url: str) -> bool:
    """Return whether a base URL looks like Atlassian Cloud."""
    try:
        hostname = urlparse(str(base_url)).hostname
    except ValueError:
        return False
    return bool(hostname and hostname.endswith(".atlassian.net"))


def extract_cursor(next_url: str | None) -> str | None:
    """Extract Confluence Cloud cursor pagination from a next link."""
    if not next_url:
        return None
    values = parse_qs(urlparse(next_url).query).get("cursor")
    return values[0] if values else None


def quote_cql(value: str) -> str:
    """Quote a CQL literal with the escaping Confluence expects."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def validate_token(value: str, *, field_name: str) -> str:
    """Validate simple CQL tokens that should never contain operators."""
    if not _TOKEN_RE.fullmatch(value):
        raise ValueError(
            f"Invalid Confluence {field_name}: only letters, numbers, "
            "underscore and hyphen are allowed."
        )
    return value


def validate_content_id(value: str) -> str:
    """Require the numeric IDs accepted by Confluence content paths."""
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
    raw_cql: str | None = None,
) -> str:
    """Build a conservative CQL search expression from shared filters."""
    if raw_cql:
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

    return " and ".join(clauses) or 'type in ("page","blogpost")'


def build_search_params(
    *,
    cql: str,
    limit: int = DEFAULT_PAGE_SIZE,
    start: int | None = None,
    cursor: str | None = None,
    expand: str = LIGHT_EXPAND,
) -> dict[str, Any]:
    """Build Confluence search params for cursor or offset pagination."""
    params: dict[str, Any] = {"cql": cql, "limit": limit, "expand": expand}
    if cursor:
        params["cursor"] = cursor
    elif start is not None:
        params["start"] = start
    return params


def _isoformat_datetimes(value: Any) -> Any:
    """Recursively convert ``datetime`` values to ISO strings for JSON output."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _isoformat_datetimes(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_isoformat_datetimes(item) for item in value]
    return value
