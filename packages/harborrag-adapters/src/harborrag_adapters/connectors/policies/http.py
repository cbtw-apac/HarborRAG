"""HTTP helpers shared by connector clients.

Handles:
- Capped response body reads
- Redacted, truncated error details
- Same-origin URL checks
- Retry delays from provider headers
- Credential probe error normalization
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.client import HTTPResponse
from typing import Protocol
from urllib.parse import ParseResult, urlparse

from harborrag_core.security.redaction import redact_secrets

from ..exceptions import AuthenticationError, AuthorizationError, FetchError

DEFAULT_MAX_RETRY_DELAY_SECONDS = 300.0
_ALLOWED_SCHEMES = frozenset({"http", "https"})

DEFAULT_ERROR_BODY_LIMIT = 500
DEFAULT_ERROR_BODY_READ_LIMIT = 8 * 1024
DEFAULT_JSON_BODY_LIMIT = 8 * 1024 * 1024


class StreamingResponse(Protocol):
    """Response surface required by ``read_capped_content``."""

    @property
    def headers(self) -> Mapping[str, str]:
        """Return the response headers."""
        ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield response body chunks of up to ``chunk_size`` bytes."""
        ...

    def close(self) -> None:
        """Release the response connection."""
        ...


def safe_error_detail(text: str | None, *, limit: int = DEFAULT_ERROR_BODY_LIMIT) -> str:
    """Return a response body snippet with secrets redacted, truncated to ``limit`` characters."""
    if not text:
        return ""
    text = redact_secrets(text)
    text = text.strip().replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}… (truncated)"
    return text


def summarize_provider_error(exc: Exception) -> str:
    """Reduce a connector exception to one diagnostic line.

    Prefers the exception's ``detail`` over ``str(exc)``. If that is a JSON object with
    a string ``message``, returns the message; otherwise returns the detail unchanged.

    Args:
        exc: Exception, usually a ``FetchError``.
    """
    detail = getattr(exc, "detail", None)
    detail = detail if isinstance(detail, str) and detail else str(exc)
    try:
        payload = json.loads(detail)
    except ValueError:
        return detail
    if not isinstance(payload, dict):
        return detail
    message = payload.get("message")
    return message if isinstance(message, str) and message.strip() else detail


def _with_status_code(message: str, status_code: int | None) -> str:
    """Append ``(HTTP <status>)`` to the message when a status code is known."""
    return message if status_code is None else f"{message} (HTTP {status_code})"


def verify_credentials(probe: Callable[[], object], *, provider: str) -> None:
    """Run an authenticated probe and re-raise its failures with a uniform message.

    Authentication, authorization and bare 403 ``FetchError`` failures are re-raised as
    ``"<provider> authentication|authorization failed: ... (HTTP <status>)"``.

    Args:
        probe: Cheap authenticated request to run.
        provider: Provider name used in the message.

    Raises:
        AuthenticationError: If the credentials are rejected.
        AuthorizationError: If the credentials lack permission, including a bare 403.
        FetchError: For any other fetch failure, unchanged.
    """
    try:
        probe()
    except AuthenticationError as exc:
        raise AuthenticationError(
            _with_status_code(
                f"{provider} authentication failed: {summarize_provider_error(exc)}",
                exc.status_code,
            ),
            status_code=exc.status_code,
        ) from exc
    except AuthorizationError as exc:
        raise AuthorizationError(
            _with_status_code(
                f"{provider} authorization failed: {summarize_provider_error(exc)}",
                exc.status_code,
            ),
            status_code=exc.status_code,
        ) from exc
    except FetchError as exc:
        if exc.status_code == 403:
            raise AuthorizationError(
                _with_status_code(
                    f"{provider} authorization failed: {summarize_provider_error(exc)}",
                    exc.status_code,
                ),
                status_code=exc.status_code,
            ) from exc
        raise


def require_same_origin_url(url: str, base_url: str, *, label: str) -> str:
    """Return ``url`` if it is relative or on the same origin as ``base_url``.

    Absolute URLs must share scheme, host and port, so credentials never go to another
    origin; non-http(s) schemes are rejected.

    Args:
        url: Link received from a source API.
        base_url: Configured source base URL.
        label: Name of the URL kind used in error messages.

    Raises:
        ValueError: If the scheme is not http(s) or the origin differs.
    """
    parsed = urlparse(url)
    if not parsed.scheme:
        return url
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsafe {label} URL scheme")
    if not same_origin(url, base_url):
        raise ValueError(f"Unsafe {label} URL outside trusted origin")
    return url


class ResponseTooLargeError(ValueError):
    """Raised when a streamed response body exceeds the configured byte cap."""


