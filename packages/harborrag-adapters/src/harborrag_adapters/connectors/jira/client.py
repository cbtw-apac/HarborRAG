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
from harborrag_adapters.connectors.http_utils import (
    ResponseTooLargeError,
    read_capped_content,
    require_same_origin_url,
    retry_delay_seconds,
    safe_error_detail,
)

from .config import JiraDeploymentType, JiraProjectConfig


logger = logging.getLogger("harborrag.adapters.connectors.jira")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class JiraClient(Protocol):
    """Small API surface needed by ``JiraConnector`` for tests and clients."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        pass

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> Any:
        pass

    def download_bytes(self, url: str) -> bytes | None:
        pass


class _RequestsJiraClient:
    """Authenticated, rate-limited JIRA REST client."""

    def __init__(self, config: JiraProjectConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.api_version = (
            "3" if config.deployment_type == JiraDeploymentType.CLOUD else "2"
        )
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        if config.deployment_type == JiraDeploymentType.CLOUD:
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """GET a JIRA REST endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"JIRA returned non-JSON response for {endpoint}") from exc

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> Any:
        """POST a JSON body to a JIRA REST endpoint and decode the response."""
        response = self._request("POST", self._api_url(endpoint), json=json)
        try:
            return response.json()
        except ValueError as exc:
            raise FetchError(f"JIRA returned non-JSON response for {endpoint}") from exc

    def download_bytes(self, url: str) -> bytes | None:
        """Download attachment bytes only from the configured JIRA origin."""
        try:
            safe_url = require_same_origin_url(
                url,
                self.base_url,
                label="JIRA download",
            )
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        response = self._request(
            "GET", safe_url, headers={"Accept": "*/*"}, stream=True
        )
        try:
            content = read_capped_content(
                response, self.config.max_attachment_size_bytes
            )
        except ResponseTooLargeError as exc:
            raise FetchError(str(exc)) from exc
        return content or None

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
                        f"JIRA request failed with HTTP "
                        f"{response.status_code}: {safe_error_detail(response.text)}"
                    )
                return response

            last_error = FetchError(
                f"JIRA request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _api_url(self, endpoint: str) -> str:
        """Build a JIRA REST API URL from a relative endpoint."""
        return f"{self.base_url}/rest/api/{self.api_version}/{endpoint.lstrip('/')}"

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
            "Retrying JIRA request after error, attempt %d/%d: %s",
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
