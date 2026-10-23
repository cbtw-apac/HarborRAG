"""Jira REST client protocol and its requests-based implementation."""

from __future__ import annotations

import logging
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from harborrag_adapters.connectors.atlassian.client import (
    AtlassianClientContext,
    AtlassianRestClient,
)
from harborrag_adapters.connectors.rate_limiting import (
    ConnectorRateLimiter,
    RateLimitIdentity,
)

from .config import JiraDeploymentType, JiraProjectConfig

logger = logging.getLogger("harborrag.adapters.connectors.jira")


class JiraClient(Protocol):
    """Client surface the Jira connector needs; tests can substitute a fake."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the decoded JSON of a GET request."""
        pass

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        """Return the decoded JSON of a POST request."""
        pass

    def download_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes | None:
        """Download a URL, capped at ``max_bytes`` when given."""
        pass

    def close(self) -> None:
        """Release the HTTP session."""
        pass


class _RequestsJiraClient(AtlassianRestClient[JiraProjectConfig]):
    """Authenticated, rate-limited Jira REST client.

    Cloud uses API v3 with basic auth (email and token); Data Center uses API v2 with
    a bearer token.
    """

    def __init__(
        self,
        config: JiraProjectConfig,
        *,
        rate_limiter: ConnectorRateLimiter | None = None,
    ) -> None:
        super().__init__(
            config,
            context=AtlassianClientContext(
                base_url=config.base_url,
                provider_label="JIRA",
                logger=logger,
                rate_limit_identity=RateLimitIdentity.from_http_source(
                    connector_type="jira",
                    deployment_type=str(config.deployment_type),
                    base_url=config.base_url,
                    credential_parts=(config.email or "", config.token or ""),
                ),
            ),
            rate_limiter=rate_limiter,
        )
        self.api_version = "3" if config.deployment_type == JiraDeploymentType.CLOUD else "2"
        if config.deployment_type == JiraDeploymentType.CLOUD:
            if config.email is None or config.token is None:
                raise ValueError("email and token are required for JIRA Cloud API token auth")
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a REST endpoint and return its decoded JSON."""
        return self._json_response(
            "GET",
            self._api_url(endpoint),
            endpoint,
            params=params,
        )

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        """POST a JSON body and return the decoded response."""
        return self._json_response(
            "POST",
            self._api_url(endpoint),
            endpoint,
            json=json,
        )

    def download_bytes(self, url: str, *, max_bytes: int | None = None) -> bytes | None:
        """Download a file from the configured origin. Cloud URLs get ``redirect=false``."""
        if self.config.deployment_type == JiraDeploymentType.CLOUD:
            url = _with_query_parameter(url, "redirect", "false")
        return self._download_bytes(
            url,
            label="JIRA download",
            max_bytes=max_bytes,
        )

    def _api_url(self, endpoint: str) -> str:
        return f"{self.base_url}/rest/api/{self.api_version}/{endpoint.lstrip('/')}"


def _with_query_parameter(url: str, name: str, value: str) -> str:
    """Return ``url`` with query parameter ``name`` set, keeping the others."""
    parts = urlsplit(url)
    query = [
        (key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True) if key != name
    ]
    query.append((name, value))
    return urlunsplit(parts._replace(query=urlencode(query)))
