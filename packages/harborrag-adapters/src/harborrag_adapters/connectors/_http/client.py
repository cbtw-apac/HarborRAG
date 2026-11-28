"""The retry loop and error mapping every HTTP connector client shares.

A provider client subclasses :class:`RetryingHttpClient` and supplies only how
one attempt is sent (session, credentials, throttling) and its
:class:`RetryPolicy`. Nothing here knows a provider: no URLs, credentials or
payload shapes. The loop retries transient failures only -- connection errors,
HTTP 429 and 5xx -- a bounded number of times, waits at least as long as
``Retry-After`` asks, and turns the final response into one typed connector
error. That error never names the request URL, chains an exception that does,
or shows a credential the subclass reports in ``_known_secrets``.

Start from: RetryingHttpClient.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict, Unpack

import requests

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
    ItemNotFoundError,
    RateLimitError,
)
from harborrag_adapters.connectors.policies.http import (
    retry_delay_seconds,
    safe_response_error_detail,
)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RequestOptions(TypedDict, total=False):
    """The ``requests`` options connector clients send with a request."""

    params: Mapping[str, Any] | None
    json: Any  # any JSON value; requests serialises it
    data: Mapping[str, str]
    headers: Mapping[str, str]
    stream: bool
    allow_redirects: bool


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """How often, and how long, one request may be retried.

    Attributes:
        max_retries: Retries after the first attempt; zero sends once.
        backoff_factor: Seconds before the first retry when the response names
            no wait; doubles for each later retry.
        max_retry_after_seconds: A ``Retry-After`` longer than this ends the
            request instead of waiting; ``None`` always waits, capped by
            ``retry_delay_seconds``.
    """

    max_retries: int
    backoff_factor: float
    max_retry_after_seconds: float | None = None


class RetryingHttpClient:
    """Base for connector HTTP clients: bounded retries of transient failures, typed errors.

    Subclasses implement ``_send`` and ``_retry_policy`` and may override
    ``_rate_limited``, ``_refresh_credentials`` and ``_known_secrets``.

    Args:
        label: Provider name used in error messages and logs, such as ``"GitHub"``.
        logger: Logger that records retries.
    """

    def __init__(self, *, label: str, logger: logging.Logger) -> None:
        self._label = label
        self._logger = logger

    def request(
        self, method: str, url: str, **options: Unpack[RequestOptions]
    ) -> requests.Response:
        """Send one request, retrying transient failures, and return the final response.

        A 401 is resent once with fresh credentials when ``_refresh_credentials``
        allows it; that resend does not use up a retry. The body of a refused
        answer, a 4xx other than 429, is read once and serves both
        ``_rate_limited`` and the raised error.

        Args:
            method: HTTP method, such as ``"GET"``.
            url: Absolute URL the subclass has already checked.
            **options: Passed unchanged to ``requests`` on every attempt.

        Returns:
            The first response below HTTP 400 that is not retried.

        Raises:
            AuthenticationError: HTTP 401, after the credential refresh if any.
            AuthorizationError: HTTP 403 that is not a rate limit.
            ItemNotFoundError: HTTP 404.
            RateLimitError: Still rate limited after the last retry, or asked to
                wait longer than ``max_retry_after_seconds``.
            FetchError: Any other HTTP error, or the connection kept failing. A
                connection failure names only the exception type, never the URL.
        """
        policy = self._retry_policy()
        refreshed = False
        for attempt in range(policy.max_retries + 1):
            final = attempt == policy.max_retries
            try:
                response = self._send(method, url, **options)
                if response.status_code == 401 and not refreshed and self._refresh_credentials():
                    refreshed = True
                    response.close()
                    response = self._send(method, url, **options)
            except requests.RequestException as exc:
                if final:
                    raise FetchError(
                        f"{self._label} request failed: {type(exc).__name__}"
                    ) from None
                self._wait(policy, attempt, exc)
                continue

            refusal = self._refusal_detail(response)
            rate_limited = self._rate_limited(response, refusal or "")
            transient = rate_limited or response.status_code in RETRYABLE_STATUS
            if transient and not final and not self._retry_after_too_long(response, policy):
                error = (RateLimitError if rate_limited else FetchError)(
                    f"{self._label} request returned HTTP {response.status_code}"
                )
                headers = response.headers
                response.close()
                self._wait(policy, attempt, error, headers)
                continue
            self._raise_for_final(response, rate_limited=rate_limited, detail=refusal)
            return response
        raise FetchError(f"{self._label} request failed")

    def _send(self, method: str, url: str, **options: Unpack[RequestOptions]) -> requests.Response:
        """Send one attempt; subclasses add credentials and throttling."""
        raise NotImplementedError

    def _retry_policy(self) -> RetryPolicy:
        """Return the retry policy for the next request, read from the client's config."""
        raise NotImplementedError

    def _rate_limited(self, response: requests.Response, detail: str) -> bool:
        """Return whether ``response`` asks the client to slow down.

        Args:
            response: Response whose status and headers are checked.
            detail: Redacted body of a refused answer, already read because a
                streamed body reads only once; empty for any other response.
        """
        return response.status_code == 429

    def _refresh_credentials(self) -> bool:
        """Drop cached credentials after a 401; return ``True`` to resend once."""
        return False

    def _known_secrets(self) -> Iterable[str | None]:
        """Return the credential values sent with requests, kept out of error text."""
        return ()

    def _error_detail(self, response: requests.Response) -> str:
        """Read a bounded, redacted excerpt of an error body and close the response."""
        return safe_response_error_detail(response, known_secrets=self._known_secrets())

    def _refusal_detail(self, response: requests.Response) -> str | None:
        """Read the body of a 4xx other than 429, which is not retried for its status."""
        status = response.status_code
        if status < 400 or status in RETRYABLE_STATUS:
            return None
        return self._error_detail(response)

    def _raise_for_final(
        self, response: requests.Response, *, rate_limited: bool, detail: str | None
    ) -> None:
        """Raise the typed error of a response that is not retried; return below HTTP 400."""
        if response.status_code < 400:
            return
        text = self._error_detail(response) if detail is None else detail
        if rate_limited:
            raise RateLimitError(text, status_code=response.status_code)
        raise_for_status(response, label=self._label, detail=text)

    def _retry_after_too_long(self, response: requests.Response, policy: RetryPolicy) -> bool:
        limit = policy.max_retry_after_seconds
        if limit is None:
            return False
        requested = retry_delay_seconds(response.headers, 0.0, max_delay=math.inf, jitter=False)
        if requested <= limit:
            return False
        self._logger.warning(
            "%s asked to retry after %.0fs, beyond the %.0fs retry wait; not retrying",
            self._label,
            requested,
            limit,
        )
        return True

    def _wait(
        self,
        policy: RetryPolicy,
        attempt: int,
        error: Exception,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Sleep before the next attempt, honouring the provider's retry headers."""
        delay = retry_delay_seconds(headers, policy.backoff_factor * (2**attempt))
        self._logger.warning(
            "Retrying %s request after %s, attempt %d/%d",
            self._label,
            type(error).__name__,
            attempt + 1,
            policy.max_retries,
        )
        if delay > 0:
            time.sleep(delay)


def raise_for_status(response: requests.Response, *, label: str, detail: str | None = None) -> None:
    """Raise the typed connector error for a final response at or above HTTP 400.

    The error carries the HTTP status and a bounded, redacted excerpt of the
    response body.

    Args:
        response: Final response; one below HTTP 400 is left alone.
        label: Provider name used in the error message.
        detail: Redacted body already read from ``response``; read here when ``None``.

    Raises:
        AuthenticationError: HTTP 401.
        AuthorizationError: HTTP 403.
        ItemNotFoundError: HTTP 404.
        FetchError: Any other status at or above 400.
    """
    status = response.status_code
    if status < 400:
        return
    if detail is None:
        detail = safe_response_error_detail(response)
    if status == 401:
        raise AuthenticationError(detail, status_code=401)
    if status == 403:
        raise AuthorizationError(detail, status_code=403)
    if status == 404:
        raise ItemNotFoundError(
            f"{label} request failed with HTTP 404: {detail}", status_code=404, detail=detail
        )
    raise FetchError(
        f"{label} request failed with HTTP {status}: {detail}", status_code=status, detail=detail
    )
