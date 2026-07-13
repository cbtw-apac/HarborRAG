from __future__ import annotations

import random
import time
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from typing import Protocol
from urllib.parse import ParseResult, urlparse

from harborrag_core.security.redaction import redact_secrets

DEFAULT_MAX_RETRY_DELAY_SECONDS = 300.0
_ALLOWED_SCHEMES = frozenset({"http", "https"})

DEFAULT_ERROR_BODY_LIMIT = 500


class StreamingResponse(Protocol):
    """Minimal response surface required by :func:`read_capped_content`."""

    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield response body chunks."""
        ...

    def close(self) -> None:
        """Release the response connection."""
        ...


def safe_error_detail(
    text: str | None, *, limit: int = DEFAULT_ERROR_BODY_LIMIT
) -> str:
    """Return a truncated, secret-redacted snippet of a response body."""
    if not text:
        return ""
    text = redact_secrets(text)
    text = text.strip().replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}… (truncated)"
    return text


def require_same_origin_url(url: str, base_url: str, *, label: str) -> str:
    """Validate absolute URLs before sending authenticated requests.

    Connectors often receive download links from source APIs. Relative links are
    safe to join against the configured base URL, but absolute links must stay on
    the same scheme, host, and port to avoid leaking credentials to another
    origin. Absoluteness is decided by parsing (not a case-sensitive prefix
    check), and any non-http(s) scheme is rejected outright.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsafe {label} URL scheme: {url}")
    if not same_origin(url, base_url):
        raise ValueError(f"Unsafe {label} URL outside trusted origin: {url}")
    return url


class ResponseTooLargeError(ValueError):
    """Raised when a streamed response body exceeds the configured byte cap."""


def read_capped_content(
    response: StreamingResponse,
    max_bytes: int | None,
    *,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a ``requests`` response body while enforcing a hard byte ceiling.

    Streaming with an incremental cap bounds peak memory even when the provider
    misreports (or omits) the size and when ``Content-Length`` is absent. The
    read is aborted — and the connection released — as soon as the cap is
    exceeded, so a hostile/unbounded body can never be fully buffered.
    """
    if max_bytes is not None:
        declared = response.headers.get("Content-Length")
        if declared is not None:
            try:
                declared_len = int(declared)
            except ValueError:
                declared_len = None
            if declared_len is not None and declared_len > max_bytes:
                response.close()
                raise ResponseTooLargeError(
                    f"Content-Length {declared} exceeds cap {max_bytes}"
                )

    buffer = bytearray()
    for chunk in response.iter_content(chunk_size=chunk_size):
        if not chunk:
            continue
        buffer.extend(chunk)
        if max_bytes is not None and len(buffer) > max_bytes:
            response.close()
            raise ResponseTooLargeError(
                f"Downloaded body exceeds cap {max_bytes} bytes"
            )
    return bytes(buffer)


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
    *,
    max_delay: float = DEFAULT_MAX_RETRY_DELAY_SECONDS,
    jitter: bool = True,
) -> float:
    """Choose a retry delay from provider headers or a fallback backoff.

    HTTP providers commonly return either ``Retry-After`` or
    ``X-RateLimit-Reset``. This helper centralizes that interpretation so every
    connector sleeps consistently when it is throttled. The result is always
    clamped to ``max_delay`` (defending against hostile headers) and, when
    ``jitter`` is set, spread by a small random factor to avoid a thundering
    herd of synchronized retries across workers.
    """
    delay = _raw_delay(headers, fallback_delay)
    delay = max(0.0, min(delay, max_delay))
    if jitter and delay > 0:
        delay += random.uniform(0.0, min(1.0, delay * 0.1))
    return max(0.0, min(delay, max_delay))


def _raw_delay(
    headers: Mapping[str, str] | HTTPResponse | None,
    fallback_delay: float,
) -> float:
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
    if hasattr(headers, "get"):
        value = headers.get(name)
    elif hasattr(headers, "getheader"):
        value = headers.getheader(name)
    else:
        value = None
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


def _port(parsed: ParseResult) -> int | None:
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme.lower() == "https":
        return 443
    if parsed.scheme.lower() == "http":
        return 80
    return None
