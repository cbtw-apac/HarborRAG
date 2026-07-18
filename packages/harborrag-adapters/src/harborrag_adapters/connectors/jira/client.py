"""JIRA client protocol and requests-based implementation."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from harborrag_adapters.connectors.shared.atlassian_client import AtlassianRestClient

from .config import JiraDeploymentType, JiraProjectConfig

logger = logging.getLogger("harborrag.adapters.connectors.jira")


class JiraClient(Protocol):
    """Small API surface needed by ``JiraConnector`` for tests and clients."""

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a decoded JIRA GET response."""
        pass

    def post_json(
        self,
        endpoint: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a decoded JIRA POST response."""
        pass

    def download_bytes(self, url: str) -> bytes | None:
        """Download bytes from a trusted JIRA URL."""
        pass


class _RequestsJiraClient(AtlassianRestClient[JiraProjectConfig]):
    """Authenticated, rate-limited JIRA REST client."""

    def __init__(self, config: JiraProjectConfig) -> None:
        super().__init__(
            config,
            base_url=config.base_url,
            provider_label="JIRA",
            logger=logger,
        )
        self.api_version = "3" if config.deployment_type == JiraDeploymentType.CLOUD else "2"
        if config.deployment_type == JiraDeploymentType.CLOUD:
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a JIRA REST endpoint and decode its JSON body."""
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
        """POST a JSON body to a JIRA REST endpoint and decode the response."""
        return self._json_response(
            "POST",
            self._api_url(endpoint),
            endpoint,
            json=json,
        )

    def download_bytes(self, url: str) -> bytes | None:
        """Download attachment bytes only from the configured JIRA origin."""
        return self._download_bytes(url, label="JIRA download")

    def _api_url(self, endpoint: str) -> str:
        """Build a JIRA REST API URL from a relative endpoint."""
        return f"{self.base_url}/rest/api/{self.api_version}/{endpoint.lstrip('/')}"
