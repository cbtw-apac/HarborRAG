"""Retry, size-cap, origin and error-text policies shared by every connector.

Error text built here can reach logs, run status and API responses, so it is
redacted and truncated before it leaves this module.
"""

from __future__ import annotations

import json
import random
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
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

_REDACTED = "<redacted>"
_MIN_KNOWN_SECRET_LENGTH = 4


class StreamingResponse(Protocol):
    """Minimal response surface required by :func:`read_capped_content`."""

    @property
    def headers(self) -> Mapping[str, str]:
        """Read-only response headers compatible with covariant SDK mappings."""
        ...

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield response body chunks."""
        ...

    def close(self) -> None:
        """Release the response connection."""
        ...


def safe_error_detail(
    text: str | None,
    *,
    limit: int = DEFAULT_ERROR_BODY_LIMIT,
    known_secrets: Iterable[str | None] = (),
) -> str:
    """Return a truncated, secret-redacted snippet of a response body.

    Generic redaction only finds labelled values and well-known token shapes.
    Each known secret is also replaced wherever it appears, longest first and
    before truncation, so no part of it survives.

    Args:
        text: Response body or provider message, or ``None`` when there is none.
        limit: Maximum characters kept before the truncation marker.
        known_secrets: Credential values sent with the request. ``None`` and
            values shorter than four characters are skipped, because replacing
            them would blank ordinary words in the message.

    Returns:
        One line of redacted text, or an empty string when ``text`` is empty.
    """
    if not text:
        return ""
    text = redact_secrets(_remove_known_secrets(text, known_secrets), replacement=_REDACTED)
    text = text.strip().replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}… (truncated)"
    return text


def _remove_known_secrets(text: str, known_secrets: Iterable[str | None]) -> str:
    """Replace each known secret, longest first, so none is left partly shown."""
    values = {value for value in known_secrets if value and len(value) >= _MIN_KNOWN_SECRET_LENGTH}
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, _REDACTED)
    return text


def summarize_provider_error(exc: Exception) -> str:
    """Reduce a connector exception to one diagnostic line for re-raising.

    The provider body kept on a `FetchError` is preferred over the exception
    text, which can repeat the HTTP status. When that body is a JSON object with
    a non-blank string ``message``, only the message is kept. Any other body,
    including JSON cut short by truncation, is returned unchanged.

    Args:
        exc: Exception raised by a connector request.

    Returns:
        The provider's message, or the raw detail when it has none.
    """
    detail = exc.detail if isinstance(exc, FetchError) and exc.detail else str(exc)
    try:
        payload = json.loads(detail)
    except ValueError:
        return detail
    if not isinstance(payload, dict):
        return detail
    message = payload.get("message")
    return message if isinstance(message, str) and message.strip() else detail


def _with_status_code(message: str, status_code: int | None) -> str:
    """Append the HTTP status, since a provider's error body may not mention it."""
    return message if status_code is None else f"{message} (HTTP {status_code})"


