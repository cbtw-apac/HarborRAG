"""GitHub REST client protocol and its requests-based implementation.

Handles:
- Authenticated, rate-limited GET requests
- Retries with provider retry headers
- Capped JSON decoding and error classification
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.policies.http import (
    DEFAULT_JSON_BODY_LIMIT,
    ResponseTooLargeError,
    read_capped_json,
    require_same_origin_url,
    retry_delay_seconds,
    safe_response_error_detail,
)

from .config import GitHubRepositoryConfig

logger = logging.getLogger("harborrag.adapters.connectors.github")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GitHubClient(Protocol):
    """API surface ``GitHubConnector`` needs from a GitHub client."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET an endpoint and return the decoded JSON object or list of objects.

        Args:
            endpoint: API path relative to the API URL, or a same-origin absolute URL.
            params: Query parameters.
            max_bytes: Response body cap in bytes; defaults to the shared JSON limit.

        Raises:
            FetchError: If the request fails or the body is not valid JSON of that shape.
            AuthenticationError: If GitHub rejects the credentials.
            RateLimitError: If rate limiting persists after the retries.
        """
        ...

    def close(self) -> None:
        """Release any HTTP resources held by the client."""
        ...


class _RequestsGitHubClient:
    """Requests-based GitHub REST client with local rate limiting and retries.

    Owns its HTTP session; ``close`` releases it.
    """

    def __init__(self, config: GitHubRepositoryConfig) -> None:
        """Create the HTTP session with GitHub API headers and the configured token.

        Args:
            config: Validated repository config.
        """
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": config.api_version,
            }
        )
        if config.token:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def close(self) -> None:
        """Close the underlying HTTP session."""
        self.session.close()

    def __enter__(self) -> _RequestsGitHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """GET an endpoint and return the decoded JSON object or list of objects.

        Args:
            endpoint: API path relative to the API URL, or a same-origin absolute URL.
            params: Query parameters.
            max_bytes: Response body cap in bytes; defaults to the shared JSON limit.

        Raises:
            FetchError: If the request fails, the URL is cross-origin, or the body is too
                large or not valid JSON of that shape.
            AuthenticationError: If GitHub answers 401 or a non-rate-limit 403.
            RateLimitError: If rate limiting persists after the retries.
        """
        response = self._request("GET", self._api_url(endpoint), params=params, stream=True)
        try:
            payload = read_capped_json(
                response,
                max_bytes=DEFAULT_JSON_BODY_LIMIT if max_bytes is None else max_bytes,
            )
        except ResponseTooLargeError as exc:
            raise FetchError(f"GitHub response exceeded byte limit for {endpoint}") from exc
        except ValueError as exc:
            raise FetchError(f"GitHub returned non-JSON response for {endpoint}") from exc
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, list):
            items: list[dict[str, Any]] = []
            for item in payload:
                if not isinstance(item, dict):
                    raise FetchError(f"GitHub returned invalid JSON for {endpoint}")
                items.append(item)
            return items
        raise FetchError(f"GitHub returned invalid JSON for {endpoint}")

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send a request with local throttling, retrying transient and rate-limit errors."""
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=self.config.request_timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise FetchError("GitHub request failed") from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401,):
                raise AuthenticationError(safe_response_error_detail(response))

            if response.status_code >= 400:
                # A streamed `requests.Response` body can only be read once.
                detail = safe_response_error_detail(response, limit=2_000)
                if self._rate_limited(response, detail):
                    if attempt == self.config.max_retries:
                        raise RateLimitError(detail)
                    last_error = RateLimitError("GitHub rate limit exceeded")
                    retry_headers = response.headers
                    response.close()
                    self._sleep(attempt, last_error, retry_headers, throttled=True)
                    continue
                if response.status_code == 403:
                    raise AuthenticationError(detail)
                if (
                    response.status_code not in _RETRYABLE_STATUS
                    or attempt == self.config.max_retries
                ):
                    raise FetchError(
                        f"GitHub request failed with HTTP {response.status_code}: {detail}"
                    )

                last_error = FetchError(f"GitHub request returned HTTP {response.status_code}")
                retry_headers = response.headers
                response.close()
                self._sleep(attempt, last_error, retry_headers)
                continue

            return response

        raise FetchError("GitHub request failed") from last_error

    def _api_url(self, endpoint: str) -> str:
        """Return the full API URL. Raises ``FetchError`` for a cross-origin absolute URL."""
        if endpoint.startswith(("http://", "https://")):
            try:
                return require_same_origin_url(
                    endpoint,
                    self.config.api_url,
                    label="GitHub API",
                )
            except ValueError as exc:
                raise FetchError(str(exc)) from exc
        return f"{self.config.api_url}/{endpoint.lstrip('/')}"

    @staticmethod
    def _rate_limited(response: requests.Response, detail: str) -> bool:
        """Return whether a failed response is a rate limit, by status, headers or ``detail``."""
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if response.headers.get("Retry-After"):
            return True
        body = detail.lower()
        return "secondary rate limit" in body or "abuse detection" in body

    def _acquire(self) -> None:
        """Sleep so requests stay at least ``60 / requests_per_minute`` seconds apart."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(
        self,
        attempt: int,
        error: Exception,
        headers: Any = None,
        *,
        throttled: bool = False,
    ) -> None:
        """Sleep before a retry, preferring provider retry headers over exponential backoff."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay, throttled=throttled)
        logger.warning(
            "Retrying GitHub request after %s, attempt %d/%d",
            type(error).__name__,
            attempt + 1,
            self.config.max_retries,
        )
        if delay > 0:
            time.sleep(delay)
