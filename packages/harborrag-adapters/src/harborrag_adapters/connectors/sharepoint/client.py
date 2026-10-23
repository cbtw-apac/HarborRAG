"""Microsoft Graph client protocol and its requests-based implementation.

Handles:
- Bearer token from config or client-credentials flow, cached until near expiry
- Authenticated, rate-limited GET requests with retries
- Capped JSON and file-byte reads

References:
- https://learn.microsoft.com/graph/overview
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Protocol

import requests

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.policies.http import (
    ResponseTooLargeError,
    read_capped_content,
    read_capped_json,
    require_same_origin_url,
    retry_delay_seconds,
    safe_response_error_detail,
)

from .config import SharePointSiteConfig

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SharePointClient(Protocol):
    """API surface ``SharePointConnector`` needs from a Microsoft Graph client."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET an endpoint and return the decoded JSON object.

        Args:
            endpoint: Graph path relative to the API URL, or a same-origin absolute URL.
            params: Query parameters.

        Raises:
            FetchError: If the request fails or the body is not a JSON object.
            AuthenticationError: If Graph answers 401 or 403.
        """
        pass

    def get_bytes(self, endpoint: str) -> bytes:
        """GET an endpoint and return its body, capped by ``max_file_size_bytes``.

        Raises:
            FetchError: If the request fails or the body exceeds the cap.
            AuthenticationError: If Graph answers 401 or 403.
        """
        pass

    def close(self) -> None:
        """Release connector-owned HTTP resources."""
        pass


class _RequestsGraphClient:
    """Requests-based Graph client with local rate limiting, retries and token caching.

    Owns its HTTP session; ``close`` releases it and drops the cached token.
    """

    def __init__(self, config: SharePointSiteConfig) -> None:
        """Create the HTTP session and throttle state. Makes no request.

        Args:
            config: Validated site config.
        """
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0
        self._rate_lock = threading.Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET an endpoint and return the decoded JSON object.

        Args:
            endpoint: Graph path relative to the API URL, or a same-origin absolute URL.
            params: Query parameters.

        Raises:
            FetchError: If the request fails, the URL is cross-origin, or the body is too
                large or not a JSON object.
            AuthenticationError: If Graph answers 401 or 403.
            RateLimitError: If throttling persists after the retries.
        """
        response = self._request("GET", self._api_url(endpoint), params=params, stream=True)
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise FetchError(f"Microsoft Graph returned non-JSON for {endpoint}") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"Microsoft Graph returned invalid JSON for {endpoint}")
        return payload

    def get_bytes(self, endpoint: str) -> bytes:
        """GET a file endpoint and return its bytes, capped by ``max_file_size_bytes``.

        Raises:
            FetchError: If the request fails or the body exceeds the cap.
            AuthenticationError: If Graph answers 401 or 403.
        """
        response = self._request(
            "GET",
            self._api_url(endpoint),
            headers={"Accept": "*/*"},
            stream=True,
        )
        try:
            return read_capped_content(response, self.config.max_file_size_bytes)
        except ResponseTooLargeError as exc:
            raise FetchError(str(exc)) from exc

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send an authenticated request with local throttling, retrying 429 and 5xx."""
        last_error: Exception | None = None
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self._access_token()}"

        for attempt in range(self.config.max_retries + 1):
            self._acquire()
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    timeout=self.config.request_timeout_seconds,
                    **kwargs,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise FetchError("Microsoft Graph request failed") from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(
                    safe_response_error_detail(response), status_code=response.status_code
                )
            if response.status_code == 429 and attempt == self.config.max_retries:
                raise RateLimitError(safe_response_error_detail(response))
            if response.status_code not in _RETRYABLE_STATUS or attempt == self.config.max_retries:
                if response.status_code >= 400:
                    raise FetchError(
                        f"Microsoft Graph request failed with HTTP "
                        f"{response.status_code}: {safe_response_error_detail(response)}"
                    )
                return response

            last_error = FetchError(f"Microsoft Graph request returned HTTP {response.status_code}")
            retry_headers = response.headers
            response.close()
            self._sleep(attempt, last_error, retry_headers)

        raise FetchError("Microsoft Graph request failed") from last_error

    def _access_token(self) -> str:
        """Return the configured token, or a cached client-credentials token renewed near expiry."""
        if self.config.access_token:
            return self.config.access_token
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token

        response = self._request_token()
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise AuthenticationError("Microsoft identity returned non-JSON token") from exc

        if not isinstance(payload, dict):
            raise AuthenticationError("Microsoft identity returned invalid JSON token")
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Microsoft identity token response missing token")
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(payload.get("expires_in") or 3599)
        return self._token

    def _request_token(self) -> requests.Response:
        """POST for an OAuth token, retrying network errors, 429 and 5xx."""
        # TODO: derive the token authority and scope from graph_api_url for national clouds.
        token_url = f"https://login.microsoftonline.com/{self.config.tenant_id}/oauth2/v2.0/token"
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.post(
                    token_url,
                    data={
                        "client_id": self.config.client_id,
                        "client_secret": self.config.client_secret,
                        "grant_type": "client_credentials",
                        "scope": "https://graph.microsoft.com/.default",
                    },
                    timeout=self.config.request_timeout_seconds,
                    stream=True,
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == self.config.max_retries:
                    raise AuthenticationError("Microsoft identity request failed") from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code not in _RETRYABLE_STATUS:
                if response.status_code >= 400:
                    raise AuthenticationError(safe_response_error_detail(response))
                return response
            if attempt == self.config.max_retries:
                raise AuthenticationError(safe_response_error_detail(response))

            last_error = AuthenticationError(
                f"Microsoft identity token request returned HTTP {response.status_code}"
            )
            headers = response.headers
            response.close()
            self._sleep(attempt, last_error, headers)

        raise AuthenticationError("Microsoft identity request failed") from last_error

    def close(self) -> None:
        """Close the HTTP session and discard the cached token."""

        self._token = None
        self._token_expires_at = 0.0
        self.session.close()

    def _api_url(self, endpoint: str) -> str:
        """Return the full Graph URL. Raises ``FetchError`` for a cross-origin absolute URL."""
        if endpoint.startswith(("http://", "https://")):
            try:
                return require_same_origin_url(
                    endpoint,
                    self.config.graph_api_url,
                    label="Microsoft Graph",
                )
            except ValueError as exc:
                raise FetchError(str(exc)) from exc
        return f"{self.config.graph_api_url}/{endpoint.lstrip('/')}"

    def _acquire(self) -> None:
        """Sleep so requests stay at least ``60 / requests_per_minute`` seconds apart."""
        with self._rate_lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before a retry, preferring provider retry headers over exponential backoff."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        logger.warning(
            "Retrying Microsoft Graph request after %s, attempt %d/%d",
            type(error).__name__,
            attempt + 1,
            self.config.max_retries,
        )
        if delay > 0:
            time.sleep(delay)
