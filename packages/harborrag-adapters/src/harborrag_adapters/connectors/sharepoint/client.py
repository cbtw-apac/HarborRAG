"""Microsoft Graph client protocol and requests-based implementation."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Protocol, Unpack

import requests

from harborrag_adapters.connectors._http import RequestOptions, RetryingHttpClient, RetryPolicy
from harborrag_adapters.connectors.exceptions import (
    AuthenticationError,
    AuthorizationError,
    FetchError,
)
from harborrag_adapters.connectors.policies.http import (
    DEFAULT_MAX_RETRY_DELAY_SECONDS,
    ResponseTooLargeError,
    read_capped_content,
    read_capped_json,
    require_same_origin_url,
)
from harborrag_adapters.connectors.policies.network import guarded_session
from harborrag_adapters.connectors.rate_limiting import (
    ConnectorRateLimiter,
    LocalIntervalRateLimiter,
    RateLimitIdentity,
)

from .config import SharePointAuthMode, SharePointSiteConfig
from .delegated import DELEGATED_SCOPE, LOGIN_HINT, DelegatedTokenCache, identity_url
from .permissions import reject_broad_delegated_scopes, reject_broad_permissions

logger = logging.getLogger("harborrag.adapters.connectors.sharepoint")


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


class _GraphRetries(RetryingHttpClient):
    """Retry policy shared by the Graph and identity clients of one site config."""

    config: SharePointSiteConfig

    def _retry_policy(self) -> RetryPolicy:
        # Retrying before Graph's Retry-After only earns another throttle, so a
        # longer requested wait ends the request and the item is retried next run.
        return RetryPolicy(
            self.config.max_retries,
            self.config.backoff_factor,
            max_retry_after_seconds=DEFAULT_MAX_RETRY_DELAY_SECONDS,
        )


class _IdentityClient(_GraphRetries):
    """Token requests to Microsoft identity, outside the Graph rate-limit lane.

    Args:
        config: Site config that supplies the timeout and retry settings.
        session: Session the Graph client owns; it carries no Graph token.
    """

    def __init__(self, config: SharePointSiteConfig, session: requests.Session) -> None:
        super().__init__(label="Microsoft identity", logger=logger)
        self.config = config
        self._session = session

    def _send(self, method: str, url: str, **options: Unpack[RequestOptions]) -> requests.Response:
        return self._session.request(
            method, url, timeout=self.config.request_timeout_seconds, **options
        )


class _RequestsGraphClient(_GraphRetries):
    """Authenticated, rate-limited Microsoft Graph client."""

    def __init__(
        self,
        config: SharePointSiteConfig,
        *,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        super().__init__(label="Microsoft Graph", logger=logger)
        self.config = config
        self.session = guarded_session()
        self.session.headers.update({"Accept": "application/json"})
        # Graph throttles per app registration and tenant, so every worker
        # sharing them draws from one lane of the shared limiter.
        self._rate_limit_identity = RateLimitIdentity.from_http_source(
            connector_type="sharepoint",
            deployment_type=str(config.auth_mode),
            base_url=config.graph_api_url,
            credential_parts=(str(config.tenant_id or ""), str(config.client_id or "")),
        )
        self._rate_limiter = rate_limiter or LocalIntervalRateLimiter()
        self._token: str | None = None
        self._token_expires_at = 0.0

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a Graph endpoint and decode its JSON body."""
        response = self.request("GET", self._api_url(endpoint), params=params, stream=True)
        try:
            payload = read_capped_json(response)
        except (ValueError, ResponseTooLargeError) as exc:
            raise FetchError(f"Microsoft Graph returned non-JSON for {endpoint}") from exc
        if not isinstance(payload, dict):
            raise FetchError(f"Microsoft Graph returned invalid JSON for {endpoint}")
        return payload

    def get_bytes(self, endpoint: str) -> bytes:
        """GET a Graph endpoint that returns file bytes, capped by size limit."""
        response = self.request(
            "GET",
            self._api_url(endpoint),
            headers={"Accept": "*/*"},
            stream=True,
        )
        try:
            return read_capped_content(response, self.config.max_file_size_bytes)
        except ResponseTooLargeError as exc:
            raise FetchError(str(exc)) from exc

    def _send(self, method: str, url: str, **options: Unpack[RequestOptions]) -> requests.Response:
        """Send one attempt with the current token; a retry may outlive the last one."""
        # ``options`` is this attempt's own copy, so the token never leaks into a retry.
        options["headers"] = {
            **options.get("headers", {}),
            "Authorization": f"Bearer {self._access_token()}",
        }
        self._acquire()
        return self.session.request(
            method, url, timeout=self.config.request_timeout_seconds, **options
        )

    def _refresh_credentials(self) -> bool:
        """Drop the cached token after a 401 so one resend tells expiry from bad credentials."""
        if not self._can_refresh_token():
            return False
        self._discard_token()
        return True

    def _can_refresh_token(self) -> bool:
        """Whether a new token can be obtained; a configured one is used as-is."""
        return self.config.auth_mode != SharePointAuthMode.ACCESS_TOKEN

    def _discard_token(self) -> None:
        self._token = None
        self._token_expires_at = 0.0

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
        """POST for an OAuth token; every failure to get one is an authentication failure."""
        token_url = identity_url(str(self.config.tenant_id), "token")
        try:
            return _IdentityClient(self.config, self.session).request(
                "POST", token_url, data=form, stream=True
            )
        except (AuthenticationError, AuthorizationError, FetchError) as exc:
            raise AuthenticationError(str(exc), status_code=exc.status_code) from exc

    def close(self) -> None:
        """Close the connector-owned HTTP connection pool and discard its token."""

        self._discard_token()
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
        """Throttle requests through the shared limiter's Graph lane."""
        self._rate_limiter.acquire(
            self._rate_limit_identity.scope("graph"),
            requests_per_minute=self.config.requests_per_minute,
        )
