"""GitHub client protocol and requests-based implementation."""

from __future__ import annotations

import logging
from typing import Any, Protocol, Unpack

import requests

from harborrag_adapters.connectors._http import RequestOptions, RetryingHttpClient, RetryPolicy
from harborrag_adapters.connectors.exceptions import FetchError
from harborrag_adapters.connectors.policies.http import (
    DEFAULT_JSON_BODY_LIMIT,
    ResponseTooLargeError,
    read_capped_json,
    require_same_origin_url,
    safe_response_error_detail,
)
from harborrag_adapters.connectors.policies.network import guarded_session
from harborrag_adapters.connectors.rate_limiting import (
    ConnectorRateLimiter,
    LocalIntervalRateLimiter,
    RateLimitIdentity,
)

from .config import GitHubRepositoryConfig

logger = logging.getLogger("harborrag.adapters.connectors.github")


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


class _RequestsGitHubClient(RetryingHttpClient):
    """GitHub REST client on the shared retry loop and the token's rate-limit lane.

    A 403 that carries GitHub's primary or secondary rate-limit signal is
    retried like a 429.

    Args:
        config: Repository config that supplies the API URL, token and request settings.
        rate_limiter: Limiter shared by every worker reading with the same token;
            a per-process limiter when omitted.
    """

    def __init__(
        self,
        config: GitHubRepositoryConfig,
        *,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        super().__init__(label="GitHub", logger=logger)
        self.config = config
        self.session = guarded_session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": config.api_version,
            }
        )
        if config.token:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})
        # GitHub throttles per token, so every worker using it shares one lane.
        self._rate_limit_identity = RateLimitIdentity.from_http_source(
            connector_type="github",
            deployment_type="rest",
            base_url=config.api_url,
            credential_parts=(config.token or "",),
        )
        self._rate_limiter = rate_limiter or LocalIntervalRateLimiter()

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
        response = self.request("GET", self._api_url(endpoint), params=params, stream=True)
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

    def _retry_policy(self) -> RetryPolicy:
        return RetryPolicy(self.config.max_retries, self.config.backoff_factor)

    def _send(self, method: str, url: str, **options: Unpack[RequestOptions]) -> requests.Response:
        """Send one attempt through the shared limiter's REST lane."""
        self._rate_limiter.acquire(
            self._rate_limit_identity.scope("rest"),
            requests_per_minute=self.config.requests_per_minute,
        )
        return self.session.request(
            method,
            url,
            timeout=self.config.request_timeout_seconds,
            **options,
        )

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
        """A 429, or a 403 that carries GitHub's primary or secondary rate-limit signal."""
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
