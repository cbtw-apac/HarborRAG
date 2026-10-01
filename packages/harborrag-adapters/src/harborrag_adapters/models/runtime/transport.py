from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from contextlib import suppress
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
        key: (
            SecretStr(item) if key.lower() in _SENSITIVE_HEADERS and isinstance(item, str) else item
        )
        for key, item in value.items()
    }


# Hostnames that container runtimes resolve to the machine running the
# container. A model server on the developer's host (Ollama, LM Studio, a
# LiteLLM proxy) is reached through one of these from inside Docker, Colima,
# Lima or Podman, and it is as local as ``localhost`` is on the host itself.
CONTAINER_HOST_ALIASES: frozenset[str] = frozenset(
    {
        "host.docker.internal",
        "gateway.docker.internal",
        "host.lima.internal",
        "host.containers.internal",
    }
)

_HTTPS_HINT = (
    " (to allow plain HTTP for a trusted host, set "
    "security.require_https_for_remote_endpoints: false and list the host in "
    "security.allowed_base_url_hosts)"
)


def is_local_hostname(hostname: str) -> bool:
    """Return whether ``hostname`` is loopback or a container-runtime alias for the host."""

    lowered = hostname.lower()
    if lowered == "localhost" or lowered in CONTAINER_HOST_ALIASES:
        return True
    with suppress(ValueError):
        return ipaddress.ip_address(lowered).is_loopback
    return False


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
    if require_https and not is_local_hostname(hostname) and parsed.scheme != "https":
        raise ValueError("remote provider base URLs must use HTTPS" + _HTTPS_HINT)
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
