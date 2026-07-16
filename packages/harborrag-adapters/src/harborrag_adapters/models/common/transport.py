from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from pydantic import SecretStr

from .security import HeaderValue, reveal_secret

_SENSITIVE_HEADERS = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key"})


def protect_sensitive_headers(value: Any) -> Any:
    """Wrap authentication header values so repr never renders plaintext."""

    if not isinstance(value, Mapping):
        return value
    return {
        key: SecretStr(item)
        if key.lower() in _SENSITIVE_HEADERS and isinstance(item, str)
        else item
        for key, item in value.items()
    }


def validate_base_url(
    url: str | None,
    *,
    allowed_hosts: frozenset[str] | None,
    require_https: bool,
) -> None:
    """Enforce endpoint scheme, loopback, and optional host allowlist policy."""

    if not url:
        return
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"invalid provider base URL: {url!r}")
    hostname = parsed.hostname.lower()
    is_local = hostname == "localhost"
    if not is_local:
        try:
            is_local = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            pass
    if require_https and not is_local and parsed.scheme != "https":
        raise ValueError("remote provider base URLs must use HTTPS")
    normalized_hosts = (
        {allowed.lower() for allowed in allowed_hosts} if allowed_hosts is not None else None
    )
    if normalized_hosts is not None and hostname not in normalized_hosts:
        raise ValueError(f"provider base URL host {hostname!r} is not allowed")


def reveal_headers(headers: Mapping[str, HeaderValue]) -> dict[str, str]:
    """Resolve header values down to plaintext at the provider call boundary."""

    return {
        key: value for key, item in headers.items() if (value := reveal_secret(item)) is not None
    }
