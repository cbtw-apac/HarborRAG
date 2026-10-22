"""Microsoft Graph client protocol and requests-based implementation."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Protocol

import requests

from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
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

from .config import SharePointAuthMode, SharePointSiteConfig
from .delegated import DELEGATED_SCOPE, LOGIN_HINT, DelegatedTokenCache, identity_url
from .permissions import reject_broad_delegated_scopes, reject_broad_permissions

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

    def close(self) -> None:
        """Release connector-owned HTTP resources."""
        pass


class _RequestsGraphClient:
    """Authenticated, rate-limited Microsoft Graph client."""

    def __init__(self, config: SharePointSiteConfig) -> None:
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
        """GET a Graph endpoint and decode its JSON body."""
        response = self._request("GET", self._api_url(endpoint), params=params, stream=True)
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise FetchError(f"Microsoft Graph returned non-JSON for {endpoint}") from exc
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
                    raise FetchError("Microsoft Graph request failed") from exc
                self._sleep(attempt, exc)
                continue

            self._raise_for_final_status(response, final_attempt=attempt == self.config.max_retries)
            if response.status_code not in _RETRYABLE_STATUS or attempt == self.config.max_retries:
                return response

            last_error = FetchError(f"Microsoft Graph request returned HTTP {response.status_code}")
            retry_headers = response.headers
            response.close()
            self._sleep(attempt, last_error, retry_headers)

        raise FetchError("Microsoft Graph request failed") from last_error

    @staticmethod
    def _raise_for_final_status(response: requests.Response, *, final_attempt: bool) -> None:
        """Raise the typed error for a response that will not be retried.

        A 401 means the credentials are rejected, which is fatal for the run. A
        403 means the app lacks access to that resource, which callers handle
        per item (or as fatal when it is the configured site itself).
        """
        status = response.status_code
        if status == 401:
            raise AuthenticationError(safe_response_error_detail(response), status_code=401)
        if status == 403:
            raise AuthorizationError(safe_response_error_detail(response), status_code=403)
        if status in _RETRYABLE_STATUS and not final_attempt:
            return
        if status == 429:
            raise RateLimitError(safe_response_error_detail(response), status_code=429)
        if status >= 400:
            raise FetchError(
                f"Microsoft Graph request failed with HTTP "
                f"{status}: {safe_response_error_detail(response)}",
                status_code=status,
            )

    def _access_token(self) -> str:
        """Return a configured token or obtain/cache one for the auth mode.

        Client credentials request an app-only token; device code redeems the
        signed-in user's cached refresh token and stores the rotated one.
        """
        if self.config.auth_mode == SharePointAuthMode.ACCESS_TOKEN and self.config.access_token:
            reject_broad_permissions(self.config.access_token)
            return self.config.access_token
        if self._token and time.monotonic() < self._token_expires_at - 60:
            return self._token

        cache = self._delegated_cache()
        form = self._token_form(cache)
        try:
            response = self._request_token(form)
        except AuthenticationError as exc:
            if cache is None:
                raise
            # An expired or revoked refresh token only recovers by signing in again.
            raise AuthenticationError(f"{exc}; {LOGIN_HINT}", status_code=exc.status_code) from exc
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise AuthenticationError("Microsoft identity returned non-JSON token") from exc

        if not isinstance(payload, dict):
            raise AuthenticationError("Microsoft identity returned invalid JSON token")
        token = payload.get("access_token")
        if not token:
            raise AuthenticationError("Microsoft identity token response missing token")
        reject_broad_permissions(str(token))
        if cache is not None:
            reject_broad_delegated_scopes(str(token))
            rotated = payload.get("refresh_token")
            if rotated:
                cache.save(str(rotated))
        self._token = str(token)
        self._token_expires_at = time.monotonic() + int(payload.get("expires_in") or 3599)
        return self._token

    def _token_form(self, cache: DelegatedTokenCache | None) -> dict[str, str]:
        """Return the OAuth token request body for client credentials or a refresh."""
        if cache is None:
            return {
                "client_id": str(self.config.client_id),
                "client_secret": str(self.config.client_secret),
                "grant_type": "client_credentials",
                "scope": "https://graph.microsoft.com/.default",
            }
        return {
            "client_id": str(self.config.client_id),
            "grant_type": "refresh_token",
            "refresh_token": cache.load_refresh_token(),
            "scope": DELEGATED_SCOPE,
        }

    def _delegated_cache(self) -> DelegatedTokenCache | None:
        """Return the refresh token cache for device code auth, else ``None``."""
        if self.config.auth_mode != SharePointAuthMode.DEVICE_CODE:
            return None
        return DelegatedTokenCache(
            path=Path(str(self.config.token_cache_path)),
            tenant_id=str(self.config.tenant_id),
            client_id=str(self.config.client_id),
        )

    def _request_token(self, form: dict[str, str]) -> requests.Response:
        """POST for an OAuth token, retrying transient failures and 429/5xx."""
        token_url = identity_url(str(self.config.tenant_id), "token")
        last_error: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.post(
                    token_url,
                    data=form,
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
        """Close the connector-owned HTTP connection pool and discard its token."""

        self._token = None
        self._token_expires_at = 0.0
        self.session.close()

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
        with self._rate_lock:
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
            "Retrying Microsoft Graph request after %s, attempt %d/%d",
            type(error).__name__,
            attempt + 1,
            self.config.max_retries,
        )
        if delay > 0:
            time.sleep(delay)