def verify_credentials(probe: Callable[[], object], *, provider: str) -> None:
    """Run a cheap authenticated probe and normalize how it fails.

    A rejected credential is reported as an authentication failure. A 403,
    including a bare 403 `FetchError`, means the credential is valid but lacks
    permission, so it is reported as an authorization failure. Each message
    starts with the provider name, so every connector reports failures alike.

    Args:
        probe: Callable that sends one authenticated request to the provider.
        provider: Provider name that starts each error message.

    Raises:
        AuthenticationError: The provider rejected the credential.
        AuthorizationError: The credential lacks permission for the probed resource.
        FetchError: The probe failed for any other reason.
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
    """Return a source-supplied URL only when it keeps the configured origin.

    Source APIs return links that are fetched with the connector's credentials.
    A path-relative link, with neither scheme nor host, is returned for the
    caller to join to ``base_url``. A link that names a host must use http or
    https on the scheme, host and port of ``base_url``, so a scheme-relative
    ``//host/path`` link is refused.

    Args:
        url: Link taken from a source response.
        base_url: Configured base URL whose origin the link must keep.
        label: Short name of the link's purpose, used in the error message.

    Returns:
        ``url`` unchanged.

    Raises:
        ValueError: ``url`` has a scheme other than http or https, names a
            host without a scheme, or points outside the origin of ``base_url``.
    """
    parsed = urlparse(url)
    if not parsed.scheme and not parsed.netloc:
        return url
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(f"Unsafe {label} URL scheme")
    if not same_origin(url, base_url):
        raise ValueError(f"Unsafe {label} URL outside trusted origin")
    return url


class ResponseTooLargeError(ValueError):
    """A streamed response body is larger than the configured byte cap."""


def read_capped_content(
    response: StreamingResponse,
    max_bytes: int | None,
    *,
    chunk_size: int = 64 * 1024,
) -> bytes:
    """Read a streamed response body while enforcing a hard byte ceiling.

    The cap is checked as each chunk arrives, so peak memory stays bounded when
    ``Content-Length`` is missing or wrong. A declared length over the cap is
    refused before reading. The response is closed in every case.

    Args:
        response: Streamed response to read.
        max_bytes: Largest body accepted, or ``None`` for no cap.
        chunk_size: Bytes requested per read.

    Returns:
        The whole response body.

    Raises:
        ResponseTooLargeError: The declared or downloaded body is over ``max_bytes``.
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
    """Decode a JSON response body after reading it under a byte cap.

    Args:
        response: Streamed response to read.
        max_bytes: Largest body accepted.

    Returns:
        The decoded JSON value.

    Raises:
        ResponseTooLargeError: The body is over ``max_bytes``.
        ValueError: The body is not valid JSON.
    """
    body = read_capped_content(response, max_bytes)
    return json.loads(body)


def safe_response_error_detail(
    response: StreamingResponse,
    *,
    limit: int = DEFAULT_ERROR_BODY_LIMIT,
    known_secrets: Iterable[str | None] = (),
) -> str:
    """Read a small prefix of an error body, then redact and truncate it.

    Args:
        response: Error response. It is closed after the read.
        limit: Maximum characters kept before the truncation marker.
        known_secrets: Credential values sent with the request, replaced as
            `safe_error_detail` does.

    Returns:
        The redacted detail, or a fixed message when the body is over the read cap.
    """
    try:
        body = read_capped_content(response, DEFAULT_ERROR_BODY_READ_LIMIT)
    except ResponseTooLargeError:
        return "response body exceeded safe diagnostic limit"
    return safe_error_detail(
        body.decode("utf-8", errors="replace"), limit=limit, known_secrets=known_secrets
    )


def same_origin(url: str, base_url: str) -> bool:
    """Return whether two URLs share scheme, host and effective port.

    Scheme and host compare case-insensitively. An http or https URL without a
    port counts as using that scheme's default port.

    Args:
        url: URL to check.
        base_url: URL whose origin ``url`` must match.

    Returns:
        ``True`` when both URLs have the same origin.
    """
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

    ``Retry-After`` is read first, then ``X-RateLimit-Reset``. The result is
    clamped to ``max_delay`` so a hostile header cannot stall a worker. Jitter
    keeps many workers from retrying at the same moment.

    Args:
        headers: Response headers, or ``None`` when there is no response.
        fallback_delay: Seconds to wait when no header gives a usable delay.
        max_delay: Largest delay returned, in seconds.
        jitter: Add a random delay of up to 10 percent, at most one second.

    Returns:
        Seconds to wait before the next attempt, from 0 to ``max_delay``.
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
    """Return the delay the headers ask for, before clamping and jitter."""
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
    """Read one stripped header from a mapping or an object with ``getheader``."""
    if hasattr(headers, "get"):
        value = headers.get(name)
    elif hasattr(headers, "getheader"):
        value = headers.getheader(name)
    else:
        value = None
    return str(value).strip() if value else None


def _parse_retry_after(value: str) -> float | None:
    """Parse ``Retry-After`` as seconds or an HTTP date, or return ``None``."""
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
    """Return the explicit port, or the default port of an http or https URL."""
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme.lower() == "https":
        return 443
    if parsed.scheme.lower() == "http":
        return 80
    return None
