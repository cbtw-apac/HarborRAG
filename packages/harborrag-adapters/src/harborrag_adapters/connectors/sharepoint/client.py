"""Microsoft Graph client protocol and requests-based implementation."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

import requests  # type: ignore[import-untyped]

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    FetchError,
    RateLimitError,
)
from harborrag_adapters.connectors.utils.http import (
    ResponseTooLargeError,
    read_capped_content,
    require_same_origin_url,
    retry_delay_seconds,
    safe_error_detail,
)

from .config import SharePointSiteConfig

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class SharePointClient(Protocol):
    """Small Microsoft Graph API surface needed by ``SharePointConnector``."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a decoded Microsoft Graph response object."""
        pass

    def get_bytes(self, endpoint: str) -> bytes:
        """Return bytes downloaded from a Microsoft Graph endpoint."""
        pass


class _RequestsGraphClient:
    """Authenticated, rate-limited Microsoft Graph client."""

    def __init__(self, config: SharePointSiteConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0
        self._token: str | None = None
        self._token_expires_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a Graph endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params)
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise FetchError(
                f"Microsoft Graph returned non-JSON for {endpoint}"
            ) from exc
        if not isinstance(payload, dict):
            raise FetchError(f"Microsoft Graph returned invalid JSON for {endpoint}")
        return payload

    def get_bytes(self, endpoint: str) -> bytes:
        """GET a Graph endpoint that returns file bytes, capped by size limit."""
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
        """Send one Graph request with auth, local rate limiting, and retries."""
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
                    raise FetchError(str(exc)) from exc
                self._sleep(attempt, exc)
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(safe_error_detail(response.text))
            if response.status_code == 429 and attempt == self.config.max_retries:
                raise RateLimitError(safe_error_detail(response.text))
            if (
                response.status_code not in _RETRYABLE_STATUS
                or attempt == self.config.max_retries
            ):
                if response.status_code >= 400:
                    raise FetchError(
                        f"Microsoft Graph request failed with HTTP "
                        f"{response.status_code}: {safe_error_detail(response.text)}"
                    )
                return response

            last_error = FetchError(
                f"Microsoft Graph request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _access_token(self) -> str:
        """Return a configured token or obtain/cache one by client credentials."""
        if self.config.access_token:
            return self.config.access_token
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token

        token_url = (
            f"https://login.microsoftonline.com/{self.config.tenant_id}"
            "/oauth2/v2.0/token"
        )
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
            )
        except requests.RequestException as exc:
            raise AuthenticationError(str(exc)) from exc

        if response.status_code >= 400:
            raise AuthenticationError(safe_error_detail(response.text))
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise AuthenticationError(
                "Microsoft identity returned non-JSON token"
            ) from exc

        if not isinstance(payload, dict):
            raise AuthenticationError("Microsoft identity returned invalid JSON token")
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Microsoft identity token response missing token")
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(
            payload.get("expires_in") or 3599
        )
        return self._token

    def _api_url(self, endpoint: str) -> str:
        """Build a Graph API URL while rejecting cross-origin absolute URLs."""
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
            "Retrying Microsoft Graph request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
