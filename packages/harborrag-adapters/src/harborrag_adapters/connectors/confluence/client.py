"""Confluence client protocol and requests-based implementation."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from harborrag_adapters.connectors.shared.atlassian_client import AtlassianRestClient

from .config import ConfluenceDeploymentType, ConfluenceSpaceConfig

logger = logging.getLogger("harborrag.adapters.connectors.confluence")


class ConfluenceClient(Protocol):
    """Small API surface needed by ``ConfluenceConnector``.

    Tests can provide this protocol without constructing a real authenticated
    requests session.
    """

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a decoded Confluence response object."""

    def download_bytes(self, url: str) -> bytes | None:
        """Download bytes from a trusted Confluence URL."""


class _RequestsConfluenceClient(AtlassianRestClient[ConfluenceSpaceConfig]):
    """Authenticated, rate-limited Confluence REST client."""

    def __init__(self, config: ConfluenceSpaceConfig) -> None:
        super().__init__(
            config,
            base_url=config.base_url,
            provider_label="Confluence",
            logger=logger,
        )
        if config.deployment_type == ConfluenceDeploymentType.CLOUD:
            self.session.auth = (config.email, config.token)
        else:
            self.session.headers.update({"Authorization": f"Bearer {config.token}"})

    def get_json(
        self,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET a Confluence REST endpoint and decode its JSON body."""
        return self._json_response(
            "GET",
            self._api_url(endpoint),
            endpoint,
            params=params,
        )

    def download_bytes(self, url: str) -> bytes | None:
        """Download attachment bytes only from the configured Confluence origin."""
        return self._download_bytes(url, label="Confluence download")

    def _api_url(self, endpoint: str) -> str:
        """Build a Confluence REST API URL from a relative endpoint."""
        return f"{self.base_url}/rest/api/{endpoint.lstrip('/')}"
