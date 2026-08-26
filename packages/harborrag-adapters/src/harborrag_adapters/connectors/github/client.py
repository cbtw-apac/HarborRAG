"""GitHub client protocol and requests-based implementation."""

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
    """Small API surface needed by ``GitHubConnector``."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        """Return a decoded GitHub response object or object list."""
        ...

    def close(self) -> None:
        """Release any HTTP resources held by the client."""
        ...


class _RequestsGitHubClient:
    """Authenticated, rate-limited GitHub REST client."""

    def __init__(self, config: GitHubRepositoryConfig) -> None:
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
        """GET a GitHub API endpoint and decode its JSON body."""
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
        """Send one HTTP request with local rate limiting and retry handling."""
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
            if self._rate_limited(response):
                if attempt == self.config.max_retries:
                    raise RateLimitError(safe_response_error_detail(response))
                last_error = RateLimitError("GitHub rate limit exceeded")
                retry_headers = response.headers
                response.close()
                self._sleep(attempt, last_error, retry_headers)
                continue
            if response.status_code == 403:
                raise AuthenticationError(safe_response_error_detail(response))
            if response.status_code not in _RETRYABLE_STATUS or attempt == self.config.max_retries:
                if response.status_code >= 400:
                    raise FetchError(
                        f"GitHub request failed with HTTP "
                        f"{response.status_code}: {safe_response_error_detail(response)}"
                    )
                return response

            last_error = FetchError(f"GitHub request returned HTTP {response.status_code}")
            retry_headers = response.headers
            response.close()
            self._sleep(attempt, last_error, retry_headers)

        raise FetchError("GitHub request failed") from last_error

    def _api_url(self, endpoint: str) -> str:
        """Build a GitHub API URL while rejecting cross-origin absolute URLs."""
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
    def _rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        if response.headers.get("Retry-After"):
            return True
        body = safe_response_error_detail(response, limit=2_000).lower()
        return "secondary rate limit" in body or "abuse detection" in body

    def _acquire(self) -> None:
        """Throttle requests according to the configured per-minute budget."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before retrying, honoring provider retry headers when present."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        logger.warning(
            "Retrying GitHub request after %s, attempt %d/%d",
            type(error).__name__,
            attempt + 1,
            self.config.max_retries,
        )
        if delay > 0:
            time.sleep(delay)