def read_capped_content(
    response: StreamingResponse,
    max_bytes: int | None,
    *,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a streamed response body, aborting once it exceeds ``max_bytes``.

    Checks ``Content-Length`` first, then counts streamed bytes. Always closes the response.

    Args:
        response: Streaming response to read.
        max_bytes: Byte cap, or ``None`` for no cap.
        chunk_size: Bytes per streamed chunk.

    Returns:
        The full body.

    Raises:
        ResponseTooLargeError: If the body exceeds ``max_bytes``.
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
                raise ResponseTooLargeError(f"Content-Length {declared} exceeds cap {max_bytes}")

    buffer = bytearray()
    try:
        for chunk in response.iter_content(chunk_size=chunk_size):
            if not chunk:
                continue
            if max_bytes is not None and len(buffer) + len(chunk) > max_bytes:
                raise ResponseTooLargeError(f"Downloaded body exceeds cap {max_bytes} bytes")
            buffer.extend(chunk)
        return bytes(buffer)
    finally:
        response.close()


def read_capped_json(
    response: StreamingResponse,
    *,
    max_bytes: int = DEFAULT_JSON_BODY_LIMIT,
) -> object:
    """Read a response body under the byte cap and decode it as JSON.

    Args:
        response: Streaming response to read.
        max_bytes: Byte cap for the body.

    Raises:
        ResponseTooLargeError: If the body exceeds ``max_bytes``.
        ValueError: If the body is not valid JSON.
    """
    body = read_capped_content(response, max_bytes)
    return json.loads(body)


def safe_response_error_detail(
    response: StreamingResponse,
    *,
    limit: int = DEFAULT_ERROR_BODY_LIMIT,
) -> str:
    """Return a redacted snippet of an error response body.

    Reads at most 8 KiB; a larger body yields a fixed placeholder message.

    Args:
        response: Error response to read.
        limit: Maximum characters in the snippet.
    """
    try:
        body = read_capped_content(response, DEFAULT_ERROR_BODY_READ_LIMIT)
    except ResponseTooLargeError:
        return "response body exceeded safe diagnostic limit"
    return safe_error_detail(body.decode("utf-8", errors="replace"), limit=limit)


def same_origin(url: str, base_url: str) -> bool:
    """Return whether two URLs share scheme, host and effective port."""
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
    throttled: bool = False,
) -> float:
    """Choose a retry delay from ``Retry-After``, the rate-limit reset, or a fallback.

    ``X-RateLimit-Reset`` applies only when the response is ``throttled`` or
    ``X-RateLimit-Remaining`` is ``0``.

    Args:
        headers: Response headers, or ``None``.
        fallback_delay: Delay in seconds when no header applies.
        max_delay: Upper bound on the result in seconds.
        jitter: Add up to 10% (at most 1 second) of random spread.
        throttled: Caller classified the response as rate limited.

    Returns:
        Delay in seconds, between 0 and ``max_delay``.
    """
    delay = _raw_delay(headers, fallback_delay, throttled=throttled)
    delay = max(0.0, min(delay, max_delay))
    if jitter and delay > 0:
        delay += random.uniform(0.0, min(1.0, delay * 0.1))
    return max(0.0, min(delay, max_delay))


def _raw_delay(
    headers: Mapping[str, str] | HTTPResponse | None,
    fallback_delay: float,
    *,
    throttled: bool,
) -> float:
    """Return the unclamped delay: ``Retry-After``, then ``X-RateLimit-Reset``, else fallback."""
    if not headers:
        return fallback_delay

    retry_after = _header(headers, "Retry-After")
    if retry_after:
        parsed = _parse_retry_after(retry_after)
        if parsed is not None:
            return parsed

    if not throttled and _header(headers, "X-RateLimit-Remaining") != "0":
        return fallback_delay

    reset_at = _header(headers, "X-RateLimit-Reset")
    if reset_at:
        try:
            return max(0.0, float(reset_at) - time.time())
        except ValueError:
            return fallback_delay

    return fallback_delay


def _header(headers: Mapping[str, str] | HTTPResponse, name: str) -> str | None:
    """Return a stripped header value from a mapping or ``HTTPResponse``, or ``None``."""
    if hasattr(headers, "get"):
        value = headers.get(name)
    elif hasattr(headers, "getheader"):
        value = headers.getheader(name)
    else:
        value = None
    return str(value).strip() if value else None


def _parse_retry_after(value: str) -> float | None:
    """Return ``Retry-After`` (seconds or HTTP date) as seconds from now, else ``None``.

    A date without a timezone is read as UTC; past dates give ``0.0``.
    """
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
