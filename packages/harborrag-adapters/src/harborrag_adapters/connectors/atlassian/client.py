"""Shared HTTP behavior for Jira and Confluence REST clients."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock, get_ident, local
from typing import Any, Protocol, Unpack
from urllib.parse import urlparse

import requests

from harborrag_adapters.connectors._http import RequestOptions, RetryingHttpClient, RetryPolicy
from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.policies.http import (
    ResponseTooLargeError,
    read_capped_content,
    read_capped_json,
    require_same_origin_url,
    safe_response_error_detail,
    same_origin,
)
from harborrag_adapters.connectors.policies.network import guarded_session
from harborrag_adapters.connectors.rate_limiting import (
    ConnectorRateLimiter,
    LocalIntervalRateLimiter,
    RateLimitIdentity,
)

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


@dataclass(frozen=True, slots=True)
class AtlassianClientContext:
    """Stable HTTP and rate-limit identity for one Atlassian client."""

    base_url: str
    provider_label: str
    logger: logging.Logger
    rate_limit_identity: RateLimitIdentity


class AtlassianRestClient[ConfigT: AtlassianHttpConfig](RetryingHttpClient):
    """Rate-limited requests client with shared errors and download safety."""

    def __init__(
        self,
        config: ConfigT,
        *,
        context: AtlassianClientContext,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        super().__init__(label=context.provider_label, logger=context.logger)
        self.config = config
        self.base_url = context.base_url.rstrip("/")
        self.session = guarded_session()
        self.session.headers.update({"Accept": "application/json"})
        self._redirect_session = guarded_session()
        self._owner_thread = get_ident()
        self._thread_sessions = local()
        self._sessions = {self.session, self._redirect_session}
        self._sessions_lock = Lock()
        self._provider_label = context.provider_label
        self._rate_limit_identity = context.rate_limit_identity
        self._rate_limiter = rate_limiter or LocalIntervalRateLimiter()

    def _json_response(
        self,
        method: str,
        url: str,
        endpoint: str,
        **options: Unpack[RequestOptions],
    ) -> dict[str, Any]:
        """Send a request and require a JSON object response."""
        options.setdefault("stream", True)
        response = self.request(method, url, **options)
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise FetchError(
                f"{self._provider_label} returned non-JSON response for {endpoint}"
            ) from exc
        if not isinstance(payload, dict):
            raise FetchError(
                f"{self._provider_label} returned invalid JSON for {endpoint}: expected an object"
            )
        return payload

    def _download_bytes(
        self,
        url: str,
        *,
        label: str,
        max_bytes: int | None = None,
    ) -> bytes | None:
        """Download a capped body, following at most one trusted media redirect."""
        try:
            safe_url = require_same_origin_url(url, self.base_url, label=label)
        except ValueError as exc:
            raise FetchError(str(exc)) from exc
        response = self.request(
            "GET",
            safe_url,
            headers={"Accept": "*/*"},
            stream=True,
            allow_redirects=False,
        )
        if 300 <= response.status_code < 400:
            response = self._follow_trusted_redirect(response, label=label)
        try:
            configured_cap = self.config.max_attachment_size_bytes
            caps = tuple(cap for cap in (configured_cap, max_bytes) if cap is not None)
            content = read_capped_content(response, min(caps) if caps else None)
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
            follow_up = self._redirect_session.get(
                location,
                headers={"Accept": "*/*"},
                timeout=self.config.request_timeout_seconds,
                stream=True,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise FetchError(f"{label} redirect request failed") from exc
        if follow_up.status_code >= 400:
            detail = safe_response_error_detail(follow_up)
            raise FetchError(
                f"{label} redirect target failed with HTTP {follow_up.status_code}: {detail}"
            )
        if 300 <= follow_up.status_code < 400:
            raise FetchError(f"{label} refused chained HTTP redirect {follow_up.status_code}")
        return follow_up

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(self.config.max_retries, self.config.backoff_factor)

    def _send(self, method: str, url: str, **options: Unpack[RequestOptions]) -> requests.Response:
        """Send one throttled attempt on this thread's session."""
        self._acquire(url)
        return self._session_for_thread().request(
            method,
            url,
            timeout=self.config.request_timeout_seconds,
            **options,
        )

    def close(self) -> None:
        """Close the connector-owned HTTP connection pool."""

        with self._sessions_lock:
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    def _session_for_thread(self) -> requests.Session:
        """Return an independently pooled session for concurrent descriptors."""

        if get_ident() == self._owner_thread:
            return self.session
        session: requests.Session | None = getattr(self._thread_sessions, "session", None)
        if session is not None:
            return session
        session = guarded_session()
        session.headers.update(self.session.headers)
        session.auth = self.session.auth
        session.cookies.update(self.session.cookies)
        session.verify = self.session.verify
        session.cert = self.session.cert
        session.proxies.update(self.session.proxies)
        with self._sessions_lock:
            self._sessions.add(session)
        self._thread_sessions.session = session
        return session

    def _acquire(self, url: str) -> None:
        """Throttle requests in the source API-family lane."""

        path = urlparse(url).path.lower()
        api_family = "rest" if "/rest/api/" in path else "attachment"
        self._rate_limiter.acquire(
            self._rate_limit_identity.scope(api_family),
            requests_per_minute=self.config.requests_per_minute,
        )
