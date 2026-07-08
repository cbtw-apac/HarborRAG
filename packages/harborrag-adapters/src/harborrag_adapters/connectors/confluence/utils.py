from __future__ import annotations

from datetime import datetime
from typing import Any

from urllib.parse import urlparse


def is_cloud_hostname(base_url: str) -> bool:
    """True if `base_url` looks like a Confluence Cloud site (*.atlassian.net).

    Deliberately has zero dependency on config.py or any Confluence-specific
    types, so config.py can import this at module load time without the
    config <-> auth circular import the previous version worked around with
    a deferred (function-local) import inside __post_init__.
    """
    try:
        hostname = urlparse(str(base_url)).hostname
    except ValueError:
        return False
    if not hostname:
        return False
    return hostname == "atlassian.net" or hostname.endswith(".atlassian.net")


def _quote_cql_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _sanitize_space_key(space_key: str) -> str:
    if not _ALLOWED_TOKEN_RE.fullmatch(space_key):
        raise ValueError(
            "Invalid Confluence space key. Only alphanumerics, underscore and hyphen are allowed."
        )
    return _quote_cql_literal(space_key)


def _sanitize_content_types(content_types: list[str]) -> list[str]:
    sanitized: list[str] = []
    for content_type in content_types:
        if not isinstance(content_type, str) or not _ALLOWED_TOKEN_RE.fullmatch(
            content_type
        ):
            raise ValueError(f"Invalid Confluence content type: {content_type!r}")
        sanitized.append(_quote_cql_literal(content_type))
    return sanitized


def _build_cql(
    space_key: str, content_types: list[str] | None, updated_after: datetime | None
) -> str:
    cql = f"space = {_sanitize_space_key(space_key)}"
    if content_types:
        safe_types = _sanitize_content_types(content_types)
        cql += f" and type in ({','.join(safe_types)})"
    if updated_after is not None:
        cql += f' and lastmodified >= "{updated_after.strftime("%Y/%m/%d %H:%M")}"'
    return cql


def build_cloud_search_params(
    space_key: str,
    content_types: list[str] | None,
    cursor: str | None,
    light: bool = False,
    updated_after: datetime | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "expand": LIGHT_EXPAND if light else CONTENT_EXPAND,
        "limit": DEFAULT_PAGE_SIZE,
        "cql": _build_cql(space_key, content_types, updated_after),
    }
    if cursor is not None:
        params["cursor"] = cursor
    return params


def build_dc_search_params(
    space_key: str,
    content_types: list[str] | None,
    start: int,
    light: bool = False,
    updated_after: datetime | None = None,
) -> dict[str, Any]:
    return {
        "expand": LIGHT_EXPAND if light else CONTENT_EXPAND,
        "limit": DEFAULT_PAGE_SIZE,
        "start": start,
        "cql": _build_cql(space_key, content_types, updated_after),
    }