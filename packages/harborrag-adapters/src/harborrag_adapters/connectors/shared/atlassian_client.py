"""Shared HTTP behavior for Jira and Confluence REST clients."""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol
from urllib.parse import urlparse

import requests

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
    same_origin,
)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Confluence/Jira Cloud attachment downloads redirect once to Atlassian's
# media platform with a short-lived, file-scoped signed URL baked into the
# query string. That single hop is safe to follow without our own Atlassian
# credentials; every other cross-origin redirect target stays refused.
_TRUSTED_MEDIA_REDIRECT_HOSTS = frozenset({"api.media.atlassian.com"})


def _is_trusted_redirect(location: str, base_url: str) -> bool:
    """Return whether a redirect target is safe to follow."""
    parsed = urlparse(location)
    if parsed.scheme.lower() != "https":
        return False
    if (parsed.hostname or "").lower() in _TRUSTED_MEDIA_REDIRECT_HOSTS:
        return True
    return same_origin(location, base_url)


class AtlassianHttpConfig(Protocol):
    """Configuration fields shared by the two Atlassian HTTP clients."""

    requests_per_minute: int
    request_timeout_seconds: float
    max_retries: int
    backoff_factor: float
    max_attachment_size_bytes: int | None


class AtlassianRestClient[ConfigT: AtlassianHttpConfig]:
    """Rate-limited requests client with shared errors and download safety."""

    def __init__(
        self,
        config: ConfigT,
        *,
        base_url: str,
        provider_label: str,
        logger: logging.Logger,
    ) -> None:
        self.config = config
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._provider_label = provider_label
        self._logger = logger
        self._min_interval = 60.0 / config.requests_per_minute
        self._last_request_at = 0.0

    def _json_response(
        self,
        method: str,
        url: str,
        endpoint: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send a request and require a JSON object response."""
        response = self._request(method, url, **kwargs)
        try:
            payload: object = response.json()
        except ValueError as exc:
            raise FetchError(
                f"{self._provider_label} returned non-JSON response for {endpoint}"
            ) from exc
        if not isinstance(payload, dict):
            raise FetchError(
                f"{self._provider_label} returned invalid JSON for {endpoint}: expected an object"
            )
        return payload

    def _download_bytes(self, url: str, *, label: str) -> bytes | None:
        """Download a capped body, following at most one trusted media redirect."""
        try:
            safe_url = require_same_origin_url(url, self.base_url, label=label)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        response = self._request(
            "GET",
            safe_url,
            headers={"Accept": "*/*"},
            stream=True,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            response = self._follow_trusted_redirect(response, label=label)
        try:
            content = read_capped_content(
                response,
                self.config.max_attachment_size_bytes,
            )
        except ResponseTooLargeError as exc:
            raise FetchError(str(exc)) from exc
        return content or None

    def _follow_trusted_redirect(
        self,
        response: requests.Response,
        *,
        label: str,
    ) -> requests.Response:
        """Follow a single validated redirect hop without forwarding our credentials.

        The initial request already proved the origin trustworthy; the redirect
        Location is server-issued, not attacker-supplied. Untrusted targets
        (anything but the same origin or Atlassian's media platform) are still
        refused, and the fetch to the trusted target is anonymous so our
        session's Confluence/Jira credentials never reach a different origin.
        """
        location = response.headers.get("Location") or ""
        response.close()
        if not location or not _is_trusted_redirect(location, self.base_url):
            raise FetchError(f"{label} refused HTTP redirect {response.status_code}")
        try:
            follow_up = requests.get(
                location,
                headers={"Accept": "*/*"},
                timeout=self.config.request_timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FetchError(str(exc)) from exc
        if follow_up.status_code >= 400:
            detail = safe_error_detail(follow_up.text)
            raise FetchError(f"{label} redirect target failed with HTTP {follow_up.status_code}: {detail}")
        if 300 <= follow_up.status_code < 400:
            raise FetchError(f"{label} refused chained HTTP redirect {follow_up.status_code}")
        return follow_up

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Send one HTTP request with rate limiting and retry handling."""
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

            if response.status_code == 401:
                raise AuthenticationError(safe_error_detail(response.text))
            if response.status_code == 429 and attempt == self.config.max_retries:
                raise RateLimitError(safe_error_detail(response.text))
            if response.status_code not in _RETRYABLE_STATUS or attempt == self.config.max_retries:
                if response.status_code >= 400:
                    detail = safe_error_detail(response.text)
                    raise FetchError(
                        f"{self._provider_label} request failed with HTTP "
                        f"{response.status_code}: {detail}"
                    )
                return response

            last_error = FetchError(
                f"{self._provider_label} request returned HTTP {response.status_code}"
            )
            self._sleep(attempt, last_error, response.headers)

        raise FetchError(str(last_error))

    def _acquire(self) -> None:
        """Throttle requests according to the configured per-minute budget."""
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _sleep(self, attempt: int, error: Exception, headers: Any = None) -> None:
        """Sleep before retrying, honoring provider retry headers."""
        fallback_delay = self.config.backoff_factor * (2**attempt)
        delay = retry_delay_seconds(headers, fallback_delay)
        self._logger.warning(
            "Retrying %s request after error, attempt %d/%d: %s",
            self._provider_label,
            attempt + 1,
            self.config.max_retries,
            error,
        )
        if delay > 0:
            time.sleep(delay)
