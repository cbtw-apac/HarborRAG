from __future__ import annotations

import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from typing import Mapping
from urllib.parse import urlparse


def require_same_origin_url(url: str, base_url: str, *, label: str) -> str:
    """Validate absolute URLs before sending authenticated requests.

    Connectors often receive download links from source APIs. Relative links are
    safe to join against the configured base URL, but absolute links must stay on
    the same scheme, host, and port to avoid leaking credentials to another
    origin.
    """
    if not url.startswith(("http://", "https://")):
        return url
    if not same_origin(url, base_url):
        raise ValueError(f"Unsafe {label} URL outside trusted origin: {url}")
    return url


def same_origin(url: str, base_url: str) -> bool:
    """Return whether two URLs share scheme, host, and effective port."""
    parsed = urlparse(url)
    base = urlparse(base_url)
    return (
        parsed.scheme.lower() == base.scheme.lower()
        and (parsed.hostname or "").lower() == (base.hostname or "").lower()
        and _port(parsed) == _port(base)
    )


def retry_delay_seconds(
    headers: Mapping[str, str] | HTTPResponse | None,
    fallback_delay: float,
) -> float:
    """Choose a retry delay from provider headers or a fallback backoff.

    HTTP providers commonly return either ``Retry-After`` or
    ``X-RateLimit-Reset``. This helper centralizes that interpretation so every
    connector sleeps consistently when it is throttled.
    """
    if not headers:
        return fallback_delay

    retry_after = _header(headers, "Retry-After")
    if retry_after:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return parsed

    reset_at = _header(headers, "X-RateLimit-Reset")
    if reset_at:
        try:
            return max(0.0, float(reset_at) - time.time())
        except ValueError:
            return fallback_delay

    return fallback_delay


def _header(headers: Mapping[str, str] | HTTPResponse, name: str) -> str | None:
    value = headers.get(name) if hasattr(headers, "get") else None
    return str(value).strip() if value else None


def _parse_retry_after(value: str) -> float | None:
    try:
        return max(0.0, float(value))
    except ValueError:
        retry_after_date = value

    try:
        retry_at = parsedate_to_datetime(retry_after_date)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(tz=UTC)).total_seconds())


def _port(parsed) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme.lower() == "https":
        return 443
    if parsed.scheme.lower() == "http":
        return 80
    return None
