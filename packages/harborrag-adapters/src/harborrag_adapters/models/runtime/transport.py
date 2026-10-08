from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

from pydantic import SecretStr

from harborrag_core.security.field_names import canonical_field_tokens, is_sensitive_field_name

from .security import HeaderValue, reveal_secret

# Header vocabulary the shared field-name policy does not flag on its own: it knows
# ``api_key`` and ``subscription_key`` as compounds, but not a bare ``key``
# (``X-Functions-Key``), ``auth``, ``signature``, ``subscription`` or ``cookie``.
_EXTRA_SENSITIVE_HEADER_TOKENS = frozenset({"auth", "cookie", "key", "signature", "subscription"})


def is_sensitive_header_name(name: str) -> bool:
    """Return whether a header name suggests it carries credential material.

    Matching ignores case, ``-``/``_`` separators and camel case. The shared
    field-name policy is widened with header vocabulary because gateways such as
    Azure API Management (``Ocp-Apim-Subscription-Key``) and webhook signers
    authenticate through such headers. A false positive only hides a value from
    ``repr`` and configuration dumps; the provider still receives the plaintext.
    """

    return is_sensitive_field_name(name) or bool(
        canonical_field_tokens(name) & _EXTRA_SENSITIVE_HEADER_TOKENS
    )


def protect_sensitive_headers(value: Any) -> Any:
    """Wrap credential-like header values so repr and dumps never render plaintext."""

    if not isinstance(value, Mapping):
        return value
    return {
        key: (SecretStr(item) if isinstance(item, str) and is_sensitive_header_name(key) else item)
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
        raise ValueError("invalid provider base URL")
    if parsed.username or parsed.password:
        raise ValueError("provider base URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("provider base URL must not contain query or fragment data")
    hostname = parsed.hostname.lower()
    is_local = hostname == "localhost"
    if not is_local:
        with suppress(ValueError):
            is_local = ipaddress.ip_address(hostname).is_loopback
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
