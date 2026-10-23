"""Validation shared by connector configs and clients.

Handles:
- HTTPS URL validation with a private-network check
- HTTP tuning bounds
- Optional size and count limits
- Nested collection limits while paging
"""

from __future__ import annotations

import itertools
import math
import socket
from collections.abc import Iterable
from urllib.parse import urlparse

from harborrag_adapters.connectors.exceptions import DocumentProcessingError
from harborrag_core.security.url_policy import URLPolicy, URLPolicyError

DEFAULT_MAX_NESTED_ITEMS = 1000


def validate_non_negative_limit(name: str, value: int | None) -> None:
    """Raise ``ValueError`` unless ``value`` is ``None`` or an integer of at least 0."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be an integer greater than or equal to 0")


class _DeferredResolution(Exception):
    """DNS name did not resolve during validation; the decision is deferred."""


def _numeric_addresses(hostname: str, port: int) -> tuple[str, ...]:
    """Return addresses for a literal IP host, including legacy numeric forms, without DNS."""
    try:
        infos = socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST
        )
    except (socket.gaierror, UnicodeError):
        return ()
    return tuple(sorted({str(info[4][0]) for info in infos}))


def _system_resolve_host(hostname: str, port: int) -> tuple[str, ...]:
    """Resolve a DNS name. Raises ``_DeferredResolution`` if it does not resolve."""
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise _DeferredResolution(hostname) from exc
    return tuple(sorted({str(info[4][0]) for info in infos}))


# Test suites replace this seam with a hermetic stub.
_resolve_host = _system_resolve_host


def _config_resolver(hostname: str, port: int) -> Iterable[str]:
    """Resolve a host for URL policy checks: literal IPs without DNS, else system DNS."""
    return _numeric_addresses(hostname, port) or _resolve_host(hostname, port)


def validate_https_url(name: str, value: str, *, allow_private_network: bool = False) -> None:
    """Require an HTTPS URL without credentials whose host resolves only to public addresses.

    Literal IPs are checked without DNS; a name that does not resolve is accepted.

    Args:
        name: Config field name used in error messages.
        value: URL to validate.
        allow_private_network: Skip the host check for on-premises sources.

    Raises:
        ValueError: If the URL is not HTTPS, embeds credentials, or targets a non-public host.
    """
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{name} must be an HTTPS URL without embedded credentials")
    if allow_private_network:
        return
    # TODO: re-check the resolved address at connection time to cover DNS rebinding.
    try:
        URLPolicy(allowed_schemes={"https"}, resolver=_config_resolver).validate(value)
    except _DeferredResolution:
        return
    except URLPolicyError as exc:
        raise ValueError(
            f"{name} is not an allowed connector host ({exc}); set "
            "allow_private_network: true only for an on-premises source on a trusted network"
        ) from exc


def validate_http_tuning(
    *,
    requests_per_minute: int,
    request_timeout_seconds: float,
    max_retries: int,
    backoff_factor: float,
) -> None:
    """Validate shared HTTP tuning values.

    Args:
        requests_per_minute: Integer from 1 to 6000.
        request_timeout_seconds: Finite number greater than 0.
        max_retries: Integer of at least 0.
        backoff_factor: Finite number of at least 0.

    Raises:
        ValueError: If any value is out of range or not of the expected type.
    """
    if (
        isinstance(requests_per_minute, bool)
        or not isinstance(requests_per_minute, int)
        or not 1 <= requests_per_minute <= 6000
    ):
        raise ValueError("requests_per_minute must be between 1 and 6000")
    if (
        isinstance(request_timeout_seconds, bool)
        or not isinstance(request_timeout_seconds, (int, float))
        or not math.isfinite(request_timeout_seconds)
        or request_timeout_seconds <= 0
    ):
        raise ValueError("request_timeout_seconds must be a finite number greater than 0")
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be greater than or equal to 0")
    if (
        isinstance(backoff_factor, bool)
        or not isinstance(backoff_factor, (int, float))
        or not math.isfinite(backoff_factor)
        or backoff_factor < 0
    ):
        raise ValueError("backoff_factor must be a finite number greater than or equal to 0")


def extend_with_limit[T](
    target: list[T],
    values: Iterable[T],
    *,
    limit: int | None,
    label: str,
    setting_name: str,
) -> None:
    """Append a page of nested results to ``target``, raising when ``limit`` is exceeded.

    Args:
        target: List to extend.
        values: Page of results; consumed at most one item past the allowance.
        limit: Maximum total count, or ``None`` for no limit.
        label: Collection name used in the error message.
        setting_name: Config setting name used in the error message.

    Raises:
        DocumentProcessingError: If the combined count exceeds ``limit``.
    """
    if limit is None:
        target.extend(values)
        return
    # Consume at most one item past the remaining allowance.
    remaining = max(limit - len(target), 0)
    page = list(itertools.islice(values, remaining + 1))
    enforce_collection_limit(
        count=len(target) + len(page),
        limit=limit,
        label=label,
        setting_name=setting_name,
    )
    target.extend(page)


def truncate_with_limit[T](target: list[T], values: Iterable[T], *, limit: int | None) -> bool:
    """Append a page of nested results to ``target``, cutting it at ``limit``.

    Args:
        target: List to extend.
        values: Page of results; consumed at most one item past the allowance.
        limit: Maximum total count, or ``None`` for no limit.

    Returns:
        ``True`` if ``values`` was truncated.
    """
    if limit is None:
        target.extend(values)
        return False
    remaining = max(limit - len(target), 0)
    page = list(itertools.islice(values, remaining + 1))
    truncated = len(page) > remaining
    target.extend(page[:remaining])
    return truncated


def enforce_collection_limit(
    *,
    count: int,
    limit: int | None,
    label: str,
    setting_name: str,
) -> None:
    """Raise ``DocumentProcessingError`` when ``count`` exceeds ``limit``.

    Args:
        count: Collection size.
        limit: Maximum size, or ``None`` for no limit.
        label: Collection name used in the message.
        setting_name: Config setting name used in the message.
    """
    if limit is not None and count > limit:
        raise DocumentProcessingError(f"{label} count {count} exceeds {setting_name} {limit}")
